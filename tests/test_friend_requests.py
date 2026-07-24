from __future__ import annotations

import unittest

from wechat_ai.friend_requests import (
    FriendRequestNotExecutableError,
    FriendRequestOutcomeUncertainError,
    accept_friend_requests,
    execute_friend_request_action,
    scan_friend_requests,
)


class FakeControl:
    def __init__(
        self,
        name: str = "",
        control_type: str = "",
        children: list["FakeControl"] | None = None,
        *,
        automation_id: str = "",
    ) -> None:
        self.Name = name
        self.ControlTypeName = control_type
        self.children = children or []
        self.AutomationId = automation_id
        self.clicked = 0
        self.on_click = None

    def GetChildren(self) -> list["FakeControl"]:
        return self.children

    def Click(self) -> None:
        self.clicked += 1
        if self.on_click is not None:
            self.on_click()


def request_row(name: str, message: str, *, automation_id: str = "") -> tuple[FakeControl, FakeControl, FakeControl]:
    accept = FakeControl("\u63a5\u53d7", "ButtonControl")
    reject = FakeControl("\u62d2\u7edd", "ButtonControl")
    row = FakeControl(
        control_type="ListItemControl",
        automation_id=automation_id,
        children=[
            FakeControl(name, "TextControl"),
            FakeControl(message, "TextControl"),
            accept,
            reject,
        ],
    )
    return row, accept, reject


class FriendRequestTests(unittest.TestCase):
    def test_scan_returns_stable_identity_and_request_details(self) -> None:
        row, _accept, _reject = request_row("Alice", "I am from sales", automation_id="row-1")
        root = FakeControl(children=[row])

        first = scan_friend_requests(root=root)
        second = scan_friend_requests(root=root, open_new_friends=False)

        self.assertEqual(first, second)
        self.assertEqual(first[0].display_name, "Alice")
        self.assertEqual(first[0].verification_message, "I am from sales")
        self.assertEqual(first[0].raw["available_actions"], ["accept", "reject"])
        self.assertTrue(first[0].external_request_id.startswith("wxfr_"))

    def test_exact_accept_rescans_and_verifies_action_disappeared(self) -> None:
        row, accept, _reject = request_row("Alice", "hello", automation_id="row-1")
        root = FakeControl(children=[row])
        request_id = scan_friend_requests(root=root)[0].external_request_id
        accept.on_click = lambda: root.children.clear()

        result = execute_friend_request_action(
            request_id,
            "accept_friend",
            root=root,
            verify_delay_seconds=0,
        )

        self.assertEqual(accept.clicked, 1)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.evidence["verification"], "request_removed_from_pending_list")

    def test_accept_completes_unique_matching_secondary_confirmation(self) -> None:
        row, accept, _reject = request_row("Alice", "hello", automation_id="row-1")
        root = FakeControl(children=[row])
        request_id = scan_friend_requests(root=root)[0].external_request_id
        done = FakeControl("\u5b8c\u6210", "ButtonControl")
        dialog = FakeControl(
            control_type="PaneControl",
            children=[
                FakeControl("\u6dfb\u52a0\u5230\u901a\u8baf\u5f55", "TextControl"),
                FakeControl("Alice", "TextControl"),
                done,
            ],
        )
        accept.on_click = lambda: setattr(root, "children", [dialog])
        done.on_click = lambda: root.children.clear()

        result = execute_friend_request_action(
            request_id,
            "accept_friend",
            root=root,
            verify_delay_seconds=0,
        )

        self.assertEqual((accept.clicked, done.clicked), (1, 1))
        self.assertEqual(result.evidence["confirmation"], "clicked")

    def test_secondary_confirmation_never_clicks_when_contact_context_mismatches(self) -> None:
        row, accept, _reject = request_row("Alice", "hello", automation_id="row-1")
        root = FakeControl(children=[row])
        request_id = scan_friend_requests(root=root)[0].external_request_id
        done = FakeControl("\u5b8c\u6210", "ButtonControl")
        dialog = FakeControl(
            control_type="PaneControl",
            children=[
                FakeControl("\u6dfb\u52a0\u5230\u901a\u8baf\u5f55", "TextControl"),
                FakeControl("Mallory", "TextControl"),
                done,
            ],
        )
        accept.on_click = lambda: setattr(root, "children", [dialog])

        with self.assertRaisesRegex(FriendRequestOutcomeUncertainError, "does not match"):
            execute_friend_request_action(
                request_id,
                "accept_friend",
                root=root,
                verify_delay_seconds=0,
            )

        self.assertEqual(accept.clicked, 1)
        self.assertEqual(done.clicked, 0)

    def test_reject_completes_unique_matching_confirmation(self) -> None:
        row, _accept, reject = request_row("Alice", "hello", automation_id="row-1")
        root = FakeControl(children=[row])
        request_id = scan_friend_requests(root=root)[0].external_request_id
        confirm = FakeControl("\u786e\u5b9a", "ButtonControl")
        dialog = FakeControl(
            control_type="PaneControl",
            children=[
                FakeControl("\u786e\u8ba4\u62d2\u7edd", "TextControl"),
                FakeControl("Alice", "TextControl"),
                confirm,
            ],
        )
        reject.on_click = lambda: setattr(root, "children", [dialog])
        confirm.on_click = lambda: root.children.clear()

        result = execute_friend_request_action(
            request_id,
            "reject_friend",
            root=root,
            verify_delay_seconds=0,
        )

        self.assertEqual((reject.clicked, confirm.clicked), (1, 1))
        self.assertEqual(result.evidence["confirmation"], "clicked")

    def test_exact_action_never_clicks_when_request_is_stale(self) -> None:
        first_row, first_accept, _ = request_row("Alice", "hello", automation_id="row-1")
        root = FakeControl(children=[first_row])
        request_id = scan_friend_requests(root=root)[0].external_request_id
        other_row, other_accept, _ = request_row("Bob", "hello", automation_id="row-2")
        root.children = [other_row]

        with self.assertRaisesRegex(FriendRequestNotExecutableError, "no longer visible"):
            execute_friend_request_action(request_id, "accept_friend", root=root, verify_delay_seconds=0)

        self.assertEqual(first_accept.clicked, 0)
        self.assertEqual(other_accept.clicked, 0)

    def test_unchanged_action_after_click_is_uncertain(self) -> None:
        row, accept, _ = request_row("Alice", "hello", automation_id="row-1")
        root = FakeControl(children=[row])
        request_id = scan_friend_requests(root=root)[0].external_request_id

        with self.assertRaisesRegex(FriendRequestOutcomeUncertainError, "outcome is uncertain"):
            execute_friend_request_action(request_id, "accept_friend", root=root, verify_delay_seconds=0)

        self.assertEqual(accept.clicked, 1)

    def test_manual_compatibility_helper_respects_dry_run_and_limit(self) -> None:
        first_row, first, _ = request_row("Alice", "one")
        second_row, second, _ = request_row("Bob", "two")
        root = FakeControl(children=[first_row, second_row])

        dry_run = accept_friend_requests(live=False, root=root)
        live = accept_friend_requests(live=True, limit=1, root=root)

        self.assertEqual(dry_run.found, 2)
        self.assertEqual(dry_run.accepted, 0)
        self.assertEqual(live.accepted, 1)
        self.assertEqual(first.clicked, 1)
        self.assertEqual(second.clicked, 0)


if __name__ == "__main__":
    unittest.main()
