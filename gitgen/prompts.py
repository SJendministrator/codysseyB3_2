"""프롬프트 설계 — 결과 품질은 '무엇을 어떤 형식으로 주느냐'에서 결정된다.

구성 원칙
  1. system : 역할 + 출력 형식 + 금지 규칙 (매번 동일, 형식을 강제하는 부분)
  2. user   : 이번 변경의 컨텍스트 (브랜치, 파일 목록, diff 통계, 변경 이유, diff 본문)
  3. 출력 형식은 '파싱 가능한 고정 구조'로 요구 → 후처리/검증이 쉬워진다
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .git_collector import Changes

# ====================================================================
# [1] 기본 커밋 type 목록
# ====================================================================
DEFAULT_TYPES = ["feat", "fix", "docs", "refactor", "test", "chore", "style", "perf", "build", "ci"]


# ====================================================================
# [2] 팀 컨벤션 — .ai-gitgen.json 의 "convention" 으로 덮어쓰기
# ====================================================================
@dataclass
class Convention:
    """팀 컨벤션 — .ai-gitgen.json 의 "convention" 으로 덮어쓸 수 있다."""
    commit_types: List[str] = field(default_factory=lambda: list(DEFAULT_TYPES))
    scopes: List[str] = field(default_factory=list)
    require_scope: bool = False
    subject_max: int = 50
    include_body: bool = True
    extra_rules: List[str] = field(default_factory=list)
    pr_title_prefix: bool = True
    pr_extra_rules: List[str] = field(default_factory=list)
    pr_checklist: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Convention":
        c = cls()
        for k, v in (d or {}).items():
            if hasattr(c, k):
                setattr(c, k, v)
        return c


# ====================================================================
# [3] 언어 규칙 (ko / en)
# ====================================================================
LANG_RULE = {
    "ko": "모든 문장은 한국어로 작성한다. type/scope 키워드와 코드·파일 이름은 영어 그대로 둔다. "
          "제목은 '~추가', '~수정'처럼 명사형으로 끝낸다.",
    "en": "Write in English. Use the imperative mood in the subject (e.g. 'Add', 'Fix').",
}

# diff 읽는 법 — 커밋/PR 공통. 실제 실행에서 생긴 오해(문맥 줄·주석을 기능 추가로 읽음)를 막는다.
DIFF_READING_RULES = """- diff에서 '+'로 시작하는 줄은 추가, '-'로 시작하는 줄은 삭제된 줄이다. 앞이 공백인 줄은 바뀌지 않은 문맥이므로 변경 내용으로 쓰지 않는다.
- 추가/삭제된 줄이 주석·docstring·README 같은 문서뿐이면 기능 변경으로 쓰지 않는다. 이때 type은 docs(코드 주석 정리는 style도 가능)를 쓴다.
- 주석 안에 적힌 기능 이름(예: '# Anthropic API 형식')을 보고 그 기능을 '추가했다'고 쓰지 않는다.
- [생략된 파일] 목록에 있는 파일은 내용을 보지 못한 것이다. 파일 이름과 diff 통계로 알 수 있는 사실만 쓰고, 내용을 추측하지 않는다."""


# ====================================================================
# [4] 커밋 메시지 system 프롬프트 — 역할 · 출력 형식 · 금지 규칙
# ====================================================================
def _commit_system(conv: Convention, lang: str) -> str:
    scope_rule = (
        f"- scope는 반드시 다음 중 하나를 쓴다: {', '.join(conv.scopes)}" if conv.scopes and conv.require_scope
        else f"- scope는 선택. 쓴다면 다음 중에서 고른다: {', '.join(conv.scopes)}" if conv.scopes
        else "- scope는 선택. 변경된 모듈/디렉터리 이름이 분명할 때만 쓴다."
    )
    body_rule = (
        "3. 빈 줄 1개\n"
        "4. 본문: '- '로 시작하는 불릿 2~4개. 핵심 변경을 요약하고, 변경된 파일/모듈 이름을 1~3개 언급한다.\n"
        "   각 불릿은 무엇을 + 왜 를 한 줄로. 72자 안팎으로."
        if conv.include_body else
        "3. 본문은 쓰지 않는다 (제목 1줄만)."
    )
    extra = "".join(f"\n- {r}" for r in conv.extra_rules)
    return f"""당신은 코드 리뷰 경험이 많은 시니어 개발자이며, git diff를 읽고 커밋 메시지를 작성한다.

[출력 형식] — 이 형식 외의 어떤 텍스트도 출력하지 않는다.
1. 첫 줄: <type>(<scope>): <subject>   또는 scope 없이 <type>: <subject>
2. 제목은 {conv.subject_max}자 이내 (절대 72자를 넘기지 않는다), 끝에 마침표 없음
{body_rule}

[규칙]
- type은 다음 중 하나: {', '.join(conv.commit_types)}
{scope_rule}
- diff에 실제로 있는 변경만 쓴다. 추측하거나 없는 기능을 지어내지 않는다.
{DIFF_READING_RULES}
- 여러 변경이 섞여 있으면 가장 중요한 변경을 제목으로, 나머지는 본문 불릿으로.
- [MASKED:...] 로 가려진 값은 민감정보이므로 언급하지 않는다.
- 코드 블록(```), 따옴표, "커밋 메시지:" 같은 머리말을 붙이지 않는다.
- {LANG_RULE.get(lang, LANG_RULE['ko'])}{extra}
"""


# ====================================================================
# [5] PR system 프롬프트 — TITLE + Why / What / How to Test 템플릿
# ====================================================================
def _pr_system(conv: Convention, lang: str) -> str:
    prefix = ("제목은 커밋과 같은 '<type>: <요약>' 형식으로 쓴다." if conv.pr_title_prefix
              else "제목은 type 접두어 없이 자연스러운 한 문장 요약으로 쓴다.")
    checklist = ""
    if conv.pr_checklist:
        items = "\n".join(f"- [ ] {c}" for c in conv.pr_checklist)
        checklist = f"\n## Checklist\n{items}"
    extra = "".join(f"\n- {r}" for r in conv.pr_extra_rules)
    return f"""당신은 동료가 리뷰하기 쉬운 Pull Request 설명을 쓰는 시니어 개발자다.

[출력 형식] — 정확히 아래 구조로만 출력한다. 다른 머리말/꼬리말 금지.
TITLE: <PR 제목 1줄, 80자 이내>

## Why
- <이 변경이 필요한 배경/문제, 1~3개>

## What
- <핵심 변경 사항, 파일/모듈 단위로 2~5개>

## How to Test
- <리뷰어가 그대로 따라 할 수 있는 확인 방법, 명령어는 `백틱`으로, 1~4개>{checklist}

[규칙]
- 각 섹션은 반드시 '- '로 시작하는 불릿을 1개 이상 가진다.
- {prefix}
- diff와 사용자가 준 변경 이유에 근거해서만 쓴다. 변경 이유가 없으면 diff에서 합리적으로 추론하되 과장하지 않는다.
{DIFF_READING_RULES}
- How to Test는 실제로 실행 가능한 단계여야 한다 (예: 실행 명령, 확인할 출력).
- [MASKED:...] 로 가려진 값은 언급하지 않는다.
- 코드 블록(```)으로 전체를 감싸지 않는다.
- {LANG_RULE.get(lang, LANG_RULE['ko'])}{extra}
"""


# ====================================================================
# [6] user 프롬프트 — 이번 변경의 컨텍스트 (브랜치 · 파일 · 통계 · 변경 이유 · diff)
# ====================================================================
def _context_block(ch: Changes, diff: str, context: Optional[str], omitted: Optional[str] = None) -> str:
    files = "\n".join(f"- [{f.label}] {f.path}" for f in ch.files[:50]) or "- (없음)"
    if len(ch.files) > 50:
        files += f"\n- ... 외 {len(ch.files) - 50}개"
    parts = [
        f"브랜치: {ch.branch}" + (f" (base: {ch.base})" if ch.base else ""),
        f"변경 수집 방식: {ch.mode}",
        f"\n[변경 파일 목록] ({len(ch.files)}개)\n{files}",
    ]
    if ch.stat:
        parts.append(f"\n[diff 통계]\n{ch.stat}")
    if context:
        parts.append(f"\n[작성자가 알려준 변경 이유/요구사항]\n{context}")
    if omitted:
        parts.append(f"\n[생략된 파일] — 안전 모드 전송량 제한으로 아래 diff는 일부 또는 전부 빠져 있음\n{omitted}")
    parts.append(f"\n[diff]\n{diff}")
    return "\n".join(parts)


# ====================================================================
# [7] 프롬프트 생성 함수 — cli.py 에서 호출 (system, user) 반환
# ====================================================================
def build_commit_prompt(ch: Changes, diff: str, conv: Convention, lang: str = "ko",
                        context: Optional[str] = None, omitted: Optional[str] = None):
    user = "아래 변경 사항으로 커밋 메시지를 작성해 줘.\n\n" + _context_block(ch, diff, context, omitted)
    return _commit_system(conv, lang), user


def build_pr_prompt(ch: Changes, diff: str, conv: Convention, lang: str = "ko",
                    context: Optional[str] = None, omitted: Optional[str] = None):
    user = "아래 변경 사항으로 Pull Request 제목과 본문을 작성해 줘.\n\n" + _context_block(ch, diff, context, omitted)
    return _pr_system(conv, lang), user
