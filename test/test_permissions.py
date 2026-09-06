"""Permission persistence and approval-flow tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.config.permissions import PermissionStore
from core.runtime.agent import InnoAgentRuntime
from core.runtime.config import RuntimeConfig
from core.tool.base import ToolContext
from core.tool.registry import ToolRegistry
from core.tool.shell_tool import ShellTool
from test.fakes import FakeModel


class PermissionTest(unittest.TestCase):
    def test_repository_rule_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PermissionStore(tmp)
            store.allow("shell", {"command": "git status"})
            reloaded = PermissionStore(tmp)
            self.assertEqual(reloaded.decision("shell", {"command": "git status"}), "allow")
            self.assertIsNone(reloaded.decision("shell", {"command": "git reset --hard"}))

    def test_allow_always_applies_to_new_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = RuntimeConfig(
                workspace_root=tmp,
                profile_root=str(Path(tmp) / "profiles"),
                session_root=str(Path(tmp) / "sessions"),
                mode="ask",
                memory_enabled=False,
            )
            runtime = InnoAgentRuntime(config, model=FakeModel())
            pending = runtime.invoke("创建 note.txt 内容 hello")
            self.assertTrue(pending.get("pending_confirmation"))
            runtime.resolve_approval(pending["session_id"], "allow_always")

            second = InnoAgentRuntime(config, model=FakeModel())
            result = second.invoke("创建 note.txt 内容 hello")
            self.assertFalse(result.get("pending_confirmation"))
            self.assertEqual((Path(tmp) / "note.txt").read_text(), "hello")

    def test_deny_does_not_execute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = RuntimeConfig(
                workspace_root=tmp,
                profile_root=str(Path(tmp) / "profiles"),
                session_root=str(Path(tmp) / "sessions"),
                mode="ask",
                memory_enabled=False,
            )
            runtime = InnoAgentRuntime(config, model=FakeModel())
            pending = runtime.invoke("写入 denied.txt 内容 no")
            result = runtime.resolve_approval(pending["session_id"], "deny")
            self.assertFalse((Path(tmp) / "denied.txt").exists())
            self.assertTrue(any(item["status"] == "blocked" for item in result["tool_results"]))

    def test_shell_requires_approval_in_ask_mode(self) -> None:
        registry = ToolRegistry()
        registry.set_default_guardrails()
        registry.register(ShellTool())
        with tempfile.TemporaryDirectory() as tmp:
            result = registry.execute_tool(
                "shell",
                {"command": "printf hello"},
                ToolContext(mode="ask", allowed_roots=[tmp], permission_store=PermissionStore(tmp)),
            )
            self.assertEqual(result.status, "needs_confirmation")


if __name__ == "__main__":
    unittest.main()
