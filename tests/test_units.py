import unittest

from gitgen import git_collector, postprocess, safe_mode


class SafeModeTest(unittest.TestCase):
    def test_masks_keys_and_personal_info(self):
        diff = (
            "diff --git a/config.py b/config.py\n"
            "+OPENAI = 'sk-proj-abcdefghijklmnopqrstuvwx1234'\n"
            "+GOOGLE = 'AIzaSyA1234567890abcdefghijklmnopqrstuv'\n"
            "+AWS_ID = 'AKIAABCDEFGHIJKLMNOP'\n"
            "+password = \"hunter2hunter2\"\n"
            "+contact: dev.kim@company.co.kr / 010-1234-5678\n"
        )
        out, rep = safe_mode.apply(diff)
        for secret in ("sk-proj-abc", "AIzaSy", "AKIAABCD", "hunter2", "dev.kim@", "010-1234"):
            self.assertNotIn(secret, out)
        self.assertIn("password = \"[MASKED:SECRET]\"", out)
        self.assertEqual(rep.masked["OPENAI_KEY"], 1)
        self.assertNotIn("SECRET", [k for k, v in rep.masked.items() if k == "SECRET_ASSIGN" and v > 1])

    def test_no_false_positive_on_numbers_and_env(self):
        diff = ("diff --git a/a.py b/a.py\n+max_tokens = 2048\n+use_token = true\n"
                "+api_key = os.environ.get('AI_API_KEY')\n+API_TOKEN=${API_TOKEN}\n")
        out, rep = safe_mode.apply(diff)
        self.assertEqual(out, diff)
        self.assertFalse(rep.masked)

    def test_private_key_block(self):
        diff = "diff --git a/k.txt b/k.txt\n+-----BEGIN RSA PRIVATE KEY-----\n+MIIEow\n+-----END RSA PRIVATE KEY-----\n"
        out, rep = safe_mode.apply(diff)
        self.assertNotIn("MIIEow", out)
        self.assertEqual(rep.masked["PRIVATE_KEY"], 1)

    def test_excludes_sensitive_files(self):
        diff = "diff --git a/.env b/.env\n+DB_PASS=xxx\ndiff --git a/a.py b/a.py\n+print(1)\n"
        out, rep = safe_mode.apply(diff)
        self.assertNotIn("DB_PASS", out)
        self.assertIn(".env", rep.excluded_files)
        self.assertIn("print(1)", out)

    def test_limits(self):
        diff = "".join(f"diff --git a/f{i}.py b/f{i}.py\n" + "+x\n" * 30 for i in range(15))
        out, rep = safe_mode.apply(diff, max_files=10, max_lines=100)
        self.assertLessEqual(rep.lines_after, 100 + 10)
        self.assertGreater(len(rep.dropped_files), 0)

    def test_off_sends_raw(self):
        diff = "diff --git a/a b/a\n+token = 'abcdefgh'\n"
        out, rep = safe_mode.apply(diff, enabled=False)
        self.assertEqual(out, diff)

    def test_custom_pattern(self):
        diff = "diff --git a/a b/a\n+emp_id = EMP-123456\n"
        out, rep = safe_mode.apply(diff, extra_patterns={"EMPLOYEE_ID": r"EMP-\d{6}"})
        self.assertIn("[MASKED:EMPLOYEE_ID]", out)


class CommitPostprocessTest(unittest.TestCase):
    def test_cleans_wrapping_and_labels(self):
        raw = "```\n커밋 메시지: \"feat(cli): 커밋 생성 기능 추가.\"\n\n* git diff 수집\n• 템플릿 적용\n```"
        r = postprocess.process_commit(raw)
        self.assertEqual(r.title, "feat(cli): 커밋 생성 기능 추가")
        self.assertEqual(r.body, "- git diff 수집\n- 템플릿 적용")
        self.assertTrue(all(c.ok for c in r.checks))

    def test_truncates_long_subject(self):
        r = postprocess.process_commit("feat: " + "아주 긴 제목 " * 20)
        self.assertLessEqual(len(r.title), 72)
        self.assertTrue(any("72" in f for f in r.fixes))

    def test_flags_non_conventional(self):
        r = postprocess.process_commit("그냥 고쳤음")
        self.assertFalse([c for c in r.checks if "Conventional" in c.name][0].ok)


class PRPostprocessTest(unittest.TestCase):
    def test_full_template(self):
        raw = ("TITLE: feat: 커밋/PR 자동 생성\n\n## Why\n- 시간 절약\n\n## What (핵심 변경)\n- diff 수집\n"
               "\n## How to Test\n1. `python main.py pr` 실행\n")
        r = postprocess.process_pr(raw)
        self.assertEqual(r.title, "feat: 커밋/PR 자동 생성")
        self.assertIn("## What\n- diff 수집", r.body)
        self.assertIn("- `python main.py pr` 실행", r.body)
        self.assertTrue(all(c.ok for c in r.checks))

    def test_missing_section_gets_placeholder(self):
        r = postprocess.process_pr("TITLE: x\n## Why\n- a\n## What\n- b\n")
        self.assertIn("## How to Test\n- (작성 필요", r.body)
        self.assertFalse([c for c in r.checks if "How to Test" in c.name][0].ok)

    def test_paragraph_becomes_bullet_and_title_truncated(self):
        r = postprocess.process_pr("TITLE: " + "a" * 100 + "\n## Why\n문단으로 쓴 배경\n## What\n- b\n## How to Test\n- c")
        self.assertLessEqual(len(r.title), 80)
        self.assertIn("## Why\n- 문단으로 쓴 배경", r.body)


class StatusParseTest(unittest.TestCase):
    def test_parse(self):
        files = git_collector.parse_status(" M a.py\nA  b.py\n?? c.py\nR  old.py -> new.py\n")
        self.assertEqual([(f.status, f.path) for f in files],
                         [("M", "a.py"), ("A", "b.py"), ("??", "c.py"), ("R", "new.py")])


if __name__ == "__main__":
    unittest.main()
