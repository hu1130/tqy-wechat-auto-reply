from __future__ import annotations

import hashlib
import re
import time
from typing import Any


EXCLUDED_CONTACT_LABELS = {
    "\u901a\u8baf\u5f55",
    "\u65b0\u7684\u670b\u53cb",
    "\u670b\u53cb\u9a8c\u8bc1",
    "\u516c\u4f17\u53f7",
    "\u4f01\u4e1a\u5fae\u4fe1\u8054\u7cfb\u4eba",
    "Contacts",
    "New Friends",
    "Official Accounts",
}


def scan_channel_contacts(
    *,
    wechat: Any,
    root: Any | None = None,
    switch_to_contacts: bool = True,
) -> list[dict[str, Any]]:
    """Read contact rows from the WeChat contacts UI without clicking a row.

    When the contact list exposes the standard UI Automation scroll pattern,
    every scroll range is sampled and the original position is restored. A
    caller can distinguish that traversal from a visible-tree-only snapshot by
    inspecting ``raw.coverage``.
    """

    if root is None:
        root = _prepare_contacts_root(wechat, switch_to_contacts=switch_to_contacts)
    rows, coverage = _scan_contact_rows(root)
    anchor_counts: dict[str, int] = {}
    for _control, _texts, anchor in rows:
        if anchor:
            anchor_counts[anchor] = anchor_counts.get(anchor, 0) + 1
    name_occurrences: dict[str, int] = {}
    result: list[dict[str, Any]] = []
    for control, texts, anchor in rows:
        display_name = texts[0]
        remark = texts[1] if len(texts) > 1 else ""
        name_key = "\x1f".join((display_name, remark))
        name_occurrences[name_key] = name_occurrences.get(name_key, 0) + 1
        ordinal = name_occurrences[name_key]
        unique_anchor = bool(anchor and anchor_counts.get(anchor) == 1)
        identity_quality = (
            "automation_id"
            if unique_anchor
            else "display_name" if ordinal == 1 else "display_name_ordinal"
        )
        identifier_source = anchor if unique_anchor else f"{name_key}\x1f{ordinal}"
        external_contact_id = "wxc_" + hashlib.sha256(
            identifier_source.encode("utf-8")
        ).hexdigest()[:32]
        result.append(
            {
                "external_contact_id": external_contact_id,
                "display_name": display_name,
                "remark": remark,
                "kind": _contact_kind(control, texts, anchor),
                "status": "active",
                "raw": {
                    "source": "wechat_contacts_ui",
                    "coverage": coverage,
                    "identity_quality": identity_quality,
                    "anchor": anchor,
                    "anchor_unique": unique_anchor,
                    "occurrence": ordinal,
                    "row_texts": texts,
                },
            }
        )
    return result


def _scan_contact_rows(root: Any) -> tuple[list[tuple[Any, list[str], str]], str]:
    scroll_pattern = _find_contact_scroll_pattern(root)
    if scroll_pattern is None:
        return _visible_contact_rows(root), "visible_automation_tree"

    original_percent = _float_property(scroll_pattern, "VerticalScrollPercent", 0.0)
    rows: dict[tuple[str, tuple[str, ...]], tuple[Any, list[str], str]] = {}
    traversal_complete = False
    try:
        if not _set_vertical_scroll(scroll_pattern, 0.0):
            raise RuntimeError("contact list refused to scroll to the top")
        _merge_contact_rows(rows, _visible_contact_rows(root))
        view_size = _float_property(scroll_pattern, "VerticalViewSize", 10.0)
        step = max(5.0, min(25.0, view_size * 0.8))
        target = step
        while target < 100.0:
            if not _set_vertical_scroll(scroll_pattern, target):
                raise RuntimeError("contact list scroll failed")
            _merge_contact_rows(rows, _visible_contact_rows(root))
            target += step
        if not _set_vertical_scroll(scroll_pattern, 100.0):
            raise RuntimeError("contact list refused to scroll to the bottom")
        _merge_contact_rows(rows, _visible_contact_rows(root))
        traversal_complete = _float_property(
            scroll_pattern, "VerticalScrollPercent", 100.0
        ) >= 99.0
    except Exception:
        _merge_contact_rows(rows, _visible_contact_rows(root))
    finally:
        _set_vertical_scroll(scroll_pattern, original_percent)
    coverage = "full_scroll_pattern" if traversal_complete else "partial_scroll_pattern"
    return list(rows.values()), coverage


def _visible_contact_rows(root: Any) -> list[tuple[Any, list[str], str]]:
    rows: list[tuple[Any, list[str], str]] = []
    for control in _walk_controls(root, max_nodes=4000):
        if not _is_contact_row(control):
            continue
        texts = _contact_texts(control)
        if texts:
            rows.append((control, texts, _control_anchor(control)))
    return rows


def _merge_contact_rows(
    target: dict[tuple[str, tuple[str, ...]], tuple[Any, list[str], str]],
    rows: list[tuple[Any, list[str], str]],
) -> None:
    for row in rows:
        _control, texts, anchor = row
        target.setdefault((anchor, tuple(texts)), row)


def _find_contact_scroll_pattern(root: Any) -> Any | None:
    candidates: list[tuple[int, Any]] = []
    for control in _walk_controls(root, max_nodes=800):
        getter = getattr(control, "GetScrollPattern", None)
        if not callable(getter):
            continue
        try:
            pattern = getter()
            if pattern is None or not bool(getattr(pattern, "VerticallyScrollable", False)):
                continue
            contact_count = len(_visible_contact_rows(control))
        except Exception:
            continue
        if contact_count:
            candidates.append((contact_count, pattern))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _set_vertical_scroll(pattern: Any, percent: float) -> bool:
    setter = getattr(pattern, "SetScrollPercent", None)
    if not callable(setter):
        return False
    try:
        result = setter(-1, max(0.0, min(100.0, float(percent))), waitTime=0.05)
    except TypeError:
        try:
            result = setter(-1, max(0.0, min(100.0, float(percent))))
        except Exception:
            return False
    except Exception:
        return False
    return result is not False


def _float_property(instance: Any, name: str, default: float) -> float:
    try:
        return float(getattr(instance, name))
    except (AttributeError, TypeError, ValueError):
        return default


def _prepare_contacts_root(wechat: Any, *, switch_to_contacts: bool) -> Any:
    try:
        import uiautomation as auto
    except ImportError as exc:
        raise RuntimeError("uiautomation is required for contact scanning") from exc
    switch = getattr(wechat, "SwitchToContact", None)
    if switch_to_contacts:
        if not callable(switch):
            raise RuntimeError("wxauto4 does not expose SwitchToContact")
        try:
            switch()
        except Exception as exc:
            raise RuntimeError(f"failed to open WeChat contacts page: {exc}") from exc
        time.sleep(0.5)
    root = auto.GetForegroundControl()
    if root is None:
        raise RuntimeError("WeChat contacts window is unavailable")
    return root


def _is_contact_row(control: Any) -> bool:
    control_type = str(
        getattr(control, "ControlTypeName", "")
        or getattr(control, "control_type", "")
        or ""
    ).lower()
    if "listitem" not in control_type and "dataitem" not in control_type:
        return False
    texts = _contact_texts(control)
    if not texts:
        return False
    if len(texts) == 1 and re.fullmatch(r"[A-Z#]", texts[0]):
        return False
    return True


def _contact_texts(control: Any) -> list[str]:
    names: list[str] = []
    own_name = _control_name(control)
    if own_name:
        names.append(own_name)
    for child in _walk_controls(control, max_nodes=80)[1:]:
        name = _control_name(child)
        if name:
            names.append(name)
    result: list[str] = []
    for name in names:
        normalized = re.sub(r"\s+", " ", name).strip()
        if not normalized or normalized in EXCLUDED_CONTACT_LABELS:
            continue
        if normalized not in result:
            result.append(normalized)
    return result[:6]


def _contact_kind(control: Any, texts: list[str], anchor: str) -> str:
    combined = " ".join(
        [anchor, *texts, str(getattr(control, "ClassName", "") or "")]
    ).lower()
    group_markers = ("@chatroom", "group", "\u7fa4\u804a", "\u7fa4")
    return "group" if any(marker in combined for marker in group_markers) else "friend"


def _walk_controls(root: Any, *, max_nodes: int) -> list[Any]:
    result: list[Any] = []
    queue = [root]
    visited: set[int] = set()
    while queue and len(result) < max_nodes:
        control = queue.pop(0)
        identity = id(control)
        if identity in visited:
            continue
        visited.add(identity)
        result.append(control)
        children: list[Any] = []
        for method_name in ("GetChildren", "get_children"):
            method = getattr(control, method_name, None)
            if callable(method):
                try:
                    children = list(method() or [])
                except Exception:
                    children = []
                break
        queue.extend(children)
    return result


def _control_anchor(control: Any) -> str:
    for attribute in ("AutomationId", "automation_id", "RuntimeId", "runtime_id"):
        value = getattr(control, attribute, "")
        if callable(value):
            try:
                value = value()
            except Exception:
                value = ""
        if value:
            return re.sub(r"\s+", " ", str(value)).strip()
    return ""


def _control_name(control: Any) -> str:
    for attribute in ("Name", "name"):
        value = getattr(control, attribute, "")
        if value:
            return str(value).strip()
    return ""


__all__ = ["scan_channel_contacts"]
