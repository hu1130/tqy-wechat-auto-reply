from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wechat_ai.ops import append_rotating_line, redact_sensitive_text, save_report, write_health_file


class OpsTests(unittest.TestCase):
    def test_redact_sensitive_text_masks_common_identifiers(self) -> None:
        text = (
            "手机号13812345678 身份证110101199001011234 "
            "银行卡6222021234567890123 验证码是123456 test@example.com"
        )

        redacted = redact_sensitive_text(text)

        self.assertIn("138****5678", redacted)
        self.assertIn("110101********1234", redacted)
        self.assertIn("6222********0123", redacted)
        self.assertIn("验证码是******", redacted)
        self.assertIn("t***@example.com", redacted)
        self.assertNotIn("13812345678", redacted)

    def test_append_rotating_line_rotates_large_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "app.log"
            path.write_text("x" * 20, encoding="utf-8")

            append_rotating_line(path, "new", max_bytes=10, backup_count=2)

            self.assertEqual(path.read_text(encoding="utf-8"), "new\n")
            self.assertTrue((Path(temp_dir) / "app.log.1").exists())

    def test_save_report_and_health_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = save_report(Path(temp_dir) / "reports", "hello", "2026-07-11")
            health_path = Path(temp_dir) / "health.json"
            write_health_file(health_path, {"status": "running"})

            self.assertEqual(report_path.read_text(encoding="utf-8"), "hello")
            self.assertIn('"status": "running"', health_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
