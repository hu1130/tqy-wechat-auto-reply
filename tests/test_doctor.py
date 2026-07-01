from __future__ import annotations

import unittest

from wechat_ai.doctor import DoctorCheck, render_checks


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


if __name__ == "__main__":
    unittest.main()
