"""Settings for the official Muse Code CLI in an ALE sandbox."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass
class MuseCodeConfig:
    name: ClassVar[str] = "muse-code"

    model: str = "muse-wonder-1.0"
    cli_version: str = "1.4.10-R6427.1"
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    reasoning_effort: str = "high"
    yolo: bool = True
    approval_mode: str = "never"
    sandbox_network: str = "enabled"
    trust_workspace: bool = True
    max_turns: int | None = None
    context_window: int | None = None
    auto_compact_token_limit: int | None = None
    context_compaction_strategy: str | None = None
    context_compaction_soft_threshold: float | None = None
    context_compaction_hard_threshold: float | None = None
    max_tool_output_bytes: int | None = None
    parallel_tool_calls: bool | None = None
    execution_capacity: int | None = None
    disable_web_tools: bool = False
    disable_reminders: bool = False
    disable_request_user_input: bool = False
    no_foreign_personal_context: bool = True
    coordinate_space: str = "pixel"
    telemetry_enabled: bool = True
    toolset: tuple[str, ...] | None = None
    settings_overrides: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"\d+\.\d+\.\d+-R\d+(?:\.\d+)?", self.cli_version):
            raise ValueError("cli_version must pin a Muse release, e.g. 1.4.10-R6427.1")
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if self.reasoning_effort not in {
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
            "ultra",
        }:
            raise ValueError("unsupported reasoning_effort")
        if self.approval_mode not in {"never", "on-request", "untrusted"}:
            raise ValueError("unsupported approval_mode")
        if self.sandbox_network not in {"restricted", "proxy-only", "enabled"}:
            raise ValueError("unsupported sandbox_network")
        if self.coordinate_space not in {"pixel", "normalized"}:
            raise ValueError("coordinate_space must be pixel or normalized")
        for name in ("context_window", "auto_compact_token_limit"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_turns is not None and self.max_turns != -1 and self.max_turns <= 0:
            raise ValueError("max_turns must be positive, -1, or null")
        if self.execution_capacity is not None and not 1 <= self.execution_capacity <= 64:
            raise ValueError("execution_capacity must be between 1 and 64")
        if self.max_tool_output_bytes is not None and (
            self.max_tool_output_bytes != 0 and self.max_tool_output_bytes < 9
        ):
            raise ValueError("max_tool_output_bytes must be 0 or at least 9")
        for name in ("context_compaction_soft_threshold", "context_compaction_hard_threshold"):
            value = getattr(self, name)
            if value is not None and not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        soft = self.context_compaction_soft_threshold
        hard = self.context_compaction_hard_threshold
        if self.auto_compact_token_limit is not None:
            if self.context_window is None:
                raise ValueError("auto_compact_token_limit requires an explicit context_window")
            if soft is not None:
                raise ValueError("choose auto_compact_token_limit or a soft threshold, not both")
            soft = self.auto_compact_token_limit / self.context_window
            if soft >= 1:
                raise ValueError("auto_compact_token_limit must be smaller than context_window")
        if soft is not None and hard is not None and soft >= hard:
            raise ValueError("soft compaction threshold must be smaller than hard threshold")
        if self.context_compaction_strategy not in {
            None,
            "summary-preserved-suffix/v1",
            "prefix-extension-summary/v1",
            "prefix-extension-inventory-summary/v1",
        }:
            raise ValueError("unsupported context_compaction_strategy")
        if self.toolset is not None:
            self.toolset = tuple(self.toolset)
            if not all(isinstance(tool, str) and tool.strip() for tool in self.toolset):
                raise ValueError("toolset must contain nonempty tool names")
        # The required GUI bridge and retained logs are harness-owned.
        for key in self.settings_overrides:
            if key.split(".")[0] in {"mcpServers", "mcp_servers", "telemetry"}:
                raise ValueError(f"settings_overrides cannot override harness-owned {key}")
