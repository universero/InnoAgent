"""InnoAgent command-line entry point."""

from __future__ import annotations

import sys

from core.agent.react import EventDrivenAgent
from core.runtime.config import RuntimeConfig
from core.llm import OpenAICompatibleModel
from core.runtime.model_config import ModelConfigLoader
from view.cli import InnoAgentCLI
from view.render import render_event


def main() -> None:
    """Load model config, build the runtime and start the CLI."""
    config = RuntimeConfig(
        workspace_root=".",
        mode="auto",
        max_iterations=20,
    )
    model_config = ModelConfigLoader(project_root=config.workspace_root).load()
    model = OpenAICompatibleModel(
        api_key=model_config.api_key,
        base_url=model_config.base_url,
        model=model_config.model,
        reasoning_effort=model_config.reasoning_effort,
    )
    pending_stream: list[str] = []

    def flush_stream() -> None:
        """Flush buffered reasoning/text delta content with a trailing newline."""
        if not pending_stream:
            return
        text = "".join(pending_stream)
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
        pending_stream.clear()

    def stream_event(event) -> None:
        """Render structured runtime events in order."""
        if event.get("type") in {"text.delta", "reasoning.delta"}:
            pending_stream.append(str(event.get("content", "")))
            return
        flush_stream()
        rendered = render_event(event)
        if rendered:
            print(rendered)

    runtime = EventDrivenAgent(
        config=config,
        model=model,
        stream_handler=stream_event,
        model_config=model_config,
        model_config_loader=ModelConfigLoader(project_root=config.workspace_root),
    )
    cli = InnoAgentCLI(runtime)
    cli.run()


if __name__ == "__main__":
    main()
