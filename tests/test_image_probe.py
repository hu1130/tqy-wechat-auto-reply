from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wechat_ai.image_probe import inspect_message, message_key, run_image_probe


class FakeImageMessage:
    def __init__(self) -> None:
        self.sender = "alice"
        self.type = "image"
        self.content = "[图片]"
        self.file_path = r"C:\Users\usb\Pictures\sample.png"
        self.info = {"name": "sample.png", "path": self.file_path}

    def Save(self) -> None:
        pass


class ImageProbeTests(unittest.TestCase):
    def test_inspect_message_detects_image_and_candidate_path(self) -> None:
        record = inspect_message(FakeImageMessage())

        self.assertTrue(record["is_image_like"])
        self.assertEqual(record["type"], "image")
        self.assertIn(r"C:\Users\usb\Pictures\sample.png", record["candidate_paths"])
        self.assertIn("Save", record["callables"])

    def test_message_key_is_stable_for_message_fields(self) -> None:
        message = FakeImageMessage()

        self.assertEqual(message_key(message), message_key(message))

    def test_run_image_probe_records_chat_name_and_message_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            message = FakeImageMessage()

            class FakeWxProbe:
                def ChatWith(self, chat_name: str) -> None:
                    self.chat_name = chat_name

                def GetAllMessage(self) -> list[FakeImageMessage]:
                    return [message]

            with patch("wechat_ai.image_probe._create_wechat_client", return_value=FakeWxProbe()):
                output = run_image_probe(
                    chat_name="chat-a",
                    seconds=1,
                    interval_seconds=0.2,
                    output_dir=temp_dir,
                )

            payload = output.read_text(encoding="utf-8")

        self.assertIn('"chat_name": "chat-a"', payload)
        self.assertIn('"message_key"', payload)
        self.assertTrue(Path(str(output)).name.startswith("image-probe-chat"))


if __name__ == "__main__":
    unittest.main()
