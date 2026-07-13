from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ScriptTests(unittest.TestCase):
    def test_bootstrap_script_targets_company_repo(self) -> None:
        text = (ROOT / "bootstrap_clean_windows.ps1").read_text(encoding="utf-8")

        self.assertIn("https://github.com/hu1130/tqy-wechat-auto-reply.git", text)
        self.assertIn('"Git.Git"', text)
        self.assertIn("winget install --id $WingetId", text)
        self.assertIn("git clone --branch", text)
        self.assertIn("setup_test_environment.ps1", text)

    def test_setup_script_creates_venv_and_runs_safety_checks(self) -> None:
        text = (ROOT / "setup_test_environment.ps1").read_text(encoding="utf-8")

        self.assertIn('-m", "venv"', text)
        self.assertIn("pip install -r requirements.txt", text)
        self.assertIn("main.py init --yes", text)
        self.assertIn("unittest discover -s tests -v", text)
        self.assertIn("main.py doctor --skip-llm", text)

    def test_batch_wrapper_runs_setup_script(self) -> None:
        text = (ROOT / "test_environment.bat").read_text(encoding="utf-8")

        self.assertIn("setup_test_environment.ps1", text)
        self.assertIn("ExecutionPolicy Bypass", text)


if __name__ == "__main__":
    unittest.main()
