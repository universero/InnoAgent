"""Integration tests for subagents and runtime compaction."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import BaseModel

from core.event.events import AgentEvent
from core.agent.model_stream import ModelBatch
from core.llm import BaseModelClient, ModelDecision, OpenAICompatibleModel, ToolCallDecision
from core.agent.react import InnoAgent
from core.runtime.config import RuntimeConfig
from core.session.store import SessionRecord
from core.tool.base import BaseTool, ToolContext, ToolResult
from core.tool.registry import ToolRegistry


class _SubagentModel(BaseModelClient):
    def respond(self, context, tool_schemas, state=None, on_token=None, on_thinking=None):
        state = state or {}
        request = str(state.get("user_input") or "")
        results = state.get("tool_results") or []
        if request.startswith("delegate"):
            if results:
                return ModelDecision(action="finish", message="parent received subagent report")
            return ModelDecision(
                action="tool_use",
                tool_calls=[
                    ToolCallDecision(
                        name="subagent",
                        arguments={"task": "inspect note.txt", "allowed_tools": ["read"]},
                    )
                ],
            )
        if results:
            return ModelDecision(action="finish", message=f"subagent found: {results[-1]['output']}")
        return ModelDecision(
            action="tool_use",
            tool_calls=[ToolCallDecision(name="read", arguments={"path": "note.txt"})],
        )


class _UsageSubagentModel(_SubagentModel):
    def stream_events(self, context, tool_schemas, *, stage="main", state=None):
        yield from super().stream_events(
            context,
            tool_schemas,
            stage=stage,
            state=state,
        )
        yield AgentEvent(
            type="response.completed",
            stage=stage,
            usage={
                "input_tokens": 2,
                "output_tokens": 1,
                "total_tokens": 3,
                "cached_tokens": 1,
            },
        )


class _UsageOnlyModel(BaseModelClient):
    def respond(self, context, tool_schemas, state=None, on_token=None, on_thinking=None):
        return ModelDecision(action="finish", message="done")

    def stream_events(self, context, tool_schemas, *, stage="main", state=None):
        yield AgentEvent(
            type="item.completed",
            stage=stage,
            item_type="message",
            content="done",
            payload={"content": "done"},
        )
        yield AgentEvent(
            type="response.completed",
            stage=stage,
            usage={
                "input_tokens": 600,
                "output_tokens": 20,
                "total_tokens": 620,
                "cached_tokens": 100,
                "reasoning_tokens": 5,
            },
        )


class _BlockingInput(BaseModel):
    value: str = "work"


class _BlockingTool(BaseTool):
    name = "blocking"
    description = "Wait until the test releases the tool."
    input_model = _BlockingInput
    parallel_safe = False

    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.started = started
        self.release = release

    def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        self.started.set()
        self.release.wait(timeout=5)
        return ToolResult(tool_name=self.name, status="success", output="tool completed")


class _SteeringModel(BaseModelClient):
    def respond(self, context, tool_schemas, state=None, on_token=None, on_thinking=None):
        state = state or {}
        if state.get("tool_results"):
            correction = next(
                (
                    item["content"]
                    for item in reversed(state.get("messages", []))
                    if "执行中用户纠偏" in item.get("content", "")
                ),
                "missing",
            )
            return ModelDecision(action="finish", message=correction)
        return ModelDecision(
            action="tool_use",
            tool_calls=[ToolCallDecision(name="blocking", arguments={})],
        )


class _BoundaryModel(BaseModelClient):
    """Block the first response so tests can inject steering at a known boundary."""

    def __init__(
        self,
        started: threading.Event,
        release: threading.Event,
        *,
        first_action: str,
    ) -> None:
        self.started = started
        self.release = release
        self.first_action = first_action
        self.calls = 0

    def respond(self, context, tool_schemas, state=None, on_token=None, on_thinking=None):
        self.calls += 1
        state = state or {}
        if self.calls == 1:
            self.started.set()
            self.release.wait(timeout=5)
            if self.first_action == "tool_use":
                return ModelDecision(
                    action="tool_use",
                    tool_calls=[ToolCallDecision(name="blocking", arguments={})],
                )
            return ModelDecision(action="finish", message="original answer")
        correction = next(
            (
                item["content"]
                for item in reversed(state.get("messages", []))
                if "执行中用户纠偏" in item.get("content", "")
            ),
            "missing",
        )
        return ModelDecision(action="finish", message=correction)


class _CapturingSubagentStream:
    def __init__(self) -> None:
        self.states: list[dict] = []

    def call(self, context, state, *, stage, tool_schemas):
        self.states.append(dict(state))
        return ModelBatch(text="done")


class AdvancedRuntimeTest(unittest.TestCase):
    def _config(self, tmp: str) -> RuntimeConfig:
        return RuntimeConfig(
            workspace_root=tmp,
            profile_root=str(Path(tmp) / "profiles"),
            session_root=str(Path(tmp) / "sessions"),
            mode="auto",
            memory_enabled=False,
        )

    def test_subagent_uses_isolated_read_only_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "note.txt").write_text("evidence", encoding="utf-8")
            runtime = InnoAgent(self._config(tmp), model=_SubagentModel())
            result = runtime.invoke("delegate inspection")
            self.assertEqual(result["response"], "parent received subagent report")
            subagent = next(item for item in result["tool_results"] if item["tool_name"] == "subagent")
            self.assertIn("evidence", subagent["output"])
            self.assertTrue(
                any(
                    event.get("stage") == "subagent"
                    and event.get("item_type") == "tool_result"
                    for event in runtime._run_events
                )
            )

    def test_subagent_usage_is_included_in_session_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "note.txt").write_text("evidence", encoding="utf-8")
            runtime = InnoAgent(self._config(tmp), model=_UsageSubagentModel())
            result = runtime.invoke("delegate inspection")
            self.assertEqual(result["usage"]["total_tokens"], 12)
            self.assertEqual(result["usage"]["cached_tokens"], 4)
            self.assertEqual(result["usage"]["requests"], 4)

    def test_response_usage_is_cumulative_and_drives_context_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgent(self._config(tmp), model=_UsageOnlyModel())

            first = runtime.invoke("first request")
            second = runtime.invoke("second request", session_id=first["session_id"])

            self.assertEqual(second["usage"]["input_tokens"], 1200)
            self.assertEqual(second["usage"]["output_tokens"], 40)
            self.assertEqual(second["usage"]["cached_tokens"], 200)
            self.assertEqual(second["usage"]["reasoning_tokens"], 10)
            self.assertEqual(second["usage"]["total_tokens"], 1240)
            self.assertEqual(second["usage"]["requests"], 2)
            self.assertEqual(second["context_usage"]["used_tokens"], 600)
            self.assertEqual(second["context_usage"]["source"], "response_api")

    def test_real_response_usage_can_trigger_automatic_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            config.max_context_tokens = 1000
            config.compact_threshold = 0.5
            config.compact_keep_recent_tokens = 20
            runtime = InnoAgent(
                config,
                model=_UsageOnlyModel(),
                summarizer=lambda _: "older context",
            )
            first = runtime.invoke("old request " * 30)

            runtime.invoke("continue", session_id=first["session_id"])

            completed = [
                event
                for event in runtime._run_events
                if event["type"] == "context.compaction.completed"
            ]
            self.assertTrue(completed)
            self.assertEqual(completed[0]["payload"]["trigger"], "auto")

    def test_subagent_sends_prompt_and_role_as_runtime_context(self) -> None:
        from core.agent.subagent import SubagentRunner
        from core.config.permissions import PermissionStore

        with tempfile.TemporaryDirectory() as tmp:
            stream = _CapturingSubagentStream()
            runner = SubagentRunner(
                stream,  # type: ignore[arg-type]
                ToolRegistry(),
                self._config(tmp),
                PermissionStore(tmp),
                lambda event: None,
            )

            result = runner.run(task="inspect tests", role="reviewer", allowed_tools=[])

            self.assertEqual(result["summary"], "done")
            runtime_context = stream.states[0]["_runtime_context"]
            self.assertIn("isolated read-only subagent", runtime_context)
            self.assertIn("Role: reviewer", runtime_context)
            self.assertEqual(
                stream.states[0]["_model_messages"],
                [{"role": "user", "content": "inspect tests"}],
            )

    def test_manual_compaction_persists_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgent(
                self._config(tmp),
                model=_SubagentModel(),
                summarizer=lambda _: "compact summary",
            )
            state, session_id = runtime.new_state("seed")
            runtime.session_store.create(SessionRecord(session_id=session_id))
            state["messages"] = [
                {"role": "user", "content": "old " * 200},
                {"role": "assistant", "content": "result " * 200},
                {"role": "user", "content": "recent"},
            ]
            runtime._begin_run(session_id, "turn")
            runtime._save_session(state)
            runtime.compactor.keep_recent_tokens = 10
            compacted = runtime.compact(session_id)
            self.assertEqual(compacted["context_summary"], "compact summary")
            event = next(
                item
                for item in runtime._run_events
                if item["type"] == "context.compaction.completed"
            )
            self.assertLess(event["payload"]["compression_ratio"], 1.0)

    def test_model_update_rebinds_all_model_stages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgent(
                self._config(tmp),
                model=OpenAICompatibleModel("key", "https://example.com", "old-model"),
            )
            updated = runtime.update_model("new-model", "high")
            self.assertIs(runtime.model_stream.model, updated)
            self.assertIs(runtime.stages.model, updated)

    def test_runtime_exposes_main_plan_and_reflection_graphs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgent(self._config(tmp), model=_SubagentModel())
            main_nodes = set(runtime.graph.compiled.get_graph().nodes)
            plan_nodes = set(runtime.stages.plan_graph.get_graph().nodes)
            reflection_nodes = set(runtime.stages.reflection_graph.get_graph().nodes)
            self.assertTrue({"main_agent", "tool_batch", "reflection"} <= main_nodes)
            self.assertIn("generate_plan", plan_nodes)
            self.assertIn("evaluate_goal", reflection_nodes)

    def test_steering_is_applied_after_running_tool_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            started = threading.Event()
            release = threading.Event()
            registry = ToolRegistry()
            registry.register(_BlockingTool(started, release))
            runtime = InnoAgent(
                self._config(tmp),
                model=_SteeringModel(),
                registry=registry,
            )
            result: dict = {}

            def run_agent() -> None:
                result.update(runtime.invoke("start blocking work"))

            worker = threading.Thread(target=run_agent)
            worker.start()
            self.assertTrue(started.wait(timeout=2))
            self.assertTrue(runtime.submit_steering("改为检查新的方向"))
            release.set()
            worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertIn("改为检查新的方向", result["response"])
            event_types = [event["type"] for event in runtime._run_events]
            self.assertLess(
                event_types.index("item.completed"),
                event_types.index("steering.applied"),
            )

    def test_immediate_steering_skips_pending_tool_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            started = threading.Event()
            release = threading.Event()
            runtime = InnoAgent(
                self._config(tmp),
                model=_BoundaryModel(started, release, first_action="tool_use"),
            )
            result: dict = {}

            worker = threading.Thread(
                target=lambda: result.update(runtime.invoke("prepare a tool call"))
            )
            worker.start()
            self.assertTrue(started.wait(timeout=2))
            self.assertTrue(runtime.submit_steering("不要执行工具", delivery="immediate"))
            release.set()
            worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertIn("不要执行工具", result["response"])
            self.assertEqual(result["tool_results"], [])
            applied = next(
                event for event in runtime._run_events if event["type"] == "steering.applied"
            )
            self.assertEqual(applied["payload"]["boundary"], "immediate")

    def test_default_steering_without_tool_is_applied_before_finish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            started = threading.Event()
            release = threading.Event()
            runtime = InnoAgent(
                self._config(tmp),
                model=_BoundaryModel(started, release, first_action="finish"),
            )
            result: dict = {}

            worker = threading.Thread(
                target=lambda: result.update(runtime.invoke("answer directly"))
            )
            worker.start()
            self.assertTrue(started.wait(timeout=2))
            self.assertTrue(runtime.submit_steering("改用新的回答方向"))
            release.set()
            worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertIn("改用新的回答方向", result["response"])
            applied = next(
                event for event in runtime._run_events if event["type"] == "steering.applied"
            )
            self.assertEqual(applied["payload"]["boundary"], "before_finish")

    def test_stop_waits_for_current_tool_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            started = threading.Event()
            release = threading.Event()
            registry = ToolRegistry()
            registry.register(_BlockingTool(started, release))
            runtime = InnoAgent(
                self._config(tmp),
                model=_SteeringModel(),
                registry=registry,
            )
            result: dict = {}

            worker = threading.Thread(
                target=lambda: result.update(runtime.invoke("start blocking work"))
            )
            worker.start()
            self.assertTrue(started.wait(timeout=2))
            self.assertTrue(runtime.request_stop())
            self.assertTrue(worker.is_alive())
            release.set()
            worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(result["finish_reason"], "user_stopped")
            self.assertEqual(result["tool_results"][-1]["output"], "tool completed")
            stopped = next(
                event for event in runtime._run_events if event["type"] == "steering.stopped"
            )
            self.assertEqual(stopped["payload"]["boundary"], "after_tool")

    def test_unexpected_run_error_clears_active_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = InnoAgent(self._config(tmp), model=_SubagentModel())

            def fail_after_completed_event(state):
                runtime._emit(
                    AgentEvent(
                        type="item.completed",
                        item_type="message",
                        payload={"content": "visible before failure"},
                    )
                )
                raise RuntimeError("boom")

            with patch.object(runtime, "_run_react", side_effect=fail_after_completed_event):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    runtime.invoke("trigger failure")
            self.assertFalse(runtime.submit_steering("too late"))
            record = runtime.list_sessions(limit=1)[0]
            events = runtime.session_store.load_events(record.session_id)
            self.assertTrue(any(event["type"] == "turn.started" for event in events))
            self.assertTrue(
                any(
                    event["type"] == "item.completed"
                    and event.get("payload", {}).get("content") == "visible before failure"
                    for event in events
                )
            )
            self.assertTrue(any(event["type"] == "turn.failed" for event in events))
            self.assertTrue(any(event["type"] == "state.checkpoint" for event in events))


if __name__ == "__main__":
    unittest.main()
