"""Structured approval protocol tests."""

from __future__ import annotations

import unittest

from core.tool.approval import ApprovalRequest, parse_approval_decision


class ApprovalProtocolTest(unittest.TestCase):
    def test_request_contains_stable_options_and_call_ids(self) -> None:
        request = ApprovalRequest.from_calls(
            [
                {
                    "call_id": "call-1",
                    "name": "write",
                    "arguments": {"path": "app.py", "content": "x"},
                }
            ],
            deferred_calls=[
                {
                    "call_id": "call-2",
                    "name": "read",
                    "arguments": {"path": "app.py"},
                }
            ],
            reason="write requires approval",
        )

        self.assertTrue(request.request_id)
        self.assertEqual(request.calls[0].call_id, "call-1")
        self.assertEqual(request.deferred_calls[0].call_id, "call-2")
        self.assertEqual(
            [option.value for option in request.options],
            ["allow_once", "allow_always", "deny"],
        )
        self.assertTrue(request.accepts("deny"))
        self.assertFalse(request.accepts("unexpected"))

    def test_legacy_option_values_are_upgraded(self) -> None:
        request = ApprovalRequest.model_validate(
            {
                "tool_name": "write",
                "arguments": {"path": "app.py"},
                "calls": [{"id": "legacy-1", "name": "write", "arguments": {}}],
                "options": ["allow_once", "allow_always", "deny"],
            }
        )

        self.assertEqual(request.calls[0].call_id, "legacy-1")
        self.assertEqual(request.options[1].label, "Always allow in this workspace")

    def test_legacy_payload_builds_a_resumable_call(self) -> None:
        request = ApprovalRequest.model_validate(
            {
                "tool_name": "write",
                "metadata": {"arguments": {"path": "app.py", "content": "x"}},
                "options": ["allow_once", "allow_always", "deny"],
            }
        )

        self.assertEqual(request.arguments["path"], "app.py")
        self.assertEqual(request.calls[0].name, "write")
        self.assertEqual(request.calls[0].arguments, request.arguments)

    def test_cli_aliases_are_normalized(self) -> None:
        self.assertEqual(parse_approval_decision("/approve once"), "allow_once")
        self.assertEqual(parse_approval_decision("一直允许"), "allow_always")
        self.assertIsNone(parse_approval_decision("anything else"))


if __name__ == "__main__":
    unittest.main()
