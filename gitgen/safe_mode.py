"""안전 모드 — diff를 AI에 보내기 전에 민감정보를 가리고 전송량을 제한한다.

정책 (기본값)
  (A) 마스킹 : API Key/토큰/비밀번호 할당/개인키/이메일/전화번호/주민번호 패턴 → [MASKED:종류]
  (B) 제한   : 최대 10개 파일, 최대 200줄 (CLI --max-files / --max-lines 로 조정)
  (+) 제외   : .env, *.pem, *.key, id_rsa 같은 민감 파일과 lock 파일은 내용 자체를 보내지 않음
"""
from __future__ import annotations

import fnmatch
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# (이름, 정규식, 치환) — 위에서부터 순서대로 적용 (구체적인 패턴이 먼저)
_RAW_PATTERNS: List[Tuple[str, str, Optional[str]]] = [
    ("PRIVATE_KEY", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----", None),
    ("ANTHROPIC_KEY", r"sk-ant-[A-Za-z0-9_\-]{20,}", None),
    ("OPENAI_KEY", r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}", None),
    ("GOOGLE_API_KEY", r"AIza[0-9A-Za-z_\-]{35}", None),
    ("AWS_ACCESS_KEY", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", None),
    ("GITHUB_TOKEN", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b", None),
    ("SLACK_TOKEN", r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b", None),
    ("JWT", r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b", None),
    # password = "..." / api_key: xxx 같은 할당 → 키 이름은 남기고 값만 가림
    ("SECRET_ASSIGN",
     r"(?i)(\b[\w.\-]*(?:password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key|private[_\-]?key|client[_\-]?secret)[\w.\-]*\b"
     r"[\"']?\s*[:=]\s*)([\"']?)(?!\[MASKED|(?:\d+|true|false|null|none|nil)\b|os\.|process\.env|getenv|environ|\$\{?[A-Z_])([^\s\"',;]{4,})\2",
     r"\1\2[MASKED:SECRET]\2"),
    ("EMAIL", r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b", None),
    ("KR_RRN", r"\b\d{6}-[1-4]\d{6}\b", None),
    ("PHONE", r"\b01[016789]-?\d{3,4}-?\d{4}\b", None),
]
PATTERNS = [(n, re.compile(p), r) for n, p, r in _RAW_PATTERNS]

SENSITIVE_FILES = [".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
                   "*credentials*", "*secret*", "*.keystore", "*.jks"]
NOISE_FILES = ["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock",
               "uv.lock", "Cargo.lock", "go.sum", "*.min.js", "*.min.css"]

# 안전 모드를 꺼도 비용 폭주를 막는 하드 리밋
HARD_MAX_LINES = 2000


@dataclass
class SafeReport:
    enabled: bool
    masked: Counter = field(default_factory=Counter)
    excluded_files: List[str] = field(default_factory=list)
    dropped_files: List[str] = field(default_factory=list)
    truncated_lines: int = 0
    lines_before: int = 0
    lines_after: int = 0
    files_sent: int = 0

    def summary_lines(self) -> List[str]:
        out = [f"전송 diff: {self.lines_before}줄 → {self.lines_after}줄, 파일 {self.files_sent}개"]
        if self.masked:
            out.append("마스킹: " + ", ".join(f"{k} {v}건" for k, v in self.masked.items()))
        if self.excluded_files:
            out.append("내용 제외(민감/lock 파일): " + ", ".join(self.excluded_files))
        if self.dropped_files:
            out.append(f"파일 수 제한으로 생략: {len(self.dropped_files)}개 ({', '.join(self.dropped_files[:5])}"
                       + (" ..." if len(self.dropped_files) > 5 else "") + ")")
        if self.truncated_lines:
            out.append(f"줄 수 제한으로 생략: {self.truncated_lines}줄")
        return out


def mask_text(text: str, counter: Optional[Counter] = None, patterns=None) -> str:
    counter = counter if counter is not None else Counter()
    for name, rx, repl in (patterns or PATTERNS):
        def _sub(m, name=name, repl=repl):
            counter[name] += 1
            return m.expand(repl) if repl else f"[MASKED:{name}]"
        text = rx.sub(_sub, text)
    return text


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


def apply(diff: str, enabled: bool = True, max_files: int = 10, max_lines: int = 200,
          extra_patterns: Optional[Dict[str, str]] = None) -> Tuple[str, SafeReport]:
    """diff에 안전 정책을 적용하고 (가공된 diff, 리포트)를 돌려준다."""
    report = SafeReport(enabled=enabled, lines_before=len(diff.splitlines()))
    patterns = list(PATTERNS)
    for n, p in (extra_patterns or {}).items():   # 설정 파일에서 추가한 사용자 정의 패턴
        patterns.append((n, re.compile(p), None))

    if not enabled:
        lines = diff.splitlines(keepends=True)
        if len(lines) > HARD_MAX_LINES:
            report.truncated_lines = len(lines) - HARD_MAX_LINES
            diff = "".join(lines[:HARD_MAX_LINES]) + f"\n... ({report.truncated_lines}줄 생략: 하드 리밋)\n"
        report.lines_after = len(diff.splitlines())
        report.files_sent = len(split_diff_by_file(diff))
        return diff, report

    out: List[str] = []
    budget = max_lines
    for path, chunk in split_diff_by_file(diff):
        if _matches(path, SENSITIVE_FILES) or _matches(path, NOISE_FILES):
            report.excluded_files.append(path)
            out.append(f"diff --git a/{path} b/{path}\n[safe-mode] 민감/자동생성 파일이라 내용을 보내지 않음\n")
            continue
        if report.files_sent >= max_files:
            report.dropped_files.append(path)
            continue
        if budget <= 0:
            report.dropped_files.append(path)
            continue
        lines = mask_text(chunk, report.masked, patterns).splitlines(keepends=True)
        if len(lines) > budget:
            report.truncated_lines += len(lines) - budget
            lines = lines[:budget] + [f"... ({path}: 이하 생략)\n"]
        budget -= len(lines)
        out.append("".join(lines))
        report.files_sent += 1

    result = "".join(out)
    report.lines_after = len(result.splitlines())
    return result, report
