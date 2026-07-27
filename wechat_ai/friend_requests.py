from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
import time
from typing import Any, Mapping


ACCEPT_LABELS = {
    "\u63a5\u53d7",
    "\u901a\u8fc7",
    "\u540c\u610f",
    "\u6dfb\u52a0",
    "Accept",
    "Add",
}
REJECT_LABELS = {
    "\u62d2\u7edd",
    "\u5ffd\u7565",
    "Reject",
    "Ignore",
}
NEW_FRIEND_LABELS = {
    "\u65b0\u7684\u670b\u53cb",
    "\u670b\u53cb\u9a8c\u8bc1",
    "New Friends",
}
CONFIRM_LABELS = {
    "\u5b8c\u6210",
    "\u786e\u5b9a",
    "Done",
    "Confirm",
}
CONFIRM_CONTEXT_LABELS = {
    "\u6dfb\u52a0\u5230\u901a\u8baf\u5f55",
    "\u6dfb\u52a0\u670b\u53cb",
    "Add to Contacts",
    "Add Friend",
}
REJECT_CONFIRM_CONTEXT_LABELS = {
    "\u786e\u8ba4\u62d2\u7edd",
    "\u62d2\u7edd\u597d\u53cb\u7533\u8bf7",
    "Confirm Reject",
    "Reject Friend Request",
}
STATUS_LABELS = ACCEPT_LABELS | REJECT_LABELS | {
    "\u5df2\u6dfb\u52a0",
    "\u5df2\u63a5\u53d7",
    "\u5df2\u62d2\u7edd",
    "Added",
    "Accepted",
    "Rejected",
}


class FriendRequestActionError(RuntimeError):
    pass


class FriendRequestNotExecutableError(FriendRequestActionError):
    """The requested UI action was not attempted."""


class FriendRequestOutcomeUncertainError(FriendRequestActionError):
    """The click was attempted but its final result cannot be proven."""


@dataclass(frozen=True)
class FriendRequestCandidate:
    external_request_id: str
    display_name: str
    verification_message: str
    status: str = "pending"
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "external_request_id": self.external_request_id,
            "display_name": self.display_name,
            "verification_message": self.verification_message,
            "status": self.status,
            "raw": dict(self.raw),
        }


@dataclass(frozen=True)
class FriendRequestActionResult:
    external_request_id: str
    operation_type: str
    status: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FriendRequestResult:
    found: int
    accepted: int
    dry_run: bool
    labels: tuple[str, ...]


@dataclass
class _ScannedRequest:
    candidate: FriendRequestCandidate
    actions: dict[str, Any]


def scan_friend_requests(
    *,
    switch_to_contacts: bool = True,
    root: Any | None = None,
    open_new_friends: bool = True,
    wechat: Any | None = None,
) -> list[FriendRequestCandidate]:
    """Return identifiable pending requests without clicking any UI control."""

    return [
        item.candidate
        for item in _scan_friend_request_controls(
            switch_to_contacts=switch_to_contacts,
            root=root,
            open_new_friends=open_new_friends,
            wechat=wechat,
        )
    ]


def execute_friend_request_action(
    external_request_id: str,
    operation_type: str,
    *,
    switch_to_contacts: bool = True,
    root: Any | None = None,
    verify_delay_seconds: float = 0.8,
    wechat: Any | None = None,
) -> FriendRequestActionResult:
    """Click one exact request, rejecting ambiguous or stale snapshots."""

    request_id = str(external_request_id).strip()
    if not request_id:
        raise ValueError("external_request_id must not be empty")
    action = _normalize_operation_type(operation_type)
    scanned = _scan_friend_request_controls(
        switch_to_contacts=switch_to_contacts,
        root=root,
        open_new_friends=True,
        wechat=wechat,
    )
    matches = [item for item in scanned if item.candidate.external_request_id == request_id]
    if not matches:
        raise FriendRequestNotExecutableError(
            "friend request is no longer visible; no click was attempted"
        )
    if len(matches) != 1:
        raise FriendRequestNotExecutableError(
            "friend request identity is ambiguous; no click was attempted"
        )
    selected = matches[0]
    control = selected.actions.get(action)
    if control is None:
        raise FriendRequestNotExecutableError(
            f"friend request does not expose a {action} action"
        )

    label = _control_name(control)
    try:
        control.Click()
    except Exception as exc:
        raise FriendRequestOutcomeUncertainError(
            f"failed to click exact friend request action: {exc}"
        ) from exc

    confirmation = _complete_confirmation_if_present(
        action,
        selected.candidate.display_name,
        root=root,
        wechat=wechat,
    )
    if verify_delay_seconds > 0:
        time.sleep(verify_delay_seconds)
    verification = _verify_action_outcome(
        request_id,
        action,
        switch_to_contacts=False,
        root=root,
        wechat=wechat,
    )
    return FriendRequestActionResult(
        external_request_id=request_id,
        operation_type=action,
        status="succeeded",
        evidence={
            "display_name": selected.candidate.display_name,
            "verification_message": selected.candidate.verification_message,
            "clicked_label": label,
            "confirmation": confirmation,
            "verification": verification,
        },
    )


def accept_friend_requests(
    *,
    live: bool = False,
    limit: int = 10,
    switch_to_contacts: bool = True,
    root: Any | None = None,
) -> FriendRequestResult:
    """Compatibility helper for local/manual use; center jobs use exact IDs."""

    if limit <= 0:
        return FriendRequestResult(found=0, accepted=0, dry_run=not live, labels=())
    scanned = _scan_friend_request_controls(
        switch_to_contacts=switch_to_contacts,
        root=root,
        open_new_friends=True,
    )
    available = [item for item in scanned if "accept" in item.actions]
    labels: list[str] = []
    accepted = 0
    for item in available[:limit]:
        button = item.actions["accept"]
        labels.append(_control_name(button))
        if not live:
            continue
        try:
            button.Click()
        except Exception:
            continue
        accepted += 1
        time.sleep(0.5)
    return FriendRequestResult(
        found=len(available),
        accepted=accepted,
        dry_run=not live,
        labels=tuple(labels),
    )


def _scan_friend_request_controls(
    *,
    switch_to_contacts: bool,
    root: Any | None,
    open_new_friends: bool,
    wechat: Any | None = None,
) -> list[_ScannedRequest]:
    if root is None:
        root = _prepare_wechat_contacts(
            switch_to_contacts=switch_to_contacts,
            wechat=wechat,
        )
    if open_new_friends:
        _open_new_friends_if_visible(root)
        time.sleep(0.3)

    control_paths = _walk_controls_with_ancestors(root, max_nodes=2000)
    action_rows: list[tuple[Any, list[Any], str, Any]] = []
    for control, ancestors in control_paths:
        action = _action_for_control(control)
        if not action:
            continue
        container = _request_container(control, ancestors)
        action_rows.append((container, ancestors, action, control))

    grouped: list[tuple[Any, dict[str, Any]]] = []
    by_identity: dict[int, int] = {}
    for container, _ancestors, action, control in action_rows:
        identity = id(container)
        position = by_identity.get(identity)
        if position is None:
            by_identity[identity] = len(grouped)
            grouped.append((container, {action: control}))
        else:
            grouped[position][1].setdefault(action, control)

    preliminary: list[tuple[str, str, str, Any, dict[str, Any], list[str]]] = []
    for container, actions in grouped:
        row_texts = _row_texts(container, actions.values())
        display_name = row_texts[0] if row_texts else ""
        verification_message = " | ".join(row_texts[1:])
        anchor = _control_anchor(container)
        base = "\x1f".join((anchor, display_name, verification_message))
        preliminary.append((base, display_name, verification_message, container, actions, row_texts))

    occurrences: dict[str, int] = {}
    result: list[_ScannedRequest] = []
    for base, display_name, verification_message, container, actions, row_texts in preliminary:
        occurrences[base] = occurrences.get(base, 0) + 1
        ordinal = occurrences[base]
        digest_source = f"{base}\x1f{ordinal}"
        request_id = "wxfr_" + hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:32]
        result.append(
            _ScannedRequest(
                candidate=FriendRequestCandidate(
                    external_request_id=request_id,
                    display_name=display_name,
                    verification_message=verification_message,
                    raw={
                        "row_texts": row_texts,
                        "available_actions": sorted(actions),
                        "anchor": _control_anchor(container),
                        "occurrence": ordinal,
                    },
                ),
                actions=actions,
            )
        )
    return result


def _verify_action_outcome(
    request_id: str,
    action: str,
    *,
    switch_to_contacts: bool,
    root: Any | None,
    wechat: Any | None,
) -> str:
    rescanned = _scan_friend_request_controls(
        switch_to_contacts=switch_to_contacts,
        root=root,
        open_new_friends=False,
        wechat=wechat,
    )
    matches = [item for item in rescanned if item.candidate.external_request_id == request_id]
    if not matches:
        return "request_removed_from_pending_list"
    if len(matches) > 1:
        raise FriendRequestOutcomeUncertainError(
            "friend request became ambiguous after click; outcome is uncertain"
        )
    if action not in matches[0].actions:
        return "requested_action_no_longer_available"
    raise FriendRequestOutcomeUncertainError(
        "friend request still exposes the clicked action; outcome is uncertain"
    )


def _complete_confirmation_if_present(
    action: str,
    display_name: str,
    *,
    root: Any | None,
    wechat: Any | None,
) -> str:
    context_labels = (
        CONFIRM_CONTEXT_LABELS if action == "accept" else REJECT_CONFIRM_CONTEXT_LABELS
    )
    current_root = root
    if current_root is None:
        current_root = _prepare_wechat_contacts(
            switch_to_contacts=False,
            wechat=wechat,
        )
    paths = _walk_controls_with_ancestors(current_root, max_nodes=1200)
    candidates: list[tuple[Any, list[Any]]] = []
    for control, ancestors in paths:
        if _control_name(control) in CONFIRM_LABELS and _is_button(control):
            candidates.append((control, ancestors))
    if not candidates:
        return "not_present"
    if len(candidates) != 1:
        raise FriendRequestOutcomeUncertainError(
            "multiple friend confirmation buttons are visible; outcome is uncertain"
        )
    button, ancestors = candidates[0]
    context_names: list[str] = []
    for candidate in reversed(ancestors):
        context_names = _descendant_names(candidate, max_nodes=120)
        if any(label in name for label in context_labels for name in context_names):
            break
    has_confirmation_context = any(
        label in name
        for label in context_labels
        for name in context_names
    )
    has_request_context = bool(display_name) and any(
        display_name == name or display_name in name for name in context_names
    )
    if not has_confirmation_context or (display_name and not has_request_context):
        raise FriendRequestOutcomeUncertainError(
            "friend confirmation context does not match the requested contact; no confirmation click was attempted"
        )
    try:
        button.Click()
    except Exception as exc:
        raise FriendRequestOutcomeUncertainError(
            f"failed to click friend confirmation action: {exc}"
        ) from exc
    time.sleep(0.4)
    return "clicked"


def _normalize_operation_type(value: str) -> str:
    normalized = str(value).strip().lower()
    mapping = {
        "accept": "accept",
        "accept_friend": "accept",
        "approve": "accept",
        "reject": "reject",
        "reject_friend": "reject",
    }
    try:
        return mapping[normalized]
    except KeyError as exc:
        raise ValueError("operation_type must be accept_friend or reject_friend") from exc


def _prepare_wechat_contacts(*, switch_to_contacts: bool, wechat: Any | None = None) -> Any:
    try:
        import uiautomation as auto
    except ImportError as exc:
        raise RuntimeError("wxauto4/uiautomation is required for friend request automation") from exc

    try:
        if wechat is None:
            from wxauto4 import WeChat

            wechat = WeChat(ads=False, resize=False)
        if switch_to_contacts and callable(getattr(wechat, "SwitchToContact", None)):
            wechat.SwitchToContact()
            time.sleep(0.5)
        return auto.GetForegroundControl()
    except Exception as exc:
        raise RuntimeError(f"failed to attach to WeChat contacts page: {exc}") from exc


def _open_new_friends_if_visible(root: Any) -> None:
    for control, _ancestors in _walk_controls_with_ancestors(root, max_nodes=800):
        name = _control_name(control)
        if any(label in name for label in NEW_FRIEND_LABELS):
            try:
                control.Click()
            except Exception:
                pass
            return


def _action_for_control(control: Any) -> str:
    name = _control_name(control)
    if not name or not _is_button(control):
        return ""
    if any(label == name or label in name for label in ACCEPT_LABELS):
        return "accept"
    if any(label == name or label in name for label in REJECT_LABELS):
        return "reject"
    return ""


def _is_button(control: Any) -> bool:
    control_type = str(
        getattr(control, "ControlTypeName", "")
        or getattr(control, "control_type", "")
        or ""
    )
    return not control_type or "Button" in control_type or "\u6309\u94ae" in control_type


def _request_container(control: Any, ancestors: list[Any]) -> Any:
    for candidate in reversed(ancestors):
        if _looks_like_request_row(candidate):
            return candidate
    return control


def _looks_like_request_row(control: Any) -> bool:
    control_type = str(
        getattr(control, "ControlTypeName", "")
        or getattr(control, "control_type", "")
        or ""
    ).lower()
    if any(token in control_type for token in ("listitem", "dataitem", "pane", "group")):
        return True
    names = _descendant_names(control, max_nodes=80)
    has_action = any(name in STATUS_LABELS for name in names)
    meaningful = [name for name in names if name not in STATUS_LABELS and name not in NEW_FRIEND_LABELS]
    return has_action and bool(meaningful)


def _row_texts(container: Any, action_controls: Any) -> list[str]:
    excluded_ids = {id(control) for control in action_controls}
    result: list[str] = []
    for control, _ancestors in _walk_controls_with_ancestors(container, max_nodes=120):
        if id(control) in excluded_ids:
            continue
        name = _control_name(control)
        if not name or name in STATUS_LABELS or name in NEW_FRIEND_LABELS:
            continue
        if name not in result:
            result.append(name)
    return result[:12]


def _descendant_names(control: Any, *, max_nodes: int) -> list[str]:
    return [
        name
        for item, _ancestors in _walk_controls_with_ancestors(control, max_nodes=max_nodes)
        if (name := _control_name(item))
    ]


def _walk_controls_with_ancestors(root: Any, *, max_nodes: int) -> list[tuple[Any, list[Any]]]:
    result: list[tuple[Any, list[Any]]] = []
    queue: list[tuple[Any, list[Any]]] = [(root, [])]
    visited: set[int] = set()
    while queue and len(result) < max_nodes:
        control, ancestors = queue.pop(0)
        identity = id(control)
        if identity in visited:
            continue
        visited.add(identity)
        result.append((control, ancestors))
        children: list[Any] = []
        for method_name in ("GetChildren", "get_children"):
            method = getattr(control, method_name, None)
            if callable(method):
                try:
                    children = list(method() or [])
                except Exception:
                    children = []
                break
        queue.extend((child, [*ancestors, control]) for child in children)
    return result


def _control_anchor(control: Any) -> str:
    for attr in ("AutomationId", "automation_id", "RuntimeId", "runtime_id"):
        value = getattr(control, attr, "")
        if callable(value):
            try:
                value = value()
            except Exception:
                value = ""
        if value:
            return _normalized_text(value)
    return ""


def _control_name(control: Any) -> str:
    for attr in ("Name", "name"):
        value = getattr(control, attr, "")
        if value:
            return str(value).strip()
    return ""


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


__all__ = [
    "FriendRequestActionResult",
    "FriendRequestActionError",
    "FriendRequestCandidate",
    "FriendRequestNotExecutableError",
    "FriendRequestOutcomeUncertainError",
    "FriendRequestResult",
    "accept_friend_requests",
    "execute_friend_request_action",
    "scan_friend_requests",
]
