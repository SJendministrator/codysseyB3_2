"""안전 모드 — diff를 AI에 보내기 전에 민감정보를 가리고 전송량을 제한한다.

정책 (기본값)
  (A) 마스킹 : API Key/토큰/비밀번호 할당/개인키/이메일/전화번호/주민번호 패턴 → [MASKED:종류]
  (B) 제한   : 최대 10개 파일, 최대 200줄 (CLI --max-files / --max-lines 로 조정)
               줄 예산은 파일마다 공평하게 나눈다 → 앞쪽 파일이 예산을 독차지하지 않음
  (+) 제외   : .env, *.pem, *.key, id_rsa 같은 민감 파일과 lock 파일은 내용 자체를 보내지 않음
"""
from __future__ import annotations

import fnmatch
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ====================================================================
# [1] 마스킹 패턴 — 민감정보 정규식 목록
#     (이름, 정규식, 치환) — 위에서부터 순서대로 적용 (구체적인 패턴이 먼저)
# ====================================================================
# 비밀값을 담을 법한 '키 이름' + 할당 기호(= 또는 :)
_SECRET_KEY = (r"(?i)(\b[\w.\-]*(?:password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key"
               r"|private[_\-]?key|client[_\-]?secret)[\w.\-]*\b[\"']?\s*[:=]\s*)")
# 값으로 보면 안 되는 것: 이미 가린 값, 숫자/불리언, 환경변수 참조
_NOT_SECRET = r"\[MASKED|(?:\d+|true|false|null|none|nil)\b|os\.|process\.env|getenv|environ|\$\{?[A-Z_]"

_RAW_PATTERNS: List[Tuple[str, str, Optional[str]]] = [
    ("PRIVATE_KEY", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----", None),
    ("ANTHROPIC_KEY", r"sk-ant-[A-Za-z0-9_\-]{20,}", None),
    ("OPENAI_KEY", r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}", None),
    ("GOOGLE_API_KEY", r"AIza[0-9A-Za-z_\-]{35}", None),
    ("AWS_ACCESS_KEY", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", None),
    ("GITHUB_TOKEN", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b", None),
    ("SLACK_TOKEN", r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b", None),
    ("JWT", r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b", None),
    # --- 비밀값 할당: 키 이름은 남기고 값만 가림 ---
    # (a) 따옴표로 감싼 값 → 문자열 리터럴이므로 가림:  password = "hunter2"
    ("SECRET_ASSIGN", _SECRET_KEY + r"([\"'])(?!" + _NOT_SECRET + r")([^\"'\n]{4,}?)\2",
     r"\1\2[MASKED:SECRET]\2"),
    # (b) 따옴표 없는 값 → 코드 표현식(req.x / int(...) / cfg[...])이면 비밀값이 아니므로 건너뜀
    ("SECRET_ASSIGN", _SECRET_KEY + r"(?![\"'])(?!" + _NOT_SECRET + r"|[A-Za-z_]\w*(?:\.|\(|\[))([^\s\"',;]{4,})",
     r"\1[MASKED:SECRET]"),
    ("EMAIL", r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b", None),
    ("KR_RRN", r"\b\d{6}-[1-4]\d{6}\b", None),
    ("PHONE", r"\b01[016789]-?\d{3,4}-?\d{4}\b", None),
]
PATTERNS = [(n, re.compile(p), r) for n, p, r in _RAW_PATTERNS]

# ====================================================================
# [2] 내용을 보내지 않을 파일 / 안전 모드 OFF 하드 리밋
# ====================================================================
SENSITIVE_FILES = [".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
                   "*credentials*", "*secret*", "*.keystore", "*.jks"]
NOISE_FILES = ["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock",
               "uv.lock", "Cargo.lock", "go.sum", "*.min.js", "*.min.css"]

# 안전 모드를 꺼도 비용 폭주를 막는 하드 리밋
HARD_MAX_LINES = 2000

# 파일 하나에 배정할 최소 줄 수 (diff 헤더 몇 줄 + 생략 표시) — 이보다 적으면 통째로 생략
MIN_LINES_PER_FILE = 6


# ====================================================================
# [3] 안전 모드 결과 리포트 — 무엇을 가리고, 무엇을 왜 줄였는지
# ====================================================================
@dataclass
class SafeReport:
    enabled: bool
    max_files: int = 10
    max_lines: int = 200
    masked: Counter = field(default_factory=Counter)
    excluded_files: List[str] = field(default_factory=list)      # 민감/lock 파일 → 내용 제외
    dropped_by_count: List[str] = field(default_factory=list)    # 파일 수 제한 → 통째로 생략
    dropped_by_lines: List[str] = field(default_factory=list)    # 줄 예산 부족 → 통째로 생략
    truncated: Dict[str, int] = field(default_factory=dict)      # 파일별 '생략한 줄 수'
    truncated_lines: int = 0                                     # 안전 모드 OFF 하드 리밋용
    lines_before: int = 0
    lines_after: int = 0
    files_sent: int = 0

    @property
    def dropped_files(self) -> List[str]:
        return self.dropped_by_count + self.dropped_by_lines

    # --- 3-1. 터미널 로그용 요약 ---
    def summary_lines(self) -> List[str]:
        out = [f"전송 diff: {self.lines_before}줄 → {self.lines_after}줄, 파일 {self.files_sent}개"]
        if self.masked:
            out.append("마스킹: " + ", ".join(f"{k} {v}건" for k, v in self.masked.items()))
        if self.excluded_files:
            out.append("내용 제외(민감/lock 파일): " + ", ".join(self.excluded_files))
        if self.dropped_by_count:
            out.append(f"파일 수 제한({self.max_files}개)으로 생략: {_names(self.dropped_by_count)}")
        if self.dropped_by_lines:
            out.append(f"줄 수 제한({self.max_lines}줄)으로 내용 전체 생략: {_names(self.dropped_by_lines)}")
        if self.truncated:
            items = ", ".join(f"{p}(-{n}줄)" for p, n in list(self.truncated.items())[:5])
            more = " ..." if len(self.truncated) > 5 else ""
            out.append(f"줄 수 제한({self.max_lines}줄)으로 일부만 전송: {items}{more}")
        if self.truncated_lines:
            out.append(f"하드 리밋으로 생략: {self.truncated_lines}줄")
        return out

    # --- 3-2. AI에게 알려줄 '생략 안내' (내용을 못 본 파일을 추측하지 않게) ---
    def omitted_note(self) -> Optional[str]:
        lines = []
        for p in self.excluded_files:
            lines.append(f"- {p}: 민감/자동생성 파일이라 내용 미전송")
        for p in self.dropped_files:
            lines.append(f"- {p}: 전송량 제한으로 diff 전체 생략")
        for p, n in self.truncated.items():
            lines.append(f"- {p}: 앞부분만 전송, {n}줄 생략")
        return "\n".join(lines) if lines else None


def _names(paths: List[str], limit: int = 5) -> str:
    return f"{len(paths)}개 ({', '.join(paths[:limit])}" + (" ..." if len(paths) > limit else "") + ")"


# ====================================================================
# [4] 마스킹 함수 — 패턴에 걸린 값을 [MASKED:종류] 로 치환
# ====================================================================
def mask_text(text: str, counter: Optional[Counter] = None, patterns=None) -> str:
    counter = counter if counter is not None else Counter()
    for name, rx, repl in (patterns or PATTERNS):
        def _sub(m, name=name, repl=repl):
            counter[name] += 1
            return m.expand(repl) if repl else f"[MASKED:{name}]"
        text = rx.sub(_sub, text)
    return text


# ====================================================================
# [5] diff 를 파일 단위로 나누기 + 파일 이름 매칭
# ====================================================================
def split_diff_by_file(diff: str) -> List[Tuple[str, str]]:
    """'diff --git a/x b/x' 단위로 쪼갠다. 반환: [(경로, 덩어리)]"""
    parts: List[Tuple[str, str]] = []
    cur_path, cur_lines = None, []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if cur_lines:
                parts.append((cur_path or "?", "".join(cur_lines)))
            m = re.match(r"diff --git (?:\"?a/)?(.+?)\"? (?:\"?b/)?(.+?)\"?\s*$", line)
            cur_path = m.group(2) if m else "?"
            if cur_path.startswith("dev/null"):
                cur_path = m.group(1)
            cur_lines = [line]
        else:
            cur_lines.append(line)
    if cur_lines:
        parts.append((cur_path or "?", "".join(cur_lines)))
    return parts


def _matches(path: str, patterns: List[str]) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(path, p) for p in patterns)


# ====================================================================
# [6] 줄 예산 공평 분배 — 작은 파일은 통째로, 큰 파일은 남은 예산을 나눠 가짐
# ====================================================================
def fair_share(sizes: List[int], budget: int) -> List[int]:
    """작은 파일부터 '남은 예산 ÷ 남은 파일 수' 만큼씩 배정한다.
       예) sizes=[98, 30, 20], budget=100  →  [50, 30, 20]
           (20줄·30줄 파일은 통째로, 98줄 파일은 남은 50줄)
       예) sizes=[500, 500, 500], budget=200  →  [66, 67, 67]"""
    alloc = [0] * len(sizes)
    left = max(0, budget)
    order = sorted(range(len(sizes)), key=lambda i: sizes[i])
    for k, i in enumerate(order):
        share = left // (len(sizes) - k)
        alloc[i] = min(sizes[i], share)
        left -= alloc[i]
    return alloc


# ====================================================================
# [7] 안전 정책 적용 — 이 모듈의 메인 함수
# ====================================================================
def apply(diff: str, enabled: bool = True, max_files: int = 10, max_lines: int = 200,
          extra_patterns: Optional[Dict[str, str]] = None) -> Tuple[str, SafeReport]:
    """diff에 안전 정책을 적용하고 (가공된 diff, 리포트)를 돌려준다."""
    report = SafeReport(enabled=enabled, max_files=max_files, max_lines=max_lines,
                        lines_before=len(diff.splitlines()))
    patterns = list(PATTERNS)
    for n, p in (extra_patterns or {}).items():   # 설정 파일에서 추가한 사용자 정의 패턴
        patterns.append((n, re.compile(p), None))

    # --- 7-1. 안전 모드 OFF: 원문 그대로 + 하드 리밋만 ---
    if not enabled:
        lines = diff.splitlines(keepends=True)
        if len(lines) > HARD_MAX_LINES:
            report.truncated_lines = len(lines) - HARD_MAX_LINES
            diff = "".join(lines[:HARD_MAX_LINES - 1]) + f"... ({report.truncated_lines}줄 생략: 하드 리밋)\n"
        report.lines_after = len(diff.splitlines())
        report.files_sent = len(split_diff_by_file(diff))
        return diff, report

    # --- 7-2. 1차 분류: 민감/자동생성 파일 제외 → 파일 수 제한 → 마스킹 ---
    blocks: List[Tuple[str, Optional[List[str]]]] = []   # (경로, 줄 목록) / 줄 목록 None = 제외 안내
    for path, chunk in split_diff_by_file(diff):
        if _matches(path, SENSITIVE_FILES) or _matches(path, NOISE_FILES):
            report.excluded_files.append(path)
            blocks.append((path, None))
        elif sum(1 for _, b in blocks if b is not None) >= max_files:
            report.dropped_by_count.append(path)
        else:
            blocks.append((path, mask_text(chunk, report.masked, patterns).splitlines(keepends=True)))

    # --- 7-3. 줄 예산 계산: 제외 안내(파일당 2줄)를 먼저 빼고 나머지를 공평 분배 ---
    budget = max_lines - 2 * len(report.excluded_files)
    sendable = [b for _, b in blocks if b is not None]
    alloc = iter(fair_share([len(b) for b in sendable], budget))

    # --- 7-4. 배정량만큼 자르기 (생략 표시 줄도 배정량 안에 포함 → 한도 초과 없음) ---
    out: List[str] = []
    for path, lines in blocks:
        if lines is None:
            out.append(f"diff --git a/{path} b/{path}\n[safe-mode] 민감/자동생성 파일이라 내용을 보내지 않음\n")
            continue
        n = next(alloc)
        if len(lines) <= n:
            out.append("".join(lines))
        elif n >= MIN_LINES_PER_FILE:
            omitted = len(lines) - (n - 1)
            report.truncated[path] = omitted
            out.append("".join(lines[:n - 1]) + f"... ({path}: 이하 {omitted}줄 생략)\n")
        else:
            report.dropped_by_lines.append(path)
            continue
        report.files_sent += 1

    # --- 7-5. 결과 정리 ---
    result = "".join(out)
    report.lines_after = len(result.splitlines())
    return result, report
