from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import string


@dataclass(frozen=True)
class KnowledgeHit:
    title: str
    content: str
    score: int


class KnowledgeBase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.entries = self._load_entries(self.path)

    def search(self, query: str, limit: int = 3) -> list[KnowledgeHit]:
        if not query.strip() or not self.entries:
            return []
        scored: list[KnowledgeHit] = []
        query_terms = self._terms(query)
        query_chars = self._significant_chars(query)
        for title, content in self.entries:
            haystack = f"{title}\n{content}".casefold()
            score = 0
            for term in query_terms:
                if term in haystack:
                    score += max(2, len(term))
            score += sum(1 for char in query_chars if char in haystack)
            if score > 0:
                scored.append(KnowledgeHit(title=title, content=content, score=score))
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[: max(0, limit)]

    def build_context(self, query: str, limit: int = 3) -> str:
        hits = self.search(query, limit=limit)
        if not hits:
            return ""
        blocks = []
        for index, hit in enumerate(hits, start=1):
            title = hit.title or f"知识片段 {index}"
            blocks.append(f"[{index}] {title}\n{hit.content}")
        return "\n\n".join(blocks)

    @staticmethod
    def _load_entries(path: Path) -> list[tuple[str, str]]:
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return []

        entries: list[tuple[str, str]] = []
        current_title = ""
        current_lines: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            if line.startswith("#"):
                if current_lines:
                    entries.append((current_title, "\n".join(current_lines).strip()))
                    current_lines = []
                current_title = line.lstrip("#").strip()
                continue
            current_lines.append(line)
        if current_lines:
            entries.append((current_title, "\n".join(current_lines).strip()))

        compact_entries = [
            (title, content)
            for title, content in entries
            if content
        ]
        if compact_entries:
            return compact_entries
        return [("", block.strip()) for block in re.split(r"\n\s*\n", text) if block.strip()]

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {
            item.casefold()
            for item in re.split(r"[\s,，。！？!?:：;；、/\\|()\[\]{}<>《》\"']+", text)
            if len(item.strip()) >= 2
        }

    @staticmethod
    def _significant_chars(text: str) -> set[str]:
        ignored = set(string.whitespace + string.punctuation)
        ignored.update("，。！？；：、（）【】《》“”‘’")
        return {
            char.casefold()
            for char in text
            if char not in ignored and not char.isdigit()
        }
