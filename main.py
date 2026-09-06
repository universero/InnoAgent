"""InnoAgent command-line entry point."""

from __future__ import annotations

from core.agent.react import EventDrivenAgent
from core.runtime.config import RuntimeConfig
from core.llm import OpenAICompatibleModel
from core.runtime.model_config import ModelConfigLoader
from view.cli import InnoAgentCLI


def main() -> None:
    """Load model config, build the runtime and start the CLI."""
    config = RuntimeConfig(
        workspace_root=".",
        mode="ask",
        max_iterations=20,
    )
    model_config = ModelConfigLoader(project_root=config.workspace_root).load()
    model = OpenAICompatibleModel(
        api_key=model_config.api_key,
        base_url=model_config.base_url,
        model=model_config.model,
        reasoning_effort=model_config.reasoning_effort,
    )
    runtime = EventDrivenAgent(
        config=config,
        model=model,
        model_config=model_config,
        model_config_loader=ModelConfigLoader(project_root=config.workspace_root),
    )
    cli = InnoAgentCLI(runtime)
    cli.run()


if __name__ == "__main__":
    main()
