"""출력 검증 및 다듬기 — AI 결과를 실무 규칙에 맞게 '후처리'한다 (재호출 없음 → 비용 1회 고정).

규칙
  커밋 제목 : 50자 권장, 72자 초과 시 잘라냄 / 마침표·따옴표·머리말 제거
  커밋 본문 : 불릿('- ')으로 정규화
  PR 제목   : 80자 초과 시 잘라냄
  PR 본문   : ## Why / ## What / ## How to Test 헤더 필수, 섹션마다 불릿 1개 이상 (없으면 자리표시 불릿 + 경고)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

COMMIT_SUBJECT_RECOMMENDED = 50
COMMIT_SUBJECT_MAX = 72
PR_TITLE_MAX = 80
PR_SECTIONS = ["Why", "What", "How to Test"]

_BULLET_RX = re.compile(r"^\s*(?:[-*•·▪]|\d+[.)])\s+")
_LABEL_RX = re.compile(r"^\s*(?:\*\*)?(?:commit(?: message)?|커밋(?: 메시지)?|제목|subject|title|pr title)(?:\*\*)?\s*[:：]\s*",
                       re.I)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Result:
    title: str
    body: str
    checks: List[Check] = field(default_factory=list)
    fixes: List[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.title + ("\n\n" + self.body if self.body else "")


def strip_wrapping(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^```[\w-]*\s*\n?", "", t)
    t = re.sub(r"\n?```\s*$", "", t)
    return t.strip()


def truncate(s: str, limit: int) -> str:
    """limit 이내로 자르되, 가능하면 단어 경계에서 자른다."""
    if len(s) <= limit:
        return s
    cut = s[:limit]
    sp = cut.rfind(" ")
    if sp >= limit * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,.;:-")


def _clean_title(line: str) -> str:
    t = _LABEL_RX.sub("", line.strip())
    t = t.strip().strip("`\"'“”‘’").strip()
    t = re.sub(r"^#+\s*", "", t)
    return t.rstrip("。.").strip()


def _to_bullet(line: str) -> str:
    return "- " + _BULLET_RX.sub("", line).strip()


def process_commit(raw: str, recommended: int = COMMIT_SUBJECT_RECOMMENDED,
                   max_len: int = COMMIT_SUBJECT_MAX) -> Result:
    text = strip_wrapping(raw)
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return Result("", "", [Check("제목 존재", False, "AI 응답에서 제목을 찾지 못했습니다")])

    fixes: List[str] = []
    orig_subject = lines[0]
    subject = _clean_title(orig_subject)
    if subject != orig_subject.strip():
        fixes.append("제목의 머리말/따옴표/마침표 제거")
    if len(subject) > max_len:
        subject = truncate(subject, max_len)
        fixes.append(f"제목을 {max_len}자 이내로 잘라냄")

    body_lines: List[str] = []
    for ln in lines[1:]:
        s = ln.rstrip()
        if not s.strip():
            continue
        if s.lstrip().startswith("#"):        # 마크다운 헤더는 커밋 본문에 어울리지 않음
            continue
        if not s.lstrip().startswith("- "):
            fixes.append("본문을 '- ' 불릿으로 정규화")
        body_lines.append(_to_bullet(s))
    body = "\n".join(body_lines)

    conventional = re.match(r"^[a-z]+(\([\w./\-]+\))?!?: \S", subject) is not None
    checks = [
        Check("제목 1줄 존재", bool(subject), subject[:30] + ("…" if len(subject) > 30 else "")),
        Check(f"제목 길이 ≤{recommended}자 (권장)", len(subject) <= recommended, f"{len(subject)}자"),
        Check(f"제목 길이 ≤{max_len}자 (최대)", len(subject) <= max_len, f"{len(subject)}자"),
        Check("type: 형식 (Conventional Commits)", conventional, "예) feat: ..., fix(cli): ..."),
    ]
    if body:
        checks.append(Check("본문 불릿 요약", len(body_lines) >= 1, f"불릿 {len(body_lines)}개"))
    return Result(subject, body, checks, sorted(set(fixes), key=fixes.index))


def _section_key(header: str) -> Optional[str]:
    h = header.lower()
    if "how" in h and "test" in h or "테스트" in h:
        return "How to Test"
    if re.search(r"\bwhy\b", h) or "배경" in h:
        return "Why"
    if re.search(r"\bwhat\b", h) or "변경 사항" in h or "변경사항" in h:
        return "What"
    return None


def process_pr(raw: str, max_title: int = PR_TITLE_MAX) -> Result:
    text = strip_wrapping(raw)
    lines = text.splitlines()
    fixes: List[str] = []

    # 1) 제목 찾기: "TITLE:" 줄 → 없으면 첫 번째 비헤더 줄
    title, title_idx = "", None
    for i, ln in enumerate(lines):
        if re.match(r"^\s*(?:\*\*)?(?:title|pr title|제목)(?:\*\*)?\s*[:：]", ln, re.I):
            title, title_idx = _clean_title(ln), i
            break
    if title_idx is None:
        for i, ln in enumerate(lines):
            if ln.strip() and not re.match(r"^\s*#{2,}", ln):
                title, title_idx = _clean_title(ln), i
                fixes.append("TITLE: 표시가 없어 첫 줄을 제목으로 사용")
                break
    if len(title) > max_title:
        title = truncate(title, max_title)
        fixes.append(f"PR 제목을 {max_title}자 이내로 잘라냄")

    # 2) 섹션 파싱
    sections: Dict[str, List[str]] = {k: [] for k in PR_SECTIONS}
    extras: List[Tuple[str, List[str]]] = []
    current: Optional[List[str]] = None
    for i, ln in enumerate(lines):
        if i == title_idx:
            continue
        m = re.match(r"^\s*#{1,6}\s*(.+?)\s*$", ln)
        if m:
            key = _section_key(m.group(1))
            if key:
                current = sections[key]
            else:
                extras.append((m.group(1).strip(), []))
                current = extras[-1][1]
            continue
        if current is None or not ln.strip():
            continue
        s = ln.rstrip()
        if re.match(r"^\s*- \[[ xX]\]", s):      # 체크리스트는 그대로
            current.append(s.strip())
        elif _BULLET_RX.match(s) or s.lstrip().startswith("- "):
            current.append(_to_bullet(s) if not s.lstrip().startswith("- ") else s.strip())
        else:
            current.append(_to_bullet(s))
            fixes.append("섹션 문장을 불릿으로 변환")

    checks = [Check("PR 제목 1줄 존재", bool(title), title[:30] + ("…" if len(title) > 30 else "")),
              Check(f"PR 제목 ≤{max_title}자", len(title) <= max_title, f"{len(title)}자")]
    out: List[str] = []
    for key in PR_SECTIONS:
        items = sections[key]
        ok = len(items) >= 1
        if not ok:
            items = ["- (작성 필요: AI가 이 섹션을 생성하지 못했습니다)"]
            fixes.append(f"'{key}' 섹션이 비어 자리표시 불릿 추가")
        checks.append(Check(f"## {key} 섹션 + 불릿 ≥1", ok, f"불릿 {len(sections[key])}개"))
        out.append(f"## {key}\n" + "\n".join(items))
    for name, items in extras:
        if items:
            out.append(f"## {name}\n" + "\n".join(items))

    return Result(title, "\n\n".join(out), checks, sorted(set(fixes), key=fixes.index))
