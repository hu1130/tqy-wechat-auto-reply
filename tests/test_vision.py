from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tests.test_bot import make_config
from wechat_ai.vision import VisionClient, clean_vision_text


class CapturingVisionClient(VisionClient):
    def __init__(self) -> None:
        super().__init__(
            make_config(
                vision_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                vision_api_key="dashscope-key",
                vision_model="qwen-vl-plus",
                vision_timeout_seconds=20,
                vision_prompt="识别图片",
            )
        )
        self.payload = {}
        self.headers = {}
        self.url = ""

    def _post_json(self, url, payload, headers):  # type: ignore[no-untyped-def]
        self.url = url
        self.payload = payload
        self.headers = headers
        return {"choices": [{"message": {"content": "图片里有AI客服文字"}}]}


class VisionClientTests(unittest.TestCase):
    def test_dashscope_openai_compatible_payload_uses_image_url_data_uri(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "image.png"
            path.write_bytes(b"fake-png")
            client = CapturingVisionClient()

            result = client.describe_image(path)

        self.assertEqual(result, "图片里有AI客服文字")
        self.assertEqual(client.url, "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
        self.assertEqual(client.payload["model"], "qwen-vl-plus")
        content = client.payload["messages"][0]["content"]
        self.assertEqual(content[0]["text"], "识别图片")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(client.headers["Authorization"], "Bearer dashscope-key")

    def test_clean_vision_text_removes_model_disclaimer_tail(self) -> None:
        text = (
            "表格包含客户名称、联系电话、需求类型。\n"
            "由于图片分辨率限制，部分文字细节可能不够清晰，但上述信息可以较为准确地提取出来。"
            "如果有更多具体需求或需要进一步的信息，请告知。"
        )

        self.assertEqual(clean_vision_text(text), "表格包含客户名称、联系电话、需求类型")


if __name__ == "__main__":
    unittest.main()
