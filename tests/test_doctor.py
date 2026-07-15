from __future__ import annotations

import unittest
from unittest.mock import patch

from wechat_ai.doctor import DoctorCheck, check_wechat_window, render_checks


class DoctorRenderTests(unittest.TestCase):
    def test_render_checks_includes_fix_hint(self) -> None:
        text = render_checks(
            [
                DoctorCheck(
                    name="LLM",
                    status="fail",
                    detail="Cannot reach Ollama.",
                    fix="Start Ollama.",
                )
            ]
        )
        self.assertIn("[FAIL] LLM: Cannot reach Ollama.", text)
        self.assertIn("fix: Start Ollama.", text)

    def test_wechat_window_check_warns_when_classic_window_is_missing(self) -> None:
        with patch("wechat_ai.doctor.platform.system", return_value="Windows"):
            with patch("wechat_ai.doctor._find_window_by_class", return_value=0):
                check = check_wechat_window()

        self.assertEqual(check.status, "warn")
        self.assertIn("Classic Windows WeChat main window was not found.", check.detail)
        self.assertIn("WeChat 4.x", check.fix)

    def test_wechat_window_check_passes_when_classic_window_exists(self) -> None:
        with patch("wechat_ai.doctor.platform.system", return_value="Windows"):
            with patch("wechat_ai.doctor._find_window_by_class", return_value=12345):
                check = check_wechat_window()

        self.assertEqual(check.status, "ok")
        self.assertIn("hwnd=12345", check.detail)


if __name__ == "__main__":
    unittest.main()
