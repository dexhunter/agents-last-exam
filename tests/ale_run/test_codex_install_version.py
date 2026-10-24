from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ale_run.agents.codex.config import CodexConfig
from ale_run.agents.codex.deployer import CodexDeployer


@pytest.mark.parametrize("is_linux", [True, False])
@pytest.mark.parametrize("official", [True, False])
@pytest.mark.parametrize("installed", [None, "0.0.0-old", "current"])
def test_installer_selects_requested_distribution(
    tmp_path, monkeypatch, is_linux, official, installed
):
    config = CodexConfig(cli_version="0.163.0")
    if not official:
        config.fork_version = "0.0.0-agenthle-20260614"
    expected = config.fork_version or config.cli_version
    state = {"version": expected if installed == "current" else installed}
    executor = SimpleNamespace(
        config=config,
        work_dir=str(tmp_path),
        sandbox=SimpleNamespace(is_linux=is_linux),
    )
    deployer = CodexDeployer(executor)
    monkeypatch.setattr("ale_run.agents._bootstrap.ensure_npm", AsyncMock(return_value="npm"))
    monkeypatch.setattr("ale_run.agents._bootstrap.ensure_cua_mcp_server", AsyncMock())
    monkeypatch.setattr(deployer, "_write_codex_config", AsyncMock())
    monkeypatch.setattr(
        "ale_run.agents.codex.deployer.shutil.which",
        lambda name: "codex" if state["version"] else None,
    )
    monkeypatch.setattr(
        "ale_run.agents.codex.deployer.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, f"codex-cli {state['version']}\n", ""
        ),
    )

    async def install(version):
        state["version"] = version

    async def overlay(url):
        state["version"] = config.fork_version

    npm_install = AsyncMock(side_effect=install)
    replace_binary = AsyncMock(side_effect=overlay)
    monkeypatch.setattr(deployer, "_npm_install_codex", npm_install)
    monkeypatch.setattr(deployer, "_replace_native_binary", replace_binary)
    asyncio.run(deployer.install())
    assert state["version"] == expected
    assert deployer.version == expected
    assert deployer.actual_cli_version == expected
    assert deployer.cli_source == ("official" if official else "fork")
    assert npm_install.await_count == int(installed is None or (official and installed != "current"))
    assert replace_binary.await_count == int(not official and installed != "current")
    if replace_binary.await_count:
        replace_binary.assert_awaited_once_with(
            config.patched_binary_url if is_linux else config.patched_binary_url_windows
        )


def test_default_and_legacy_version_selection():
    assert CodexConfig().cli_version == "0.163.0"
    assert CodexConfig().fork_version == ""
    legacy = CodexConfig(codex_version="0.114.0", fork_version="0.0.0-agenthle-20260614")
    assert legacy.cli_version == "0.114.0"
    assert legacy.fork_version == "0.0.0-agenthle-20260614"


@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [("codex-cli 0.163.0-custom", 0), ("codex-cli 0.163.0", 1)],
)
def test_version_check_rejects_mismatched_or_failed_binary(monkeypatch, stdout, returncode):
    deployer = CodexDeployer(SimpleNamespace(config=CodexConfig()))
    monkeypatch.setattr(
        "ale_run.agents.codex.deployer.subprocess.run",
        Mock(return_value=subprocess.CompletedProcess([], returncode, stdout, "")),
    )
    assert not asyncio.run(deployer._version_matches("codex", "0.163.0"))
