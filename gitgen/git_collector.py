"""Git 변경 사항 수집 — git status / git diff 결과를 프로그램 입력으로 변환한다.

미션 제약상 사용하는 Git 명령은 status / diff 계열과, 위치·브랜치 확인용 rev-parse 뿐이다.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


class GitError(Exception):
    """Git 실행/환경 문제."""


def run_git(args: List[str], cwd: Optional[str] = None, ok_codes=(0,)) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        raise GitError("git 명령을 찾을 수 없습니다. Git이 설치되어 있는지 확인하세요.")
    if proc.returncode not in ok_codes:
        msg = proc.stderr.strip() or proc.stdout.strip()
        raise GitError(f"git {' '.join(args)} 실패: {msg}")
    return proc.stdout


@dataclass
class FileChange:
    status: str  # 예: "M", "A", "D", "R", "??"
    path: str

    @property
    def label(self) -> str:
        return {
            "M": "수정", "A": "추가", "D": "삭제", "R": "이름변경",
            "C": "복사", "??": "새 파일(untracked)", "U": "충돌",
        }.get(self.status, self.status)


@dataclass
class Changes:
    branch: str
    mode: str                  # "staged" | "working" | "branch"
    files: List[FileChange]
    diff: str
    stat: str = ""
    base: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    @property
    def diff_lines(self) -> int:
        return len(self.diff.splitlines()) if self.diff else 0

    @property
    def is_empty(self) -> bool:
        return not self.files and not self.diff.strip()


def ensure_repo_root(cwd: Optional[str] = None) -> Path:
    """Git 저장소의 루트에서 실행 중인지 확인한다."""
    try:
        top = run_git(["rev-parse", "--show-toplevel"], cwd=cwd).strip()
    except GitError:
        raise GitError("Git 저장소가 아닙니다. `git init` 된 프로젝트 루트에서 실행하세요.")
    here = Path(cwd or ".").resolve()
    if Path(top).resolve() != here:
        raise GitError(f"프로젝트 루트에서 실행해야 합니다.\n  현재 위치: {here}\n  루트     : {top}")
    return Path(top)


def current_branch(cwd: Optional[str] = None) -> str:
    try:
        name = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd).strip()
    except GitError:
        # 커밋이 하나도 없는 저장소
        name = run_git(["symbolic-ref", "--short", "HEAD"], cwd=cwd, ok_codes=(0, 1, 128)).strip()
    return name or "(unknown)"


def has_commits(cwd: Optional[str] = None) -> bool:
    try:
        run_git(["rev-parse", "--verify", "HEAD"], cwd=cwd)
        return True
    except GitError:
        return False


def parse_status(porcelain: str) -> List[FileChange]:
    """`git status --porcelain=v1` 출력을 파싱한다."""
    files: List[FileChange] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        xy, path = line[:2], line[3:]
        if " -> " in path:          # rename: "old -> new"
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        if xy == "??":
            code = "??"
        else:
            # 스테이징(X) 우선, 없으면 워킹트리(Y)
            code = (xy[0] if xy[0] != " " else xy[1]).strip() or "M"
        files.append(FileChange(code, path))
    return files


def collect_status(cwd: Optional[str] = None) -> List[FileChange]:
    return parse_status(run_git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=cwd))


def _untracked_diff(paths: List[str], cwd: Optional[str] = None) -> str:
    """새 파일은 git diff에 안 잡히므로 `git diff --no-index /dev/null <file>`로 내용을 얻는다."""
    chunks = []
    for p in paths:
        out = run_git(["diff", "--no-index", "--", "/dev/null", p], cwd=cwd, ok_codes=(0, 1))
        chunks.append(out)
    return "".join(chunks)


def collect_for_commit(cwd: Optional[str] = None) -> Changes:
    """커밋용: 스테이징된 변경이 있으면 그것만, 없으면 워킹트리 전체(새 파일 포함)."""
    files = collect_status(cwd)
    branch = current_branch(cwd)
    staged = run_git(["diff", "--cached"], cwd=cwd)
    if staged.strip():
        staged_names = set(run_git(["diff", "--cached", "--name-only"], cwd=cwd).split())
        return Changes(
            branch=branch, mode="staged",
            files=[f for f in files if f.path in staged_names],
            diff=staged,
            stat=run_git(["diff", "--cached", "--stat"], cwd=cwd).strip(),
            notes=["스테이징된(git add) 변경만 사용했습니다."],
        )
    working = run_git(["diff"], cwd=cwd)
    untracked = [f.path for f in files if f.status == "??"]
    if untracked:
        working += _untracked_diff(untracked, cwd)
    return Changes(
        branch=branch, mode="working", files=files, diff=working,
        stat=run_git(["diff", "--stat"], cwd=cwd).strip(),
        notes=["스테이징된 변경이 없어 워킹트리 전체(새 파일 포함)를 사용했습니다."] if files else [],
    )


def detect_base(cwd: Optional[str] = None) -> Optional[str]:
    for cand in ("main", "master", "origin/main", "origin/master", "develop"):
        try:
            run_git(["rev-parse", "--verify", "--quiet", cand], cwd=cwd)
            return cand
        except GitError:
            continue
    return None


def collect_for_pr(base: Optional[str] = None, cwd: Optional[str] = None) -> Changes:
    """PR용: base...HEAD 커밋 차이 + 아직 커밋 안 한 변경까지 합친다."""
    branch = current_branch(cwd)
    base = base or detect_base(cwd)
    notes: List[str] = []
    diff, stat = "", ""
    files: List[FileChange] = []

    if base and has_commits(cwd) and base != branch:
        try:
            diff = run_git(["diff", f"{base}...HEAD"], cwd=cwd)
            stat = run_git(["diff", "--stat", f"{base}...HEAD"], cwd=cwd).strip()
            names = run_git(["diff", "--name-status", f"{base}...HEAD"], cwd=cwd)
            for line in names.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    files.append(FileChange(parts[0][0], parts[-1]))
            notes.append(f"`{base}...HEAD` 커밋 차이를 사용했습니다.")
        except GitError as e:
            notes.append(f"base 비교 실패({e}); 워킹트리 변경만 사용합니다.")
    elif base == branch:
        notes.append(f"현재 브랜치가 base({base})와 같아 워킹트리 변경만 사용합니다. "
                     "PR은 보통 feature 브랜치에서 만듭니다.")
    else:
        notes.append("base 브랜치를 찾지 못해 워킹트리 변경만 사용합니다. (--base 로 지정 가능)")

    # 커밋 안 된 변경도 포함
    local = collect_for_commit(cwd)
    if local.diff.strip():
        diff += local.diff
        known = {f.path for f in files}
        files += [f for f in local.files if f.path not in known]
        if stat:
            notes.append("아직 커밋하지 않은 변경도 함께 포함했습니다.")
        else:
            stat = local.stat

    return Changes(branch=branch, mode="branch", files=files, diff=diff, stat=stat, base=base, notes=notes)
