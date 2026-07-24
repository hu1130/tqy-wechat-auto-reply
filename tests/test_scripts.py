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
        self.assertIn("No-WeChat simulation command", text)

    def test_setup_script_creates_venv_and_runs_safety_checks(self) -> None:
        text = (ROOT / "setup_test_environment.ps1").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        self.assertIn('-m", "venv"', text)
        self.assertIn("pip install -r requirements.txt", text)
        self.assertIn("main.py init --yes", text)
        self.assertIn("unittest discover -s tests -v", text)
        self.assertIn("main.py doctor --skip-llm", text)
        self.assertIn("main.py simulate --reply", text)
        self.assertIn("wxauto4==41.1.2", requirements)

    def test_batch_wrapper_runs_setup_script(self) -> None:
        text = (ROOT / "test_environment.bat").read_text(encoding="utf-8")

        self.assertIn("setup_test_environment.ps1", text)
        self.assertIn("ExecutionPolicy Bypass", text)

    def test_watchdog_restarts_stale_or_blocked_bot(self) -> None:
        text = (ROOT / "run_watchdog.ps1").read_text(encoding="utf-8")

        self.assertIn("[string]$PythonPath", text)
        self.assertIn('status -eq "blocked"', text)
        self.assertIn("Health file is stale", text)
        self.assertIn("Stop-Process -Id $process.Id -Force", text)
        self.assertIn('[ValidateSet("local", "center")]', text)
        self.assertIn('"connector", "run-center"', text)


if __name__ == "__main__":
    unittest.main()
