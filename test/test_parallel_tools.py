"""Parallel tool batch execution tests."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest

from pydantic import BaseModel

from core.tool.base import BaseTool, ToolContext, ToolResult
from core.tool.read_tool import ReadTool
from core.tool.registry import ToolRegistry
from core.tool.write_tool import WriteTool


class _Input(BaseModel):
    value: str


class _ProbeTool(BaseTool):
    name = "probe"
    description = "parallel probe"
    input_model = _Input
    active = 0
    peak = 0
    lock = threading.Lock()

    def run(self, tool_input, context):
        with self.lock:
            type(self).active += 1
            type(self).peak = max(type(self).peak, type(self).active)
        time.sleep(0.05)
        with self.lock:
            type(self).active -= 1
        return ToolResult(tool_name=self.name, status="success", output=tool_input.value)


class ParallelToolTest(unittest.TestCase):
    def test_parallel_safe_tools_overlap_and_preserve_order(self) -> None:
        _ProbeTool.active = 0
        _ProbeTool.peak = 0
        registry = ToolRegistry()
        registry.register(_ProbeTool())
        with tempfile.TemporaryDirectory() as tmp:
            results = registry.execute_many(
                [
                    {"name": "probe", "arguments": {"value": "first"}},
                    {"name": "probe", "arguments": {"value": "second"}},
                ],
                lambda: ToolContext(mode="auto", allowed_roots=[tmp]),
                max_workers=2,
            )
        self.assertEqual(_ProbeTool.peak, 2)
        self.assertEqual([result.output for result in results], ["first", "second"])

    def test_write_is_an_execution_barrier_for_later_reads(self) -> None:
        registry = ToolRegistry()
        registry.register(WriteTool())
        registry.register(ReadTool())
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/value.txt"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("old")

            results = registry.execute_many(
                [
                    {"name": "write", "arguments": {"path": path, "content": "new"}},
                    {"name": "read", "arguments": {"path": path}},
                ],
                lambda: ToolContext(mode="auto", allowed_roots=[tmp]),
                max_workers=2,
            )

        self.assertEqual([result.status for result in results], ["success", "success"])
        self.assertEqual(results[1].output, "new")


if __name__ == "__main__":
    unittest.main()
