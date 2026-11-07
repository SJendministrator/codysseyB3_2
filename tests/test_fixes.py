"""실제 실행(2026-11-07)에서 발견한 문제 4가지의 회귀 테스트."""
import shutil
import unittest

from gitgen import postprocess, safe_mode
from tests.helpers import FakeAI, make_repo, run_cli

KEY = {"AI_API_KEY": "test-key-123"}


def _file(path, n):
    return f"diff --git a/{path} b/{path}\n" + "".join(f"+line {i}\n" for i in range(n - 1))


class FairBudgetTest(unittest.TestCase):
    """B-2/B-4: 앞쪽 파일이 줄 예산을 독차지하지 않고, 한도를 1줄도 넘지 않는다."""

    def test_every_file_gets_lines_and_limit_is_strict(self):
        sizes = {"README.md": 98, "gitgen/ai_client.py": 30, "gitgen/cli.py": 37, "gitgen/git_collector.py": 25,
                 "gitgen/postprocess.py": 28, "gitgen/prompts.py": 23, "gitgen/safe_mode.py": 24}
        diff = "".join(_file(p, n) for p, n in sizes.items())
        out, rep = safe_mode.apply(diff, max_lines=200)
        self.assertEqual(rep.files_sent, 7)                     # 예전: 2개
        self.assertEqual(rep.dropped_files, [])
        self.assertLessEqual(rep.lines_after, 200)              # 예전: 201줄
        sent = [p for p, _ in safe_mode.split_diff_by_file(out)]
        self.assertEqual(sent, list(sizes))                     # 원래 순서 유지
        self.assertIn("README.md", rep.truncated)               # 큰 파일만 잘림

    def test_fair_share(self):
        self.assertEqual(safe_mode.fair_share([98, 30, 20], 100), [50, 30, 20])
        self.assertEqual(sum(safe_mode.fair_share([500, 500, 500], 200)), 200)

    def test_drop_reason_is_correct(self):
        diff = "".join(_file(f"f{i}.py", 10) for i in range(12))
        _, rep = safe_mode.apply(diff, max_files=10, max_lines=200)
        self.assertEqual(len(rep.dropped_by_count), 2)
        self.assertTrue(any("파일 수 제한(10개)" in s for s in rep.summary_lines()))
        diff = "".join(_file(f"f{i}.py", 50) for i in range(10))
        _, rep = safe_mode.apply(diff, max_files=10, max_lines=40)
        self.assertEqual(rep.dropped_by_count, [])
        self.assertTrue(rep.dropped_by_lines)
        self.assertTrue(any("줄 수 제한(40줄)" in s for s in rep.summary_lines()))

    def test_omitted_note(self):
        diff = _file(".env", 3) + _file("big.py", 300)
        _, rep = safe_mode.apply(diff)
        note = rep.omitted_note()
        self.assertIn(".env: 민감/자동생성", note)
        self.assertIn("big.py: 앞부분만 전송", note)


class SecretFalsePositiveTest(unittest.TestCase):
    """B-3: 코드 표현식은 가리지 않고, 진짜 문자열 비밀값은 가린다."""

    def test_code_expressions_not_masked(self):
        diff = ('diff --git a/a.py b/a.py\n+            "max_tokens": req.max_tokens,\n'
                '+    max_tokens=int(st["max_tokens"]),\n+    token = get_token()\n+    api_key = cfg["key"]\n')
        out, rep = safe_mode.apply(diff)
        self.assertEqual(out, diff)
        self.assertFalse(rep.masked)

    def test_literals_still_masked(self):
        diff = ('diff --git a/a.py b/a.py\n+password = "my.pass.word"\n+db_password: s3cretValue\n'
                '+"api_key": "abcd1234efgh"\n')
        out, rep = safe_mode.apply(diff)
        for secret in ("my.pass.word", "s3cretValue", "abcd1234efgh"):
            self.assertNotIn(secret, out)
        self.assertEqual(rep.masked["SECRET_ASSIGN"], 3)


class TruncationTest(unittest.TestCase):
    """B-1: 잘린 응답은 끊긴 줄을 버리고 검증 실패로 표시한다."""

    def test_commit_truncated(self):
        raw = "refactor: AI 클라이언트 개선\n\n- 요청 구성을 분리\n- CLI 인자 정의를 확장하여"
        r = postprocess.process_commit(raw, truncated=True)
        self.assertEqual(r.body, "- 요청 구성을 분리")
        self.assertFalse([c for c in r.checks if "완결성" in c.name][0].ok)

    def test_title_never_dropped(self):
        r = postprocess.process_commit("feat: 제목만 있음", truncated=True)
        self.assertEqual(r.title, "feat: 제목만 있음")

    def test_pr_truncated(self):
        raw = "TITLE: x\n## Why\n- a\n## What\n- b\n## How to Test\n- `python3 main.py` 실행 후 출력이"
        r = postprocess.process_pr(raw, truncated=True)
        self.assertIn("## How to Test\n- (작성 필요", r.body)


class PromptAndParamsE2ETest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_gemini_sends_reasoning_effort_and_reports_thinking(self):
        (self.repo / "app.py").write_text("x = 1\n")
        ai = FakeAI(reply="feat: x\n\n- a\n- b는 XYZ", finish="length",
                    usage={"prompt_tokens": 600, "completion_tokens": 81, "total_tokens": 2648})
        try:
            code, out, err = run_cli(self.repo, "commit", "--provider", "gemini", "--base-url", ai.url,
                                     env_extra=KEY)
        finally:
            ai.close()
        self.assertEqual(code, 0, err)
        self.assertEqual(ai.requests[0]["body"]["reasoning_effort"], "low")
        self.assertIn("생각 1967", out)
        self.assertIn("응답이 max_tokens 에서 잘렸습니다", out)
        self.assertNotIn("XYZ", out.split("--- Commit Message ---")[1])
        self.assertIn("⚠ 응답 완결성", out)

    def test_openai_does_not_send_reasoning_effort(self):
        (self.repo / "app.py").write_text("x = 1\n")
        ai = FakeAI()
        try:
            run_cli(self.repo, "commit", "--provider", "openai", "--base-url", ai.url, env_extra=KEY)
            run_cli(self.repo, "commit", "--provider", "gemini", "--reasoning-effort", "off",
                    "--base-url", ai.url, env_extra=KEY)
        finally:
            ai.close()
        self.assertNotIn("reasoning_effort", ai.requests[0]["body"])
        self.assertNotIn("reasoning_effort", ai.requests[1]["body"])

    def test_prompt_has_diff_rules_and_omitted_files(self):
        (self.repo / "big.py").write_text("".join(f"v{i} = {i}\n" for i in range(400)))
        code, out, _ = run_cli(self.repo, "commit", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("앞이 공백인 줄은 바뀌지 않은 문맥", out)
        self.assertIn("type은 docs", out)
        self.assertIn("[생략된 파일]", out)
        self.assertIn("big.py: 앞부분만 전송", out)


if __name__ == "__main__":
    unittest.main()
