"""ForgecodeConfig: per-episode knobs for the forgecode (tailcallhq/forge) deployer.

Auth: forge reads its API key from environment variables.  When using
OpenRouter, ``ANTHROPIC_API_KEY`` is set to the OpenRouter key and
``ANTHROPIC_BASE_URL`` is pointed at ``https://openrouter.ai/api/v1``.
For direct providers the standard env var is exported as-is.

forge.toml ``[session]`` block pins ``provider_id`` + ``model_id`` so
multi-key environments cannot accidentally route through the wrong vendor.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass
class ForgecodeConfig:
    """Tunables for :class:`ForgecodeDeployer`.

    Standalone config (no shared base). The episode wall-budget is
    orchestration-owned; ``timeout_s`` is no longer an agent knob.
    """

    name: ClassVar[str] = "forgecode"

    # agenthle forgecode_openrouter.yaml: anthropic/claude-sonnet-4.6
    # (direct variant forgecode.yaml uses anthropic/claude-sonnet-4).
    model: str = "anthropic/claude-sonnet-4.6"

    cli_version: str = "2.14.0"
    """Pinned forge release tag (without the ``v`` prefix). The deployer
    downloads ``releases/download/v<cli_version>/forge-...-musl`` instead
    of ``releases/latest`` so every environment converges on one binary.
    Pinning matters because forge's agent mode (``-p``) otherwise
    self-updates to the latest release on startup — see
    :meth:`render_forge_toml`'s ``[updates]`` block, which also disables that
    self-update at runtime."""
    forge_version: str | None = None
    """Legacy alias for existing configurations."""

    # Sampling / model knobs surfaced through forge.toml.
    # agenthle: temperature 0.7 (both openrouter + direct).
    temperature: float | None = 0.7

    # Reserved for future use: forge tool names to disable.  forge itself
    # does not have a config-driven disable-list, so this is a forward-
    # compatible placeholder; the deployer translates this into a comment
    # in forge.toml only.
    disabled_tools: tuple[str, ...] = ()

    # Provider routing fields.  The model prefix (``anthropic/...``,
    # ``openai/...``) determines which env var to export, unless the user
    # is going through OpenRouter in which case we always use the
    # OPENROUTER_API_KEY.
    provider: str = "openrouter"
    """``"openrouter"`` or ``"direct"``."""

    def __post_init__(self) -> None:
        if self.forge_version is not None:
            self.cli_version = self.forge_version

    @property
    def is_openrouter(self) -> bool:
        return self.provider.lower() in ("openrouter", "open_router")

    # ------------------------------------------------------------------
    # forge.toml rendering
    # ------------------------------------------------------------------

    def forge_provider_id(self) -> str:
        """Return the ``provider_id`` for ``forge.toml``'s ``[session]``.

        forge's canonical IDs: ``open_router``, ``anthropic``, ``openai``,
        etc.  (``crates/forge_domain/src/provider.rs``).
        """
        if self.is_openrouter:
            return "open_router"
        prefix = self.model.split("/", 1)[0].lower()
        if prefix == "anthropic":
            return "anthropic"
        if prefix == "openai" or prefix.startswith("gpt"):
            return "openai"
        return prefix or "open_router"

    def forge_model_id(self) -> str:
        """Return the ``model_id`` for ``forge.toml``'s ``[session]``.

        OpenRouter keeps the ``vendor/model`` form; direct providers strip
        the vendor prefix (e.g. ``anthropic/claude-sonnet-4`` becomes
        ``claude-sonnet-4`` under ``provider_id = "anthropic"``).
        """
        if self.is_openrouter:
            return self.model
        provider = self.forge_provider_id()
        prefix = self.model.split("/", 1)[0].lower()
        if prefix == provider and "/" in self.model:
            return self.model.split("/", 1)[1]
        return self.model

    def render_forge_toml(self) -> str:
        """Render the ``~/.forge/.forge.toml`` content.

        Always sets ``auto_dump = "json"`` so forge writes a timestamped
        dump.json on ``TaskComplete``.  Deliberately omits
        ``max_requests_per_turn`` and ``max_tool_failure_per_turn`` so
        forge's interactive "continue anyway?" prompt never fires.

        The ``[updates]`` block disables forge's runtime self-update. In
        ``-p`` (agent) mode forge otherwise downloads ``releases/latest`` on
        startup and installs it over its own binary, which both replaces the
        running process mid-task (so the prompt never executes — rc=0 with no
        ConversationDump, hence empty usage) and defeats the version pin.
        ``frequency = "never"`` stops the update check entirely.
        """
        provider = self.forge_provider_id()
        model = self.forge_model_id()
        lines = ['auto_dump = "json"']
        if self.temperature is not None:
            lines.append(f"temperature = {float(self.temperature)}")
        lines.extend([
            "",
            "[updates]",
            "auto_update = false",
            'frequency = "never"',
            "",
            "[session]",
            f'provider_id = "{provider}"',
            f'model_id = "{model}"',
        ])
        return "\n".join(lines) + "\n"
