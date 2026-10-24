"""HermesConfig: per-episode knobs for the Hermes Agent deployer.

Hermes is the open-source CLI agent from Nous Research.  ALE runs it
against the ``cua-verse/hermes-agent`` fork on branch ``agenthle``
which carries vision patches (MCP ``ImageContent`` -> multimodal
follow-up, tool-result truncation guards).

Auth: ``OPENROUTER_API_KEY`` routed through OpenRouter.  Provider is
configurable via ``provider`` field but defaults to ``openrouter``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass
class HermesConfig:
    """Tunables for :class:`HermesDeployer`.

    Hermes is Linux-only.  The fork carries patches for MCP ImageContent
    multimodal forwarding and tool-result truncation guards.

    Standalone config (no shared base). The episode wall-budget is
    orchestration-owned; ``timeout_s`` is no longer an agent knob.
    """

    name: ClassVar[str] = "hermes"

    # agenthle hermes_openrouter.yaml: anthropic/claude-sonnet-4.6.
    model: str = "anthropic/claude-sonnet-4.6"
    max_turns: int = -1
    """Turn cap. ``-1`` = unlimited, the project-wide convention. Hermes itself
    has no unlimited sentinel (``IterationBudget.consume`` does
    ``if used >= max_total: stop``), so the deployer translates ``-1`` (or any
    value < 0) to ``100_000`` before passing ``--max-turns`` — wall-clock is then
    the real cap."""

    # Sonnet 4.6 full 1M-token context window.
    context_length: int = 1_000_000

    # Compression settings (tuned for 1M context + multi-MB screenshots).
    compression_threshold: float = 0.85
    compression_target_ratio: float = 0.20
    compression_protect_last_n: int = 8

    # Provider routing
    provider: str = "openrouter"

    base_url: str | None = None
    """Custom OpenAI-compatible base URL. ``None`` ⇒ OpenRouter default
    (``https://openrouter.ai/api/v1``). Written to both ``~/.hermes/.env``
    (OPENROUTER_BASE_URL) and the hermes config yaml (``model.base_url``) to
    point hermes at any OpenAI-compatible gateway."""

    api_key: str | None = None
    """Literal API key, used in place of OPENROUTER_API_KEY. ``None`` ⇒ read
    OPENROUTER_API_KEY from the env. When set (``api_key: ${env:...}`` in the
    agent yaml), it travels with the serialized config — no env passthrough
    whitelist change and no collision with a real OPENROUTER_API_KEY."""

    toolsets_enabled: tuple[str, ...] | None = None
    """Optional explicit toolset allowlist; None uses the CLI defaults."""

    # Prepend a keepalive preamble to every prompt so the agent knows not
    # to end the chat process early when it schedules background work.
    keepalive_preamble: bool = True
