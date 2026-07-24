from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import time
from uuid import uuid4


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}


@dataclass(frozen=True)
class ImageResolution:
    status: str
    path: str = ""
    reason: str = ""
    candidates: tuple[str, ...] = ()

    @property
    def found(self) -> bool:
        return self.status == "found" and bool(self.path)


def default_wechat_image_roots() -> list[Path]:
    roots: list[Path] = []
    home = Path(os.path.expanduser("~"))
    for base in (
        home / "xwechat_files",
        home / "Documents" / "xwechat_files",
    ):
        if not base.exists():
            continue
        roots.extend(_recent_rwtemp_roots(base))
        roots.extend(_inputtemp_roots(base))
        if not roots:
            roots.append(base)
    return roots


def _recent_rwtemp_roots(base: Path) -> list[Path]:
    roots: list[Path] = []
    month_names = _recent_month_names()
    for rwtemp in base.glob("wxid_*/temp/RWTemp"):
        if not rwtemp.exists():
            continue
        month_roots = [rwtemp / month_name for month_name in month_names if (rwtemp / month_name).exists()]
        roots.extend(month_roots or [rwtemp])
    return roots


def _inputtemp_roots(base: Path) -> list[Path]:
    roots: list[Path] = []
    for inputtemp in base.glob("wxid_*/temp/InputTemp"):
        if inputtemp.exists():
            roots.append(inputtemp)
    return roots


def _recent_month_names() -> list[str]:
    now = time.localtime()
    names: list[str] = []
    year = now.tm_year
    month = now.tm_mon
    for _ in range(2):
        names.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year -= 1
            month = 12
    return names


def resolve_recent_wechat_image(
    *,
    seen_at: float | None = None,
    roots: list[str | Path] | None = None,
    window_seconds: int = 20,
    exclude_paths: set[str] | None = None,
) -> ImageResolution:
    """Find a unique WeChat temp image near the moment an image message appeared.

    wxauto4 often exposes only "图片" without a file path. WeChat/Weixin still writes
    received or pasted images under xwechat_files temp folders. This resolver only
    returns a file when the time window has exactly one new candidate so we do not
    accidentally attach another customer's image.
    """

    anchor = seen_at or time.time()
    start = anchor - max(window_seconds, 1)
    end = anchor + max(window_seconds, 1)
    excluded = {str(Path(path).resolve()).casefold() for path in (exclude_paths or set())}
    search_roots = [Path(root) for root in (roots or default_wechat_image_roots())]
    candidates: list[Path] = []

    for root in search_roots:
        if not root.exists():
            continue
        try:
            files = root.rglob("*")
            for path in files:
                if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                resolved = str(path.resolve()).casefold()
                if resolved in excluded:
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if start <= mtime <= end:
                    candidates.append(path)
        except OSError:
            continue

    candidates = _prefer_original_received_images(candidates)
    candidate_paths = tuple(str(path) for path in candidates)
    if not candidates:
        return ImageResolution(status="missing", reason="no recent WeChat image cache file")
    if len(candidates) > 1:
        return ImageResolution(
            status="ambiguous",
            reason=f"{len(candidates)} recent WeChat image cache files",
            candidates=candidate_paths,
        )
    return ImageResolution(status="found", path=str(candidates[0]), candidates=candidate_paths)


def _prefer_original_received_images(candidates: list[Path]) -> list[Path]:
    """Prefer original received images over WeChat thumbnails or input temp files."""

    sorted_candidates = sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True)
    preferred = [path for path in sorted_candidates if _is_original_received_image(path)]
    if preferred:
        return preferred
    usable = [path for path in sorted_candidates if not _is_known_non_customer_image(path)]
    return usable or sorted_candidates


def _is_original_received_image(path: Path) -> bool:
    parts = {part.casefold() for part in path.parts}
    return "rwtemp" in parts and not _is_known_non_customer_image(path)


def _is_known_non_customer_image(path: Path) -> bool:
    parts = {part.casefold() for part in path.parts}
    if {"inputtemp", "thumb", "weappicon", "favorite"} & parts:
        return True
    name = path.name.casefold()
    return "_thumb" in name or name.endswith("_hd")


def capture_foreground_window_image(
    output_dir: str | Path = "data/image-captures",
    enhance: bool = True,
) -> ImageResolution:
    """Capture the current foreground window as a fallback for visible WeChat images.

    Some Weixin 4.x image messages are visible in the chat panel but do not expose a
    message file path and are not immediately written to xwechat_files. Capturing the
    active WeChat window lets the vision model inspect the visible customer image.
    """

    try:
        import uiautomation as auto
        from PIL import ImageGrab
    except Exception as exc:
        return ImageResolution(status="missing", reason=f"screenshot dependencies unavailable: {exc}")

    try:
        control = auto.GetForegroundControl()
        rect = getattr(control, "BoundingRectangle", None)
        left = int(getattr(rect, "left", 0))
        top = int(getattr(rect, "top", 0))
        right = int(getattr(rect, "right", 0))
        bottom = int(getattr(rect, "bottom", 0))
        if right - left < 200 or bottom - top < 200:
            return ImageResolution(status="missing", reason="foreground window is too small")

        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"wechat-window-{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}.png"
        image = ImageGrab.grab(bbox=(left, top, right, bottom))
        if enhance:
            image = enhance_wechat_window_capture(image)
        image.save(path)
        return ImageResolution(status="screenshot", path=str(path), candidates=(str(path),))
    except Exception as exc:
        return ImageResolution(status="missing", reason=f"failed to capture foreground window: {exc}")


def open_visible_wechat_image_preview() -> bool:
    """Best-effort double-click of the largest visible image thumbnail in WeChat."""

    try:
        import uiautomation as auto
    except Exception:
        return False

    try:
        root = auto.GetForegroundControl()
        candidates = []
        for control in _walk_controls(root, max_nodes=1800):
            name = str(getattr(control, "Name", "") or "").strip()
            control_type = str(getattr(control, "ControlTypeName", "") or "")
            if not _looks_like_image_control(name, control_type):
                continue
            rect = getattr(control, "BoundingRectangle", None)
            left = int(getattr(rect, "left", 0))
            top = int(getattr(rect, "top", 0))
            right = int(getattr(rect, "right", 0))
            bottom = int(getattr(rect, "bottom", 0))
            width = right - left
            height = bottom - top
            area = width * height
            if width < 60 or height < 40 or area < 3000:
                continue
            candidates.append((area, control))
        if not candidates:
            return False
        _, control = sorted(candidates, key=lambda item: item[0], reverse=True)[0]
        double_click = getattr(control, "DoubleClick", None)
        if callable(double_click):
            double_click()
        else:
            control.Click()
            time.sleep(0.15)
            control.Click()
        time.sleep(0.8)
        return True
    except Exception:
        return False


def _walk_controls(root: object, *, max_nodes: int) -> list[object]:
    result: list[object] = []
    queue = [root]
    while queue and len(result) < max_nodes:
        control = queue.pop(0)
        result.append(control)
        get_children = getattr(control, "GetChildren", None)
        if callable(get_children):
            try:
                queue.extend(list(get_children() or []))
            except Exception:
                pass
    return result


def _looks_like_image_control(name: str, control_type: str) -> bool:
    haystack = f"{name} {control_type}".casefold()
    return any(token in haystack for token in ("image", "picture", "photo", "图片", "图像"))


def enhance_wechat_window_capture(image: object) -> object:
    """Crop WeChat chrome away and upscale the visible chat content.

    The fallback screenshot is only used when no original image file is available.
    Sending the whole WeChat window wastes pixels on the session list and input box,
    so we keep the chat content area and enlarge it before passing it to vision.
    """

    width, height = image.size
    if width < 400 or height < 400:
        return image

    left = min(int(width * 0.22), 260)
    top = min(int(height * 0.08), 90)
    right = width
    bottom = max(int(height * 0.78), height - 180)
    if right - left >= 250 and bottom - top >= 250:
        image = image.crop((left, top, right, bottom))

    width, height = image.size
    target_width = min(width * 2, 2200)
    scale = max(1, int(target_width / width))
    if scale > 1:
        try:
            from PIL import Image

            image = image.resize((width * scale, height * scale), Image.Resampling.LANCZOS)
        except Exception:
            image = image.resize((width * scale, height * scale))
    return image
