from __future__ import annotations

from core.agent.react import EventDrivenAgent
from core.guardrails.policy import RunMode
from core.runtime.config import ConfigStore, RuntimeConfig
from core.llm import OpenAICompatibleModel
from view.cli import InnoAgentCLI


def main() -> None:
    config = ConfigStore(".").load(
        RuntimeConfig(
            workspace_root=".",
            mode=RunMode.ASK,
            max_iterations=150,
        )
    )
    model = OpenAICompatibleModel(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        reasoning_effort=config.reasoning_effort,
    )
    runtime = EventDrivenAgent(
        config=config,
        model=model,
    )
    cli = InnoAgentCLI(runtime)
    cli.run()


if __name__ == "__main__":
    main()
