from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wechat_ai.knowledge import KnowledgeBase


class KnowledgeBaseTests(unittest.TestCase):
    def test_search_returns_matching_markdown_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "faq.md"
            path.write_text(
                "# 发票\n可以开普通发票，专票需要人工确认。\n\n# 上班时间\n工作日 9:00-18:00。",
                encoding="utf-8",
            )

            kb = KnowledgeBase(path)
            hits = kb.search("能开发票吗", limit=1)

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].title, "发票")
        self.assertIn("人工确认", hits[0].content)

    def test_missing_file_has_no_entries(self) -> None:
        kb = KnowledgeBase("missing.md")

        self.assertEqual(kb.entries, [])
        self.assertEqual(kb.search("anything"), [])


if __name__ == "__main__":
    unittest.main()
