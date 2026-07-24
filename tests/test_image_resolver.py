from __future__ import annotations

from pathlib import Path
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

from wechat_ai.image_resolver import default_wechat_image_roots, enhance_wechat_window_capture, resolve_recent_wechat_image


class ImageResolverTests(unittest.TestCase):
    def test_resolves_unique_recent_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "image.png"
            path.write_bytes(b"png")
            now = time.time()
            os.utime(path, (now, now))

            result = resolve_recent_wechat_image(
                seen_at=now,
                roots=[temp_dir],
                window_seconds=5,
            )

        self.assertTrue(result.found)
        self.assertTrue(result.path.endswith("image.png"))

    def test_marks_ambiguous_when_multiple_images_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            now = time.time()
            for name in ("a.png", "b.jpg"):
                path = Path(temp_dir) / name
                path.write_bytes(b"image")
                os.utime(path, (now, now))

            result = resolve_recent_wechat_image(
                seen_at=now,
                roots=[temp_dir],
                window_seconds=5,
            )

        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(len(result.candidates), 2)

    def test_prefers_rwtemp_original_over_inputtemp_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_temp = root / "temp" / "InputTemp"
            rw_temp = root / "temp" / "RWTemp" / "2026-07" / "session"
            input_temp.mkdir(parents=True)
            rw_temp.mkdir(parents=True)
            input_path = input_temp / "window.png"
            original_path = rw_temp / "customer.jpg"
            input_path.write_bytes(b"screenshot")
            original_path.write_bytes(b"original")
            now = time.time()
            os.utime(input_path, (now, now))
            os.utime(original_path, (now - 1, now - 1))

            result = resolve_recent_wechat_image(
                seen_at=now,
                roots=[temp_dir],
                window_seconds=5,
            )

        self.assertTrue(result.found)
        self.assertEqual(Path(result.path).name, "customer.jpg")

    def test_default_roots_include_wechat_inputtemp_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            input_temp = home / "xwechat_files" / "wxid_test" / "temp" / "InputTemp"
            input_temp.mkdir(parents=True)

            with patch("wechat_ai.image_resolver.os.path.expanduser", return_value=str(home)):
                roots = default_wechat_image_roots()

        self.assertIn(input_temp, roots)

    def test_keeps_ambiguous_when_multiple_rwtemp_originals_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            rw_temp = Path(temp_dir) / "temp" / "RWTemp" / "2026-07" / "session"
            rw_temp.mkdir(parents=True)
            now = time.time()
            for name in ("a.jpg", "b.jpg"):
                path = rw_temp / name
                path.write_bytes(b"original")
                os.utime(path, (now, now))

            result = resolve_recent_wechat_image(
                seen_at=now,
                roots=[temp_dir],
                window_seconds=5,
            )

        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(len(result.candidates), 2)

    def test_excludes_already_used_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "image.png"
            path.write_bytes(b"png")
            now = time.time()
            os.utime(path, (now, now))

            result = resolve_recent_wechat_image(
                seen_at=now,
                roots=[temp_dir],
                window_seconds=5,
                exclude_paths={str(path)},
            )

        self.assertEqual(result.status, "missing")

    def test_enhances_window_capture_by_cropping_and_upscaling_chat_area(self) -> None:
        image = Image.new("RGB", (1000, 800), "black")

        enhanced = enhance_wechat_window_capture(image)

        self.assertGreater(enhanced.size[0], 1000)
        self.assertLess(enhanced.size[1], 800 * 2)


if __name__ == "__main__":
    unittest.main()
