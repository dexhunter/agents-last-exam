"""DroidConfig: per-episode knobs for the Factory.ai ``droid`` CLI deployer.

Factory authentication uses ``FACTORY_API_KEY`` or an authenticated CLI
session. Model calls use the provider key configured in ``settings.json``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

# Deny-only tool policy: only disabled tools are listed (no allow list). WebSearch
# is intentionally NOT disabled — internet is allowed, so web access is on.
_DISABLED_TOOLS = (
    "squad-board",
    "slack_post_message",
    "store_agent_readiness_report",
    "GenerateDroid",
    "ProposeMission",
    "StartMissionRun",
    "EndFeatureRun",
    "DismissHandoffItems",
    "ExitSpecMode",
    "AskUser",
)


@dataclass
class DroidConfig:
    """Tunables for :class:`DroidDeployer`.

    Standalone config (no shared base). The episode wall-budget is
    orchestration-owned; ``timeout_s`` is no longer an agent knob.
    """

    name: ClassVar[str] = "droid"

    # agenthle droid_openrouter_claude_opus_4_7.yaml: anthropic/claude-opus-4.7
    # (alt droid_openrouter_gpt5_5.yaml: openai/gpt-5.5).
    model: str = "anthropic/claude-opus-4.7"
    cli_version: str = "0.244.0"

    # ---- routing (no secrets — API keys come from shell env) ----
    provider: str = "openrouter"
    """Routing provider, matching agenthle's droid config. droid is a
    closed-source Factory binary whose model calls go through the BYOK
    ``customModels`` entry; the deployer wires that to OpenRouter
    (``baseUrl=https://openrouter.ai/api/v1`` + OPENROUTER_API_KEY), so
    ``"openrouter"`` is the only routing the deployer implements today.
    ``byok_provider`` below selects the wire protocol within that route."""

    base_url: str | None = None
    """Custom OpenAI-compatible endpoint for the BYOK ``customModels`` entry.
    ``None`` ⇒ OpenRouter default (``https://openrouter.ai/api/v1``). Set to
    point droid at any OpenAI-compatible gateway (written to
    ``settings.json`` ``customModels[0].baseUrl``)."""

    api_key: str | None = None
    """Literal API key for the BYOK endpoint. ``None`` ⇒ read OPENROUTER_API_KEY
    from the env. When set (typically ``api_key: ${env:...}`` in the agent yaml),
    it travels with the serialized config — no env passthrough whitelist change
    and no collision with a real OPENROUTER_API_KEY."""

    reasoning_effort: str = "high"
    """``--reasoning-effort``: off | none | low | medium | high. Defaults to
    ``high`` to match agenthle's operational droid YAMLs (the agenthle
    dataclass default of ``medium`` was not the value actually run)."""

    skip_permissions_unsafe: bool = True
    # agenthle droid_openrouter_*.yaml: max_output_tokens: 128000 (the model's
    # per-request output cap on OpenRouter — the "no client-side limit" choice;
    # higher values get a hard 400 on the first call).
    max_output_tokens: int = 128000
    byok_provider: str = "generic-chat-completion-api"
    disabled_tools: tuple[str, ...] = _DISABLED_TOOLS
