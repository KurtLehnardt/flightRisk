"""LLM reasoning backends for match confirmation."""

from flightrisk.config import get_config

PROVIDERS = ("claude", "gemma", "off")


def normalize_provider(provider: str | None) -> str:
    """Return a canonical provider name, defaulting to the configured one."""
    name = (provider if provider is not None else get_config().reasoning.provider)
    name = (name or "off").strip().lower()
    if name in ("ollama",):
        name = "gemma"
    if name in ("none", "disabled", ""):
        name = "off"
    if name not in PROVIDERS:
        raise ValueError(f"Unknown reasoning provider {name!r}; choose from {', '.join(PROVIDERS)}")
    return name


def create_reasoning_agent(provider: str | None = None):
    """Build the reasoning agent for ``provider`` (default: config).

    Returns None for "off". Raises RuntimeError when the chosen backend can't
    be used (missing API key, Ollama model not pulled, ...) so callers never
    hold an agent that can't answer, which would leave matches "pending".
    """
    name = normalize_provider(provider)
    if name == "off":
        return None
    if name == "claude":
        from flightrisk.reasoning.claude_agent import ClaudeReasoningAgent
        return ClaudeReasoningAgent()

    from flightrisk.reasoning.agent import FlightRiskAgent
    agent = FlightRiskAgent(model=get_config().reasoning.model)
    if not agent._available:
        raise RuntimeError(
            f"Gemma model {agent.model!r} is not available in Ollama (run: ollama pull {agent.model})"
        )
    return agent
