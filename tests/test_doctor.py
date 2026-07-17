from __future__ import annotations

import unittest
from unittest.mock import patch

from wechat_ai.doctor import DoctorCheck, check_weixin_window, render_checks


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

    def test_weixin_window_check_warns_when_window_is_missing(self) -> None:
        class MissingWindow:
            Name = ""
            ClassName = ""

            def Exists(self, timeout: int) -> bool:
                return False

        class FakeAutomation:
            @staticmethod
            def SetGlobalSearchTimeout(timeout: int) -> None:
                pass

            @staticmethod
            def WindowControl(searchDepth: int, Name: str) -> MissingWindow:
                return MissingWindow()

        with patch("wechat_ai.doctor.platform.system", return_value="Windows"):
            with patch.dict("sys.modules", {"uiautomation": FakeAutomation}):
                check = check_weixin_window()

        self.assertEqual(check.status, "warn")
        self.assertIn("Could not find", check.detail)
        self.assertIn("4.1.8.107", check.fix)

    def test_weixin_window_check_passes_when_window_exists(self) -> None:
        class MainWindow:
            Name = "微信"
            ClassName = "mmui::MainWindow"

            def Exists(self, timeout: int) -> bool:
                return True

        class FakeAutomation:
            @staticmethod
            def SetGlobalSearchTimeout(timeout: int) -> None:
                pass

            @staticmethod
            def WindowControl(searchDepth: int, Name: str) -> MainWindow:
                return MainWindow()

        with patch("wechat_ai.doctor.platform.system", return_value="Windows"):
            with patch.dict("sys.modules", {"uiautomation": FakeAutomation}):
                check = check_weixin_window()

        self.assertEqual(check.status, "ok")
        self.assertIn("mmui::MainWindow", check.detail)


if __name__ == "__main__":
    unittest.main()
