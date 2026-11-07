"""AI API 클라이언트 — 외부 라이브러리 없이 urllib로 REST 호출한다.

지원 방식
  - OpenAI 호환 Chat Completions  (gemini, openai, 그 외 --base-url 로 지정한 호환 서버)
  - Anthropic Messages API        (anthropic)

요청 구성 → 전송 → 응답 파싱 → 예외를 '원인이 드러나는 메시지'로 변환, 이 4단계가 전부다.
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Optional

# ====================================================================
# [1] 제공자 설정 — 어느 AI 서비스에, 어떤 주소/모델/키 이름으로 보낼지
# ====================================================================
PROVIDERS: Dict[str, dict] = {
    "gemini": {
        "style": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.5-flash",
        "key_env": "GEMINI_API_KEY",
        "key_url": "https://aistudio.google.com/apikey",
        # 2.5 Flash는 답하기 전에 '생각(thinking)'을 하며 max_tokens를 같이 쓴다.
        # low로 생각 분량을 제한해 답변이 잘리지 않게 한다. (none/low/medium/high)
        "default_reasoning_effort": "low",
    },
    "openai": {
        "style": "openai",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4.1-mini",
        "key_env": "OPENAI_API_KEY",
        "key_url": "https://platform.openai.com/api-keys",
    },
    "anthropic": {
        "style": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-haiku-5-5",
        "key_env": "ANTHROPIC_API_KEY",
        "key_url": "https://console.anthropic.com/settings/keys",
    },
}


# ====================================================================
# [2] 데이터 구조 — 오류 / 요청 / 응답
# ====================================================================
class AIError(Exception):
    def __init__(self, message: str, hint: Optional[str] = None):
        super().__init__(message)
        self.hint = hint


@dataclass
class AIRequest:
    provider: str
    model: str
    system: str
    user: str
    temperature: float = 0.3
    max_tokens: int = 2048
    timeout: float = 60.0
    base_url: Optional[str] = None
    reasoning_effort: Optional[str] = None   # None 이면 요청에 넣지 않음


@dataclass
class AIResponse:
    text: str
    model: str
    finish_reason: Optional[str]
    usage: dict = field(default_factory=dict)
    elapsed: float = 0.0


# ====================================================================
# [3] API Key 읽기 — 환경변수에서만 (하드코딩 금지)
# ====================================================================
def resolve_api_key(provider: str) -> str:
    """AI_API_KEY 우선, 없으면 제공자별 환경변수(GEMINI_API_KEY 등)를 본다. 코드에 키를 두지 않는다."""
    key = os.environ.get("AI_API_KEY") or os.environ.get(PROVIDERS.get(provider, {}).get("key_env", ""), "")
    key = key.strip()
    if not key:
        p = PROVIDERS.get(provider, {})
        raise AIError(
            "AI_API_KEY 환경변수가 설정되지 않았습니다.",
            hint=f'예) export AI_API_KEY="YOUR_KEY"'
                 + (f"\n키 발급: {p['key_url']}" if p.get("key_url") else ""),
        )
    return key


# ====================================================================
# [4] 요청 만들기 — URL · 헤더 · JSON 본문 조립
# ====================================================================
def _build_http_request(req: AIRequest, key: str) -> urllib.request.Request:
    p = PROVIDERS.get(req.provider, PROVIDERS["openai"])
    base = (req.base_url or p["base_url"]).rstrip("/")
    headers = {"Content-Type": "application/json", "User-Agent": "ai-gitgen/0.1"}

    # --- 4-1. Anthropic Messages API 형식 ---
    if p["style"] == "anthropic":
        url = f"{base}/messages"
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        body = {
            "model": req.model,
            "system": req.system,
            "messages": [{"role": "user", "content": req.user}],
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
        }
    else:
        # --- 4-2. OpenAI 호환 Chat Completions 형식 (gemini / openai) ---
        url = f"{base}/chat/completions"
        headers["Authorization"] = f"Bearer {key}"
        body = {
            "model": req.model,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.user},
            ],
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }
        # 추론(thinking) 모델의 생각 분량 — 지원하는 모델에만 보낸다 (gpt-4.1 계열은 미지원 → 400)
        if req.reasoning_effort:
            body["reasoning_effort"] = req.reasoning_effort
    # --- 4-3. JSON 직렬화 → POST 요청 객체 ---
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return urllib.request.Request(url, data=data, headers=headers, method="POST")


# ====================================================================
# [5] 오류 메시지 변환 — 응답 본문에서 원인 문장 꺼내기 + 상태코드별 안내
# ====================================================================
def _error_message(body: str) -> str:
    try:
        j = json.loads(body)
        if isinstance(j, list) and j:
            j = j[0]
        err = j.get("error", j)
        if isinstance(err, dict):
            return err.get("message") or json.dumps(err, ensure_ascii=False)[:300]
        return str(err)[:300]
    except Exception:
        return body.strip()[:300] or "(응답 본문 없음)"


_HTTP_HINTS = {
    400: ("잘못된 요청", "모델 이름(--model)이나 파라미터(--temperature, --max-tokens) 값을 확인하세요."),
    401: ("인증 실패", "API Key가 틀렸거나 만료됐습니다. AI_API_KEY 값과 --provider 가 맞는지 확인하세요."),
    403: ("권한 없음", "이 키로 해당 모델/리전에 접근할 수 없습니다. 키의 권한·결제 상태를 확인하세요."),
    404: ("찾을 수 없음", "모델 이름이 틀렸거나 엔드포인트(--base-url)가 잘못됐습니다."),
    429: ("요청 한도 초과", "무료 할당량/분당 호출 수를 넘었습니다. 잠시 후 다시 시도하세요."),
}


# ====================================================================
# [6] 호출 함수 — 전송 → 응답 파싱 → 예외 처리 (정확히 1회)
# ====================================================================
def call(req: AIRequest) -> AIResponse:
    """AI API를 정확히 1회 호출한다 (자동 재시도 없음 → 비용 예측 가능)."""
    # --- 6-1. 요청 준비 ---
    key = resolve_api_key(req.provider)
    http_req = _build_http_request(req, key)
    t0 = time.monotonic()
    # --- 6-2. 전송 + HTTP / 네트워크 / SSL / 타임아웃 예외 ---
    try:
        with urllib.request.urlopen(http_req, timeout=req.timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        title, hint = _HTTP_HINTS.get(e.code, ("서버 오류" if e.code >= 500 else "요청 실패",
                                               "AI 서비스 쪽 일시 장애일 수 있습니다. 잠시 후 다시 시도하세요."))
        raise AIError(f"API 호출 실패 (HTTP {e.code} {title}): {_error_message(body)}", hint=hint)
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise AIError(f"SSL 인증서 검증 실패: {reason}",
                          hint="macOS python.org 설치판이면 '/Applications/Python 3.x/Install Certificates.command'를 실행하세요.")
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise AIError(f"API 응답 시간 초과 ({req.timeout:.0f}초)", hint="--timeout 값을 늘리거나 네트워크를 확인하세요.")
        raise AIError(f"네트워크 오류: {reason}", hint="인터넷 연결, 프록시/방화벽, --base-url 주소를 확인하세요.")
    except (socket.timeout, TimeoutError):
        raise AIError(f"API 응답 시간 초과 ({req.timeout:.0f}초)", hint="--timeout 값을 늘리거나 네트워크를 확인하세요.")
    # --- 6-3. 응답 JSON 파싱 ---
    elapsed = time.monotonic() - t0

    try:
        j = json.loads(raw)
    except json.JSONDecodeError:
        raise AIError(f"응답이 JSON이 아닙니다: {raw[:200]}")

    # --- 6-4. 제공자별 응답 형식에서 텍스트·토큰 사용량 꺼내기 ---
    style = PROVIDERS.get(req.provider, PROVIDERS["openai"])["style"]
    try:
        if style == "anthropic":
            text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
            finish = j.get("stop_reason")
            u = j.get("usage", {})
            usage = {"input": u.get("input_tokens"), "output": u.get("output_tokens")}
        else:
            choice = j["choices"][0]
            text = (choice.get("message") or {}).get("content") or ""
            finish = choice.get("finish_reason")
            u = j.get("usage") or {}
            usage = {"input": u.get("prompt_tokens"), "output": u.get("completion_tokens")}
            # 생각(thinking)에 쓴 토큰: 상세 필드가 있으면 그 값, 없으면 total - input - output 으로 추정
            detail = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
            if detail is None and u.get("total_tokens") and usage["input"] is not None and usage["output"] is not None:
                detail = u["total_tokens"] - usage["input"] - usage["output"]
            if detail:
                usage["reasoning"] = detail
    except (KeyError, IndexError, TypeError):
        raise AIError(f"예상과 다른 응답 형식입니다: {raw[:300]}")

    # --- 6-5. 빈 응답 검사 (thinking 모델의 max_tokens 소진 등) ---
    if not text.strip():
        hint = None
        if finish in ("length", "max_tokens", "MAX_TOKENS"):
            hint = ("max_tokens 를 다 써버렸습니다. 'thinking(추론)' 모델은 답변 전에 토큰을 쓰므로 "
                    "--max-tokens 를 늘리거나 (예: 4096) --reasoning-effort none 을 써 보세요.")
        raise AIError(f"AI 응답이 비어 있습니다 (finish_reason={finish}).", hint=hint)

    # --- 6-6. 결과 반환 ---
    return AIResponse(text=text, model=j.get("model", req.model), finish_reason=finish,
                      usage=usage, elapsed=elapsed)
