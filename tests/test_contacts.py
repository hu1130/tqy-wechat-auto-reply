from __future__ import annotations

import unittest

from wechat_ai.contacts import scan_channel_contacts


class FakeControl:
    def __init__(
        self,
        name: str = "",
        control_type: str = "",
        children: list["FakeControl"] | None = None,
        automation_id: str = "",
    ) -> None:
        self.Name = name
        self.ControlTypeName = control_type
        self.AutomationId = automation_id
        self.children = children or []

    def GetChildren(self) -> list["FakeControl"]:
        return self.children


class FakeScrollPattern:
    VerticallyScrollable = True
    VerticalViewSize = 50.0

    def __init__(self, owner: "FakeScrollableControl") -> None:
        self.owner = owner
        self.VerticalScrollPercent = 40.0

    def SetScrollPercent(self, horizontal: float, vertical: float, **_kwargs: object) -> bool:
        self.VerticalScrollPercent = vertical
        self.owner.page = 0 if vertical < 50 else 1
        return True


class FakeScrollableControl(FakeControl):
    def __init__(self) -> None:
        super().__init__(control_type="ListControl")
        self.page = 0
        self.pattern = FakeScrollPattern(self)
        self.pages = [
            [FakeControl("Alice", "ListItemControl", automation_id="alice")],
            [FakeControl("Bob", "ListItemControl", automation_id="bob")],
        ]

    def GetChildren(self) -> list[FakeControl]:
        return self.pages[self.page]

    def GetScrollPattern(self) -> FakeScrollPattern:
        return self.pattern


class ContactScannerTests(unittest.TestCase):
    def test_reads_contact_rows_and_ignores_navigation_and_letter_headers(self) -> None:
        root = FakeControl(
            children=[
                FakeControl("\u901a\u8baf\u5f55", "ButtonControl"),
                FakeControl("A", "ListItemControl"),
                FakeControl(
                    control_type="ListItemControl",
                    automation_id="contact-alice",
                    children=[FakeControl("Alice", "TextControl"), FakeControl("Sales", "TextControl")],
                ),
            ]
        )

        contacts = scan_channel_contacts(wechat=object(), root=root)

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["display_name"], "Alice")
        self.assertEqual(contacts[0]["remark"], "Sales")
        self.assertEqual(contacts[0]["raw"]["coverage"], "visible_automation_tree")
        self.assertEqual(contacts[0]["raw"]["identity_quality"], "automation_id")

    def test_marks_group_rows_and_does_not_merge_reused_ui_anchors(self) -> None:
        first = FakeControl("Project Group", "ListItemControl", automation_id="team@chatroom")
        duplicate = FakeControl("Renamed Group", "ListItemControl", automation_id="team@chatroom")
        root = FakeControl(children=[first, duplicate])

        contacts = scan_channel_contacts(wechat=object(), root=root)

        self.assertEqual(len(contacts), 2)
        self.assertEqual(contacts[0]["kind"], "group")
        self.assertFalse(contacts[0]["raw"]["anchor_unique"])
        self.assertNotEqual(contacts[0]["external_contact_id"], contacts[1]["external_contact_id"])

    def test_display_name_fallback_is_explicit(self) -> None:
        root = FakeControl(children=[FakeControl("Bob", "ListItemControl")])

        contacts = scan_channel_contacts(wechat=object(), root=root)

        self.assertEqual(contacts[0]["raw"]["identity_quality"], "display_name")
        self.assertTrue(contacts[0]["external_contact_id"].startswith("wxc_"))

    def test_scrolls_complete_contact_list_and_restores_position(self) -> None:
        scrollable = FakeScrollableControl()
        root = FakeControl(children=[scrollable])

        contacts = scan_channel_contacts(wechat=object(), root=root)

        self.assertEqual([item["display_name"] for item in contacts], ["Alice", "Bob"])
        self.assertTrue(all(item["raw"]["coverage"] == "full_scroll_pattern" for item in contacts))
        self.assertEqual(scrollable.pattern.VerticalScrollPercent, 40.0)


if __name__ == "__main__":
    unittest.main()
