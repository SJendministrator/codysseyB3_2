"""CLI 진입점 — git 수집 → 안전 모드 → 프롬프트 → AI 1회 호출 → 검증/후처리 → 출력."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from . import __version__, ai_client, git_collector, postprocess, prompts, safe_mode

# ====================================================================
# [1] 기본 설정값
# ====================================================================
CONFIG_FILE = ".ai-gitgen.json"

DEFAULTS: Dict[str, Any] = {
    "provider": "gemini",
    "model": None,              # None → 제공자별 기본 모델
    "temperature": 0.3,
    "max_tokens": 2048,
    "timeout": 60.0,
    "lang": "ko",
    "safe_mode": True,
    "max_files": 10,
    "max_lines": 200,
    "base": None,
    "base_url": None,
    "reasoning_effort": None,   # None → 제공자 기본값 (gemini: low, 그 외: 보내지 않음)
}

# ====================================================================
# [2] 터미널 출력 도우미 — 로그([INFO]/[DONE]/[WARN]/[ERROR]) · 블록 · 구분선
# ====================================================================
_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
_C = {"INFO": "36", "DONE": "32", "WARN": "33", "ERROR": "31", "dim": "2", "bold": "1"}


def _c(code: str, s: str) -> str:
    return f"\033[{_C[code]}m{s}\033[0m" if _COLOR else s


def log(level: str, msg: str) -> None:
    stream = sys.stderr if level == "ERROR" else sys.stdout
    print(f"{_c(level, '[' + level + ']')} {msg}", file=stream)


def block(title: str, content: str) -> None:
    print(f"\n{_c('bold', '--- ' + title + ' ---')}")
    print(content)


def rule() -> None:
    print(_c("bold", "-" * 40))


# ====================================================================
# [3] 설정 — 설정 파일 읽기 + 기본값/설정파일/환경변수/CLI 옵션 병합
# ====================================================================
def load_config(path: Optional[str]) -> Dict[str, Any]:
    p = Path(path or CONFIG_FILE)
    if not p.exists():
        if path:
            raise SystemExit(f"[ERROR] 설정 파일을 찾을 수 없습니다: {p}")
        return {}
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"[ERROR] 설정 파일 JSON 형식 오류 ({p}): {e}")
    if any(k in cfg for k in ("api_key", "key", "AI_API_KEY")):
        raise SystemExit("[ERROR] 설정 파일에 API Key를 넣지 마세요. 환경변수 AI_API_KEY 를 사용하세요.")
    return cfg


def merge_settings(args: argparse.Namespace, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """우선순위: 기본값 < 설정 파일 < 환경변수 < CLI 옵션"""
    s = dict(DEFAULTS)
    s.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    if os.environ.get("AI_PROVIDER"):
        s["provider"] = os.environ["AI_PROVIDER"]
    if os.environ.get("AI_MODEL"):
        s["model"] = os.environ["AI_MODEL"]
    for k in DEFAULTS:
        v = getattr(args, k, None)
        if v is not None:
            s[k] = v
    if s["provider"] not in ai_client.PROVIDERS:
        raise SystemExit(f"[ERROR] 지원하지 않는 provider: {s['provider']} "
                         f"(가능: {', '.join(ai_client.PROVIDERS)})")
    s["model"] = s["model"] or ai_client.PROVIDERS[s["provider"]]["default_model"]
    # 생각(thinking) 분량: 지정 안 하면 제공자 기본값, "off" 면 요청에 아예 넣지 않음
    if s["reasoning_effort"] is None:
        s["reasoning_effort"] = ai_client.PROVIDERS[s["provider"]].get("default_reasoning_effort")
    elif s["reasoning_effort"] == "off":
        s["reasoning_effort"] = None
    if not 0 <= float(s["temperature"]) <= 2:
        raise SystemExit("[ERROR] --temperature 는 0 ~ 2 사이여야 합니다.")
    if int(s["max_tokens"]) <= 0:
        raise SystemExit("[ERROR] --max-tokens 는 1 이상이어야 합니다.")
    return s


# ====================================================================
# [4] CLI 인자 정의 — commit / pr 명령과 옵션
# ====================================================================
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("AI API 파라미터")
    g.add_argument("--provider", choices=list(ai_client.PROVIDERS), help="AI 제공자 (기본: gemini, 환경변수 AI_PROVIDER)")
    g.add_argument("--model", "-m", help="모델 이름 (기본: 제공자별 기본 모델, 환경변수 AI_MODEL)")
    g.add_argument("--temperature", "-t", type=float, help="창의성 0~2 (기본: 0.3, 낮을수록 일관됨)")
    g.add_argument("--max-tokens", type=int, dest="max_tokens", help="최대 출력 토큰 (기본: 2048)")
    g.add_argument("--timeout", type=float, help="요청 타임아웃 초 (기본: 60)")
    g.add_argument("--base-url", dest="base_url", help="OpenAI 호환 API 주소 직접 지정 (선택)")
    g.add_argument("--reasoning-effort", dest="reasoning_effort", choices=["none", "low", "medium", "high", "off"],
                   help="추론 모델의 생각 분량 (기본: gemini=low, 그 외 off=보내지 않음)")

    p = common.add_argument_group("입력/출력")
    p.add_argument("--context", "-c", help="변경 이유/요구사항을 AI에게 함께 전달 (품질↑)")
    p.add_argument("--lang", choices=["ko", "en"], help="출력 언어 (기본: ko)")
    p.add_argument("--output", "-o", help="결과를 파일로 저장 (예: git commit -F 파일)")
    p.add_argument("--config", help=f"설정 파일 경로 (기본: ./{CONFIG_FILE} 있으면 자동 로드)")
    p.add_argument("--dry-run", action="store_true", help="API를 호출하지 않고 보낼 프롬프트만 출력")
    p.add_argument("--show-prompt", action="store_true", help="API 호출 전 프롬프트도 함께 출력")

    s = common.add_argument_group("안전 모드 (민감정보 보호)")
    s.add_argument("--safe-mode", dest="safe_mode", action=argparse.BooleanOptionalAction, default=None,
                   help="민감정보 마스킹 + 전송량 제한 (기본: 켜짐, 끄려면 --no-safe-mode)")
    s.add_argument("--max-files", type=int, dest="max_files", help="안전 모드 최대 전송 파일 수 (기본: 10)")
    s.add_argument("--max-lines", type=int, dest="max_lines", help="안전 모드 최대 전송 diff 줄 수 (기본: 200)")

    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="Git 변경 사항으로 커밋 메시지 / PR 초안을 AI로 생성합니다.",
    )
    parser.add_argument("--version", action="version", version=f"ai-gitgen {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="{commit,pr}")
    sub.required = True
    sub.add_parser("commit", parents=[common], help="커밋 메시지 생성")
    pr = sub.add_parser("pr", parents=[common], help="PR 제목/본문 초안 생성")
    pr.add_argument("--base", help="비교할 base 브랜치 (기본: main/master 자동 탐지)")
    return parser


# ====================================================================
# [5] 메인 실행 흐름 — 수집 → 안전 모드 → 프롬프트 → AI 호출 → 검증 → 출력
# ====================================================================
def run(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        git_collector.ensure_repo_root()
        cfg = load_config(args.config)
        st = merge_settings(args, cfg)
        conv = prompts.Convention.from_dict(cfg.get("convention"))
        if args.config or Path(CONFIG_FILE).exists():
            log("INFO", f"설정 파일 적용: {args.config or CONFIG_FILE}")

        # --- 5-1. Git 변경 수집 (status / diff) + 변경 없음 처리 ---
        if args.command == "commit":
            ch = git_collector.collect_for_commit()
        else:
            ch = git_collector.collect_for_pr(base=st["base"])
            log("INFO", f"현재 브랜치: {ch.branch}" + (f" (base: {ch.base})" if ch.base else ""))
        log("INFO", f"Git status 수집 완료: {len(ch.files)}개 파일 변경 감지")
        log("INFO", f"Git diff 수집 완료: {ch.diff_lines}줄")
        for n in ch.notes:
            log("INFO", n)
        if ch.is_empty or not ch.diff.strip():
            what = "커밋 메시지를" if args.command == "commit" else "PR 초안을"
            log("INFO", f"변경 사항이 없습니다. {what} 생성하지 않고 종료합니다.")
            return 0

        # --- 5-2. 안전 모드 적용 (마스킹 · 전송 제한) ---
        diff, rep = safe_mode.apply(ch.diff, enabled=st["safe_mode"], max_files=st["max_files"],
                                    max_lines=st["max_lines"], extra_patterns=cfg.get("mask_patterns"))
        log("INFO", f"안전 모드: {'ON' if rep.enabled else 'OFF'}")
        for line in rep.summary_lines():
            log("INFO", "  " + line)
        if not rep.enabled:
            log("WARN", "안전 모드가 꺼져 있어 diff 원문이 그대로 전송됩니다. 민감정보가 없는지 확인하세요.")

        # --- 5-3. 프롬프트 만들기 (+ --dry-run 이면 여기서 종료) ---
        build = prompts.build_commit_prompt if args.command == "commit" else prompts.build_pr_prompt
        system, user = build(ch, diff, conv, st["lang"], args.context, rep.omitted_note())

        if args.dry_run or args.show_prompt:
            block("System Prompt", system)
            block("User Prompt", user)
            rule()
        if args.dry_run:
            log("INFO", f"--dry-run: API를 호출하지 않았습니다. (AI API 호출 횟수: 0, "
                        f"프롬프트 약 {len(system) + len(user):,}자)")
            return 0

        # --- 5-4. AI API 호출 (정확히 1회) ---
        req = ai_client.AIRequest(
            provider=st["provider"], model=st["model"], system=system, user=user,
            temperature=float(st["temperature"]), max_tokens=int(st["max_tokens"]),
            timeout=float(st["timeout"]), base_url=st["base_url"],
            reasoning_effort=st["reasoning_effort"],
        )
        ai_client.resolve_api_key(req.provider)  # 키 누락은 요청 전에 바로 알림
        log("INFO", f"AI API 요청 중... ({req.provider} / {req.model}, temperature={req.temperature}, "
                    f"max_tokens={req.max_tokens}"
                    + (f", reasoning_effort={req.reasoning_effort}" if req.reasoning_effort else "") + ")")
        resp = ai_client.call(req)
        tok = resp.usage
        what = "커밋 메시지" if args.command == "commit" else "PR 초안"
        think = f" / 생각 {tok['reasoning']}" if tok.get("reasoning") else ""
        log("DONE", f"{what} 생성 완료 ({resp.elapsed:.1f}s, 토큰 입력 {tok.get('input')} / 출력 {tok.get('output')}{think})")
        log("INFO", "AI API 호출 횟수: 1")
        truncated = resp.finish_reason in ("length", "max_tokens", "MAX_TOKENS")
        if truncated:
            log("WARN", "응답이 max_tokens 에서 잘렸습니다. 끊긴 마지막 줄은 버리고 보여줍니다. "
                        "--max-tokens 를 늘리거나 --reasoning-effort none 을 써 보세요.")

        # --- 5-5. 검증 + 후처리 → 결과 메시지 출력 ---
        if args.command == "commit":
            res = postprocess.process_commit(resp.text, recommended=conv.subject_max, truncated=truncated)
            block("Commit Message", res.text)
        else:
            res = postprocess.process_pr(resp.text, truncated=truncated)
            block("PR Title", res.title)
            block("PR Body", res.body)
        rule()

        # --- 5-6. 검증 결과 출력 (✔ / ⚠ / ↺) ---
        print(_c("bold", "[CHECK] 형식 검증"))
        for c in res.checks:
            mark = _c("DONE", "✔") if c.ok else _c("WARN", "⚠")
            print(f"  {mark} {c.name}  {_c('dim', c.detail)}")
        for f in res.fixes:
            print(f"  {_c('INFO', '↺')} 후처리: {f}")

        # --- 5-7. 파일 저장 (-o) ---
        if args.output:
            content = res.text if args.command == "commit" else f"{res.title}\n\n{res.body}"
            Path(args.output).write_text(content + "\n", encoding="utf-8")
            log("DONE", f"저장됨: {args.output}")
            if args.command == "commit":
                log("INFO", f"검토 후 적용: git commit -F {args.output}")
        print(_c("dim", "\n※ AI 초안입니다. 반드시 검토·수정 후 사용하세요."))
        return 0

    # --- 5-8. 예외 처리 — 원인 메시지 출력 후 종료 코드 반환 ---
    except git_collector.GitError as e:
        log("ERROR", str(e))
        return 1
    except ai_client.AIError as e:
        log("ERROR", str(e))
        if e.hint:
            print(f"## {e.hint}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        log("ERROR", "사용자가 중단했습니다.")
        return 130


# ====================================================================
# [6] 프로그램 진입점
# ====================================================================
def main() -> None:
    sys.exit(run())
