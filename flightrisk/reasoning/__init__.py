"""LLM reasoning backends for match confirmation.

Cloud backends are opt-in. They send the detected person's crop and the
missing child's reference photo off the device, so "claude" is refused unless
cloud use is explicitly allowed (``FLIGHTRISK_ALLOW_CLOUD_LLM=1``) and an API
key is configured. The shipped default is the local Gemma backend.
"""

from flightrisk.config import get_config

PROVIDERS = ("claude", "gemma", "off")
CLOUD_PROVIDERS = frozenset({"claude"})
CLOUD_OPT_IN_ENV = "FLIGHTRISK_ALLOW_CLOUD_LLM"


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


def cloud_llm_allowed() -> bool:
    """True only when the operator explicitly opted in to cloud reasoning."""
    return bool(get_config().reasoning.allow_cloud)


def cloud_block_reason(provider: str) -> str | None:
    """Why a cloud ``provider`` is refused right now, or None if it is not blocked."""
    if provider in CLOUD_PROVIDERS and not cloud_llm_allowed():
        return (
            "Cloud reasoning is not enabled. Claude would send person crops and the "
            f"reference photo to Anthropic; set {CLOUD_OPT_IN_ENV}=1 to opt in."
        )
    return None


def provider_unavailable_reason(provider: str) -> str | None:
    """Why ``provider`` can't be selected right now (None if it can).

    Cheap enough to call on every status update: it never contacts a backend.
    """
    reason = cloud_block_reason(provider)
    if reason is None and provider == "claude":
        from flightrisk.reasoning.claude_agent import API_KEY_ENV, resolve_api_key
        if not resolve_api_key():
            reason = f"{API_KEY_ENV} is not set."
    return reason


def create_reasoning_agent(provider: str | None = None):
    """Build the reasoning agent for ``provider`` (default: config).

    Returns None for "off". Raises RuntimeError when the chosen backend can't
    be used (cloud use not opted in, missing API key, Ollama model not
    pulled, ...) so callers never hold an agent that can't answer, which would
    leave matches "pending".
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
