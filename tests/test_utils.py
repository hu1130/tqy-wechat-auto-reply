from __future__ import annotations

import unittest

from wechat_ai.utils import MessageDeduplicator, build_history_prompt, strip_think_tags


class StripThinkTagsTests(unittest.TestCase):
    def test_strip_think_tags_removes_hidden_block(self) -> None:
        text = "你好<think>中间思考</think>世界"
        self.assertEqual(strip_think_tags(text), "你好世界")

    def test_strip_think_tags_keeps_plain_text(self) -> None:
        self.assertEqual(strip_think_tags("直接回复"), "直接回复")


class PromptBuilderTests(unittest.TestCase):
    def test_build_history_prompt_contains_recent_turns(self) -> None:
        prompt = build_history_prompt(
            system_prompt="你是助手",
            chat_name="文件传输助手",
            history=[
                {"role": "user", "sender": "张三", "content": "你好"},
                {"role": "assistant", "sender": "assistant", "content": "你好呀"},
            ],
        )
        self.assertIn("当前聊天对象: 文件传输助手", prompt)
        self.assertIn("[user|张三] 你好", prompt)
        self.assertIn("[assistant|assistant] 你好呀", prompt)


class DeduplicatorTests(unittest.TestCase):
    def test_duplicate_message_within_ttl_is_filtered(self) -> None:
        dedup = MessageDeduplicator(ttl_seconds=60)
        first = dedup.is_duplicate("chat", "alice", "hello", "text")
        second = dedup.is_duplicate("chat", "alice", "hello", "text")
        self.assertFalse(first)
        self.assertTrue(second)


if __name__ == "__main__":
    unittest.main()

