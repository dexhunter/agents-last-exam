"""Configuration for the official Qwen Code CLI."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import ClassVar, Literal


@dataclass
class QwenCodeConfig:
    name: ClassVar[str] = "qwen_code"

    model: str = "qwen3.8-max"
    base_url: str = "https://maas.qwencloudapi.com/compatible-mode/v1"
    api_key: str | None = field(default=None, repr=False)
    api_key_env: str = "OPENAI_API_KEY"
    auth_type: Literal["openai", "openai-responses"] = "openai"
    cli_version: str = "0.25.3"

    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] | None = None
    context_window: int | None = None
    auto_compact_token_limit: int | None = None
    max_output_tokens: int | None = None
    max_turns: int | None = None
    max_tool_calls: int | None = None
    max_subagent_depth: int | None = None
    coordinate_space: Literal["pixel", "normalized"] = "pixel"
    telemetry_enabled: bool = True
    disabled_tools: tuple[str, ...] = ()
    extra_envs: dict[str, str] = field(default_factory=dict, repr=False)
    """Additional process environment; harness settings take precedence."""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[\w.-]+)?", self.cli_version):
            raise ValueError("cli_version must be an exact npm version")
        if self.auth_type not in {"openai", "openai-responses"}:
            raise ValueError("auth_type must be openai or openai-responses")
        if self.coordinate_space not in {"pixel", "normalized"}:
            raise ValueError("coordinate_space must be pixel or normalized")
        if self.reasoning_effort not in {None, "none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("unsupported reasoning_effort")
        for name in (
            "context_window",
            "auto_compact_token_limit",
            "max_output_tokens",
            "max_turns",
            "max_tool_calls",
            "max_subagent_depth",
        ):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"{name} must be a positive integer")
        if self.auto_compact_token_limit is not None and (
            not self.context_window
            or not 0.01 <= self.auto_compact_token_limit / self.context_window <= 1
        ):
            raise ValueError("auto_compact_token_limit requires context_window and a 1%-100% ratio")
