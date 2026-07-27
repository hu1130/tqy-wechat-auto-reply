from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re
import unittest

from wechat_ai.center_admin_web import read_center_admin_asset


class _IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.append(str(attributes["id"]))


class CenterAdminWebTests(unittest.TestCase):
    def test_admin_root_returns_utf8_index(self) -> None:
        content_type, body = read_center_admin_asset("/admin?from=test")

        self.assertEqual(content_type, "text/html; charset=utf-8")
        decoded = body.decode("utf-8")
        self.assertIn("微信客服中心", decoded)
        self.assertIn('id="login-form"', decoded)

    def test_admin_index_rewrites_assets_for_forwarded_prefix(self) -> None:
        _, body = read_center_admin_asset(
            "/admin",
            public_prefix="/wechat-center",
        )

        decoded = body.decode("utf-8")
        self.assertIn('href="/wechat-center/admin/app.css"', decoded)
        self.assertIn('src="/wechat-center/admin/app.js"', decoded)
        self.assertNotIn('href="/admin/app.css"', decoded)

    def test_known_assets_have_explicit_mime_types(self) -> None:
        css_type, css = read_center_admin_asset("/admin/app.css")
        js_type, js = read_center_admin_asset("app.js")

        self.assertEqual(css_type, "text/css; charset=utf-8")
        self.assertEqual(js_type, "text/javascript; charset=utf-8")
        self.assertIn(b"--color-ink", css)
        self.assertIn(b"sessionStorage", js)
        self.assertNotIn(b"localStorage", js)

    def test_frontend_declares_all_required_api_routes(self) -> None:
        script = read_center_admin_asset("/admin/app.js")[1].decode("utf-8")

        self.assertIn("function defaultCenterUrl()", script)
        self.assertIn('const adminMarker = "/admin"', script)
        for route in (
            "/v1/session",
            "/v1/tenant",
            "/v1/members",
            "/v1/channel-accounts",
            "/v1/agents",
            "/v1/messages",
            "/v1/reply-jobs",
            "/v1/deliveries",
            "/v1/contacts",
            "/v1/contacts/policies/batch",
            "/v1/friend-requests",
            "/v1/friend-requests/approve-selected",
            "/v1/friend-requests/approve-batch",
            "/v1/friend-requests/approve-all",
            "/v1/friend-requests/reject-selected",
            "/v1/operation-jobs",
            "/v1/operation-limits?channel_account_id=",
            "/v1/bot-profile",
            "/v1/knowledge-base",
            "/v1/ai-credentials/",
            "/resolve",
            "/v1/audit",
        ):
            self.assertIn(route, script)

    def test_index_contains_all_operational_views(self) -> None:
        page = read_center_admin_asset("/admin/index.html")[1].decode("utf-8")

        for view in (
            "overview",
            "messages",
            "reply-jobs",
            "deliveries",
            "contacts",
            "friend-requests",
            "operation-jobs",
            "operation-settings",
            "settings",
            "audit",
        ):
            self.assertEqual(page.count(f'data-view-panel="{view}"'), 1)
        self.assertIn('id="delivery-form"', page)
        self.assertIn('id="resolve-form"', page)
        self.assertIn('id="profile-form"', page)
        self.assertIn('id="knowledge-form"', page)
        self.assertIn('id="credential-form"', page)

    def test_wechat_operations_have_complete_controls(self) -> None:
        page = read_center_admin_asset("/admin/index.html")[1].decode("utf-8")

        for element_id in (
            "contact-search",
            "contact-type-filter",
            "contact-policy-filter",
            "contact-batch-form",
            "contact-batch-welcome",
            "friend-request-channel",
            "friend-approve-selected",
            "friend-reject-selected",
            "friend-approve-batch",
            "friend-approve-all",
            "friend-request-welcome",
            "friend-request-auto-reply",
            "operation-job-status",
            "operation-resolve-dialog",
            "operation-limits-form",
            "operation-paused",
            "operation-batch-size",
            "operation-min-interval",
            "operation-max-hour",
            "operation-max-day",
            "operation-welcome-message",
            "operation-auto-enable",
            "operation-pause",
            "operation-resume",
            "confirm-dialog",
        ):
            self.assertIn(f'id="{element_id}"', page)

        for policy in ("ai_auto", "manual", "paused", "blocked"):
            self.assertIn(f'value="{policy}"', page)

    def test_html_ids_are_unique(self) -> None:
        page = read_center_admin_asset("/admin/index.html")[1].decode("utf-8")
        parser = _IdCollector()
        parser.feed(page)

        duplicates = sorted({item for item in parser.ids if parser.ids.count(item) > 1})
        self.assertEqual(duplicates, [])

    def test_frontend_uses_safe_dom_and_idempotent_writes(self) -> None:
        script = read_center_admin_asset("/admin/app.js")[1].decode("utf-8")

        for unsafe_api in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(unsafe_api, script)
        self.assertNotIn("window.confirm", script)

        write_count = len(re.findall(r'method: "(?:POST|PUT|PATCH|DELETE)"', script))
        idempotency_count = script.count('"Idempotency-Key":')
        self.assertGreater(write_count, 0)
        self.assertEqual(idempotency_count, write_count)

    def test_operations_have_loading_empty_error_and_mobile_layout(self) -> None:
        page = read_center_admin_asset("/admin/index.html")[1].decode("utf-8")
        script = read_center_admin_asset("/admin/app.js")[1].decode("utf-8")
        css = read_center_admin_asset("/admin/app.css")[1].decode("utf-8")

        for view in ("contacts", "friend-requests", "operation-jobs", "operation-settings"):
            start = page.index(f'id="view-{view}"')
            end = page.find('<section id="view-', start + 1)
            panel = page[start : end if end >= 0 else len(page)]
            self.assertIn('class="view-state"', panel)
            self.assertIn(f'setViewState("{view}", "loading"', script)
        self.assertIn('setViewState("contacts", "empty"', script)
        self.assertIn('setViewState("friend-requests", "empty"', script)
        self.assertIn('setViewState("operation-jobs", state.operationJobs.length ? "" : "empty"', script)
        self.assertIn('setViewState("operation-settings", "empty"', script)
        self.assertIn('@media (max-width: 720px)', css)
        self.assertIn('.operations-table { min-width:', css)
        self.assertIn('.table-wrap { overflow-x: auto;', css)

    def test_wechat_operation_contract_matches_backend(self) -> None:
        script = read_center_admin_asset("/admin/app.js")[1].decode("utf-8")
        friend_action = script.split("async function runFriendRequestAction", 1)[1].split(
            "async function loadOperationJobs", 1
        )[0]
        operation_labels = script.split("function operationTypeLabel", 1)[1].split(
            "function renderOperationJobs", 1
        )[0]

        for operation_type in ("accept_friend", "reject_friend", "send_welcome"):
            self.assertIn(f'{operation_type}:', operation_labels)
        for obsolete_type in (
            "approve_friend_request",
            "reject_friend_request",
            "send_welcome_message:",
        ):
            self.assertNotIn(obsolete_type, operation_labels)
        for field in ("send_welcome_message", "welcome_message", "auto_enable_reply"):
            self.assertIn(f"{field}:", friend_action)
        self.assertIn("/v1/operation-limits?channel_account_id=", friend_action)
        self.assertIn('["failed", "cancelled"].includes(status)', script)
        self.assertIn("function friendRequestActionable", script)

    def test_operational_chinese_copy_is_valid_utf8(self) -> None:
        page_bytes = read_center_admin_asset("/admin/index.html")[1]
        script_bytes = read_center_admin_asset("/admin/app.js")[1]
        page = page_bytes.decode("utf-8")
        script = script_bytes.decode("utf-8")

        for label in ("联系人策略", "好友申请", "运营任务", "运营设置", "全部加入受限队列"):
            self.assertIn(label, page)
        for label in ("通过好友申请", "拒绝好友申请", "发送欢迎语", "人工确认"):
            self.assertIn(label, script)
        self.assertNotIn("\ufffd", page)
        self.assertNotIn("\ufffd", script)

    def test_unknown_and_nested_assets_are_rejected(self) -> None:
        for target in (
            "/admin/missing.js",
            "/admin/assets/app.js",
            "/outside/index.html",
        ):
            with self.subTest(target=target):
                with self.assertRaises(FileNotFoundError):
                    read_center_admin_asset(target)

    def test_directory_traversal_is_rejected_before_disk_access(self) -> None:
        for target in (
            "/admin/../center_server.py",
            "/admin/%2e%2e/center_server.py",
            "/admin/%2E%2E%2Fcenter_server.py",
            "/admin/..%5ccenter_server.py",
            "../index.html",
        ):
            with self.subTest(target=target):
                with self.assertRaises(FileNotFoundError):
                    read_center_admin_asset(target)

    def test_module_does_not_expose_arbitrary_project_files(self) -> None:
        project_file = Path(__file__).resolve().parents[1] / "README.md"
        self.assertTrue(project_file.exists())

        with self.assertRaises(FileNotFoundError):
            read_center_admin_asset(str(project_file))


if __name__ == "__main__":
    unittest.main()
