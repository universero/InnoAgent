"""Responses API streaming parser tests."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from core.llm import OpenAICompatibleModel


class _FakeStreamResponse:
    """Minimal fake HTTP stream for testing SSE parsing."""

    def __init__(self, lines: list[str]) -> None:
        """Store SSE lines."""
        self.lines = lines

    def raise_for_status(self) -> None:
        """Simulate a successful HTTP status check."""
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def iter_lines(self):
        """Yield the configured SSE lines."""
        yield from self.lines


class StreamingModelTest(unittest.TestCase):
    """Tests for streamed text, tool calls and reasoning."""

    def test_content_is_streamed_to_callback(self) -> None:
        """Verify normal text is streamed."""
        lines = [
            'data: {"type":"response.output_text.delta","delta":"你"}',
            'data: {"type":"response.output_text.delta","delta":"好"}',
            'data: {"type":"response.completed"}',
        ]
        tokens: list[str] = []
        with patch("core.llm.responses.httpx.stream", return_value=_FakeStreamResponse(lines)):
            model = OpenAICompatibleModel("key", "https://example.com/v1", "test")
            decision = model.respond(
                "hello",
                [],
                on_token=tokens.append,
            )
        self.assertEqual(decision.message, "你好")
        self.assertEqual(tokens, ["你", "好"])
        self.assertEqual(decision.action, "finish")

    def test_streamed_tool_call_arguments_are_accumulated(self) -> None:
        """Verify streamed function-call arguments are parsed."""
        lines = [
            'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","name":"write"}}',
            'data: {"type":"response.function_call_arguments.delta","output_index":0,"delta":"{\\"path\\":\\"a.txt\\",\\"content\\":\\"hi\\"}"}',
            'data: {"type":"response.function_call_arguments.done","output_index":0,"arguments":"{\\"path\\":\\"a.txt\\",\\"content\\":\\"hi\\"}"}',
            'data: {"type":"response.completed"}',
        ]
        with patch("core.llm.responses.httpx.stream", return_value=_FakeStreamResponse(lines)):
            model = OpenAICompatibleModel("key", "https://example.com/v1", "test")
            decision = model.respond("write a file", [])
        self.assertEqual(decision.action, "tool_use")
        self.assertEqual(len(decision.tool_calls), 1)
        self.assertEqual(decision.tool_calls[0].name, "write")
        self.assertEqual(decision.tool_calls[0].arguments["path"], "a.txt")

    def test_json_content_is_parsed_and_not_streamed_as_raw_text(self) -> None:
        """Verify JSON content is parsed instead of printed."""
        json_payload = json.dumps(
            {"action": "finish", "message": "哈哈，我还不困呢！"},
            ensure_ascii=False,
        )
        lines = [
            "data: "
            + json.dumps(
                {"type": "response.output_text.delta", "delta": json_payload},
                ensure_ascii=False,
            ),
            'data: {"type":"response.completed"}',
        ]
        tokens: list[str] = []
        with patch("core.llm.responses.httpx.stream", return_value=_FakeStreamResponse(lines)):
            model = OpenAICompatibleModel("key", "https://example.com/v1", "test")
            decision = model.respond("hello", [], on_token=tokens.append)
        self.assertEqual(decision.message, "哈哈，我还不困呢！")
        self.assertEqual(decision.action, "finish")
        self.assertEqual(tokens, [])

    def test_completed_event_fallback_produces_content(self) -> None:
        """Verify completed-event fallback produces text."""
        lines = [
            "data: "
            + json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "its a test"}],
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
        ]
        tokens: list[str] = []
        with patch("core.llm.responses.httpx.stream", return_value=_FakeStreamResponse(lines)):
            model = OpenAICompatibleModel("key", "https://example.com/v1", "test")
            decision = model.respond("read test.md", [], on_token=tokens.append)
        self.assertEqual(decision.message, "its a test")
        self.assertEqual(tokens, ["its a test"])

    def test_reasoning_delta_is_sent_to_thinking_callback(self) -> None:
        """Verify reasoning deltas are sent to the thinking callback."""
        lines = [
            'data: {"type":"response.reasoning_text.delta","delta":"先读文件"}',
            'data: {"type":"response.reasoning_text.delta","delta":"，然后回答"}',
            'data: {"type":"response.completed"}',
        ]
        thinking: list[str] = []
        with patch("core.llm.responses.httpx.stream", return_value=_FakeStreamResponse(lines)):
            model = OpenAICompatibleModel("key", "https://example.com/v1", "test")
            model.respond("read test.md", [], on_thinking=thinking.append)
        self.assertEqual(thinking, ["先读文件", "，然后回答"])


if __name__ == "__main__":
    unittest.main()
