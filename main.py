from __future__ import annotations

from core.agent.react import EventDrivenAgent
from core.guardrails.policy import RunMode
from core.runtime.config import RuntimeConfig, RuntimeConfigStore
from core.llm import OpenAICompatibleModel
from core.runtime.model_config import ModelConfigLoader
from view.cli import InnoAgentCLI


def main() -> None:
    config = RuntimeConfigStore(".").load(
        RuntimeConfig(
            workspace_root=".",
            mode=RunMode.ASK,
            max_iterations=50,
        )
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
