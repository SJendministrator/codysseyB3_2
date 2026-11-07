"""실제 CLI 프로세스를 띄워 '단일 실행으로 끝까지' 동작하는지 검증한다."""
import shutil
import unittest

from tests.helpers import FakeAI, git, make_repo, run_cli

KEY = {"AI_API_KEY": "test-key-123"}


class E2ETest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_no_changes(self):
        code, out, _ = run_cli(self.repo, "commit")
        self.assertEqual(code, 0)
        self.assertIn("변경 사항이 없습니다", out)

    def test_not_repo_root(self):
        (self.repo / "sub").mkdir()
        code, _, err = run_cli(self.repo / "sub", "commit")
        self.assertEqual(code, 1)
        self.assertIn("프로젝트 루트", err)

    def test_missing_key(self):
        (self.repo / "app.py").write_text("def hello():\n    return 'hello'\n")
        code, _, err = run_cli(self.repo, "commit")
        self.assertEqual(code, 1)
        self.assertIn("AI_API_KEY 환경변수가 설정되지 않았습니다", err)
        self.assertIn('export AI_API_KEY="YOUR_KEY"', err)

    def test_commit_flow_with_safe_mode(self):
        (self.repo / "app.py").write_text("def hello():\n    return 'hello'\n")
        (self.repo / "settings.py").write_text("API_KEY = 'sk-proj-SECRETSECRETSECRET12345'\n")
        ai = FakeAI(reply="```\nfeat(app): 인사 문구 변경 및 설정 파일 추가\n\n* app.py 반환값 수정\n```")
        try:
            code, out, err = run_cli(self.repo, "commit", "--provider", "openai", "--base-url", ai.url,
                                     "--model", "my-model", "--temperature", "0.1", "--max-tokens", "300",
                                     "-o", "msg.txt", env_extra=KEY)
        finally:
            ai.close()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(ai.requests), 1)                       # 정확히 1회 호출
        req = ai.requests[0]
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(req["headers"]["Authorization"], "Bearer test-key-123")
        self.assertEqual(req["body"]["model"], "my-model")
        self.assertEqual(req["body"]["temperature"], 0.1)
        self.assertEqual(req["body"]["max_tokens"], 300)
        sent = req["body"]["messages"][1]["content"]
        self.assertNotIn("SECRETSECRET", sent)                      # 안전 모드 마스킹
        self.assertIn("settings.py", sent)                          # untracked 새 파일 포함
        self.assertIn("--- Commit Message ---", out)
        self.assertIn("feat(app): 인사 문구 변경 및 설정 파일 추가\n\n- app.py 반환값 수정", out)
        self.assertIn("AI API 호출 횟수: 1", out)
        self.assertTrue((self.repo / "msg.txt").read_text().startswith("feat(app):"))

    def test_staged_only(self):
        (self.repo / "app.py").write_text("x = 1\n")
        (self.repo / "other.py").write_text("y = 2\n")
        git(self.repo, "add", "app.py")
        code, out, _ = run_cli(self.repo, "commit", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("스테이징된(git add) 변경만", out)
        self.assertNotIn("other.py", out.split("[diff]")[1])
        self.assertIn("AI API 호출 횟수: 0", out)

    def test_pr_flow_anthropic(self):
        git(self.repo, "checkout", "-q", "-b", "feature/x")
        (self.repo / "app.py").write_text("def hello():\n    return 'hello'\n")
        git(self.repo, "commit", "-qam", "change")
        reply = "TITLE: feat: 인사 변경\n\n## Why\n- 문구 개선\n\n## What\n- app.py 수정\n"
        ai = FakeAI(reply=reply, style="anthropic")
        try:
            code, out, err = run_cli(self.repo, "pr", "--provider", "anthropic", "--base-url", ai.url,
                                     env_extra=KEY)
        finally:
            ai.close()
        self.assertEqual(code, 0, err)
        req = ai.requests[0]
        self.assertEqual(req["path"], "/v1/messages")
        self.assertEqual(req["headers"]["X-Api-Key"], "test-key-123")
        self.assertIn("main...HEAD", out)
        self.assertIn("--- PR Title ---\nfeat: 인사 변경", out)
        self.assertIn("## How to Test\n- (작성 필요", out)          # 누락 섹션 후처리
        self.assertIn("⚠", out)

    def test_auth_failure(self):
        (self.repo / "app.py").write_text("z = 3\n")
        ai = FakeAI(status=401)
        try:
            code, _, err = run_cli(self.repo, "commit", "--provider", "openai", "--base-url", ai.url,
                                   env_extra=KEY)
        finally:
            ai.close()
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401 인증 실패", err)
        self.assertIn("Incorrect API key", err)

    def test_network_failure(self):
        (self.repo / "app.py").write_text("z = 3\n")
        code, _, err = run_cli(self.repo, "commit", "--provider", "openai",
                               "--base-url", "http://127.0.0.1:9/v1", env_extra=KEY)
        self.assertEqual(code, 1)
        self.assertIn("네트워크 오류", err)

    def test_config_convention(self):
        (self.repo / ".ai-gitgen.json").write_text(
            '{"convention": {"commit_types": ["add", "fix"], "scopes": ["api"], "require_scope": true,'
            ' "extra_rules": ["이슈 번호를 제목 끝에 (#번호) 로 붙인다"]}, "max_lines": 50}')
        (self.repo / "app.py").write_text("z = 3\n")
        code, out, _ = run_cli(self.repo, "commit", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("type은 다음 중 하나: add, fix", out)
        self.assertIn("scope는 반드시 다음 중 하나를 쓴다: api", out)
        self.assertIn("이슈 번호", out)

    def test_config_rejects_key(self):
        (self.repo / ".ai-gitgen.json").write_text('{"api_key": "x"}')
        (self.repo / "app.py").write_text("z = 3\n")
        code, out, err = run_cli(self.repo, "commit", "--dry-run")
        self.assertNotEqual(code, 0)
        self.assertIn("API Key를 넣지 마세요", out + err)


if __name__ == "__main__":
    unittest.main()
