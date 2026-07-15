from __future__ import annotations

import unittest
from unittest.mock import patch

from wechat_ai.doctor import DoctorCheck, check_wechat_client, check_wxauto4, render_checks


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

    def test_wechat_client_check_warns_when_client_is_missing(self) -> None:
        with patch("wechat_ai.doctor.platform.system", return_value="Windows"):
            with patch("wechat_ai.doctor._list_running_process_names", return_value=set()):
                check = check_wechat_client()

        self.assertEqual(check.status, "warn")
        self.assertIn("Windows WeChat client was not found.", check.detail)
        self.assertIn("current Windows Weixin client", check.fix)

    def test_wechat_client_check_warns_when_process_list_is_unavailable(self) -> None:
        with patch("wechat_ai.doctor.platform.system", return_value="Windows"):
            with patch("wechat_ai.doctor._list_running_process_names", return_value=None):
                check = check_wechat_client()

        self.assertEqual(check.status, "warn")
        self.assertIn("Could not inspect running Windows processes", check.detail)
        self.assertIn("same signed-in Windows session", check.fix)

    def test_wechat_client_check_passes_when_weixin_is_running(self) -> None:
        with patch("wechat_ai.doctor.platform.system", return_value="Windows"):
            with patch("wechat_ai.doctor._list_running_process_names", return_value={"weixin.exe"}):
                check = check_wechat_client()

        self.assertEqual(check.status, "ok")
        self.assertIn("weixin.exe", check.detail)

    def test_wxauto4_check_fails_when_native_import_is_blocked(self) -> None:
        with patch("wechat_ai.doctor.platform.system", return_value="Windows"):
            with patch("wechat_ai.doctor.importlib.util.find_spec", return_value=object()):
                with patch("wechat_ai.doctor.importlib.import_module", side_effect=ImportError("DLL blocked")):
                    check = check_wxauto4()

        self.assertEqual(check.status, "fail")
        self.assertIn("could not be imported", check.detail)
        self.assertIn("do not disable or bypass", check.fix)


if __name__ == "__main__":
    unittest.main()
