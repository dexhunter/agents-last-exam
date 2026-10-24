from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from ale_run.agents.claude_code.config import ClaudeCodeConfig
from ale_run.agents.claude_code.deployer import ClaudeCodeDeployer
from ale_run.agents.codex.config import CodexConfig
from ale_run.agents.codex.deployer import CodexDeployer


@pytest.mark.parametrize("is_linux", [True, False])
def test_codex_context_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, is_linux: bool) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = CodexConfig(
        provider="direct", context_window=1_000_000, auto_compact_token_limit=900_000,
        model_catalog_content='{"models": []}',
    )
    executor = SimpleNamespace(
        config=config, work_dir=str(tmp_path),
        sandbox=SimpleNamespace(is_linux=is_linux, node="node", mcp_server_dir="cua"),
        cua_bridge_url=lambda: "http://127.0.0.1:8000",
    )
    asyncio.run(CodexDeployer(executor)._write_codex_config(config))
    rendered = tomllib.loads((tmp_path / ".codex/config.toml").read_text())
    assert rendered["model_context_window"] == 1_000_000
    assert rendered["model_auto_compact_token_limit"] == 900_000
    assert "model_catalog_json" in rendered


def test_claude_context_config(tmp_path: Path) -> None:
    config = ClaudeCodeConfig(
        provider="direct", api_key="test", context_window=1_000_000,
        auto_compact_token_limit=900_000, effort_level="max",
    )
    executor = SimpleNamespace(config=config, work_dir=str(tmp_path), env={})
    env = ClaudeCodeDeployer(executor)._build_env(config)
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "1000000"
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "900000"
    argv = ClaudeCodeDeployer._build_argv(claude_path="claude", cfg=config, mcp_config="mcp.json")
    assert argv[argv.index("--effort") + 1] == "max"


@pytest.mark.parametrize("config_class", [CodexConfig, ClaudeCodeConfig])
@pytest.mark.parametrize("field", ["context_window", "auto_compact_token_limit"])
def test_context_parameters_must_be_positive(config_class, field: str) -> None:
    with pytest.raises(ValueError, match=field):
        config_class(**{field: 0})
