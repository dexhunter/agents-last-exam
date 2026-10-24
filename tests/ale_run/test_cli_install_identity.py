from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ale_run.agents.cursor_cli.config import CursorCliConfig
from ale_run.agents.cursor_cli.deployer import CursorCliDeployer
from ale_run.agents.droid.config import DroidConfig
from ale_run.agents.droid.deployer import DroidDeployer
from ale_run.agents.forgecode.config import ForgecodeConfig
from ale_run.agents.forgecode.deployer import ForgecodeDeployer
from ale_run.agents.grok_build.config import GrokBuildConfig
from ale_run.agents.grok_build.deployer import GrokBuildDeployer
from ale_run.agents.openhands_cli.config import OpenHandsCliConfig
from ale_run.agents.openhands_cli.deployer import OpenHandsCliDeployer
from ale_run.agents.octavus_cli.config import OctavusCliConfig
from ale_run.agents.octavus_cli.deployer import OctavusCliDeployer
from ale_run.agents.zcode.config import ZCodeConfig
from ale_run.agents.claude_code.deployer import _installed_version as claude_version
from ale_run.agents.kimi_code.deployer import _command_version as kimi_version
from ale_run.agents.antigravity_cli.deployer import _installed_version as antigravity_version
from ale_run.agents.grok_build.deployer import _installed_binary_version as grok_version
from ale_run.agents.grok_build.deployer import _node_version as grok_node_version


@pytest.mark.parametrize(
    "probe", [claude_version, kimi_version, antigravity_version, grok_version, grok_node_version]
)
def test_version_probe_rejects_nonzero_exit_with_version_text(monkeypatch, probe):
    monkeypatch.setattr(
        "subprocess.run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "2.1.170", "failed"),
    )
    assert probe("cli") is None


@pytest.mark.parametrize("initial", ["0.244.0", "0.100.0", None])
@pytest.mark.parametrize("install_succeeds", [True, False])
def test_droid_checks_reinstalls_and_rechecks(tmp_path, monkeypatch, initial, install_succeeds):
    state = {"version": initial}
    cfg = DroidConfig(api_key="test")
    sandbox = SimpleNamespace(is_linux=True, node="node", mcp_server_dir="/mcp")
    executor = SimpleNamespace(config=cfg, sandbox=sandbox, work_dir=str(tmp_path), env={})
    deployer = DroidDeployer(executor)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("ale_run.agents.droid.deployer.shutil.which", lambda name: "/droid")
    monkeypatch.setattr(deployer, "_installed_version", lambda path: state["version"])
    monkeypatch.setattr("ale_run.agents._bootstrap.ensure_cua_mcp_server", AsyncMock())
    monkeypatch.setattr("ale_run.agents._bootstrap.cua_bridge_env", lambda executor: {})
    monkeypatch.setattr(
        deployer, "_resolve_disabled_tools", AsyncMock(return_value=cfg.disabled_tools)
    )

    async def install():
        if install_succeeds:
            state["version"] = cfg.cli_version

    installer = AsyncMock(side_effect=install)
    monkeypatch.setattr(deployer, "_auto_install_cli", installer)
    if initial != cfg.cli_version and not install_succeeds:
        with pytest.raises(RuntimeError, match="installed version"):
            asyncio.run(deployer.install())
    else:
        asyncio.run(deployer.install())
        assert deployer.actual_cli_version == cfg.cli_version
    assert installer.await_count == int(initial != cfg.cli_version)


@pytest.mark.parametrize(
    "returncode,stderr,expected_squad",
    [(0, "", True), (1, "Unknown tool identifier(s)", False), (1, "authentication failed", True)],
)
def test_droid_resolves_unavailable_legacy_tool(
    tmp_path, monkeypatch, returncode, stderr, expected_squad
):
    cfg = DroidConfig()
    executor = SimpleNamespace(config=cfg, work_dir=str(tmp_path), env={})
    deployer = DroidDeployer(executor)
    deployer._droid_path = "droid"
    commands = []

    def probe(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, returncode, "", stderr)

    monkeypatch.setattr("ale_run.agents.droid.deployer.subprocess.run", probe)
    result = asyncio.run(deployer._resolve_disabled_tools(cfg, tmp_path))
    assert ("squad-board" in result) is expected_squad
    assert set(result) | {"squad-board"} == set(cfg.disabled_tools)
    assert "--list-tools" in commands[0]


@pytest.mark.parametrize(
    "host_key,executor_key,expected",
    [
        (None, None, None),
        ("host-key", None, "host-key"),
        ("host-key", "executor-key", "executor-key"),
    ],
)
def test_droid_preserves_factory_auth(monkeypatch, tmp_path, host_key, executor_key, expected):
    monkeypatch.delenv("FACTORY_API_KEY", raising=False)
    if host_key:
        monkeypatch.setenv("FACTORY_API_KEY", host_key)
    cfg = DroidConfig()
    env = {"FACTORY_API_KEY": executor_key} if executor_key else {}
    executor = SimpleNamespace(config=cfg, work_dir=str(tmp_path), env=env)
    result = DroidDeployer(executor)._build_env(cfg)
    assert result.get("FACTORY_API_KEY") == expected


@pytest.mark.parametrize("is_linux", [True, False])
@pytest.mark.parametrize("initial", ["2026.10.16-e3cb207", "2026.05.28-a70ca7c"])
@pytest.mark.parametrize("install_succeeds", [True, False])
def test_cursor_checks_reinstalls_and_rechecks(
    tmp_path,
    monkeypatch,
    is_linux,
    initial,
    install_succeeds,
):
    state = {"version": initial}
    cfg = CursorCliConfig()
    sandbox = SimpleNamespace(is_linux=is_linux, node="node", mcp_server_dir="/mcp")
    executor = SimpleNamespace(config=cfg, sandbox=sandbox, work_dir=str(tmp_path), env={})
    deployer = CursorCliDeployer(executor)
    monkeypatch.setenv("HOME", str(tmp_path))
    version_dir = tmp_path / ".local" / "share" / "cursor-agent" / "versions" / cfg.cli_version
    version_dir.mkdir(parents=True)
    (version_dir / "node.exe").touch()
    (version_dir / "index.js").touch()
    monkeypatch.setattr(
        "ale_run.agents.cursor_cli.deployer.shutil.which", lambda name: "/cursor-agent"
    )
    monkeypatch.setattr(deployer, "_probe_version", lambda *args: state["version"])
    monkeypatch.setattr("ale_run.agents._bootstrap.ensure_cua_mcp_server", AsyncMock())
    monkeypatch.setattr("ale_run.agents._bootstrap.cua_bridge_env", lambda executor: {})
    monkeypatch.setattr(deployer, "_setup_cursor_auth", lambda: None)

    def install(version):
        if install_succeeds:
            state["version"] = version

    async def install_async(version):
        install(version)

    installer = AsyncMock(side_effect=install_async)
    monkeypatch.setattr(deployer, "_install_pinned", installer)
    monkeypatch.setattr(deployer, "_install_pinned_windows_sync", install)
    if initial != cfg.cli_version and not install_succeeds:
        with pytest.raises(RuntimeError, match="installed version"):
            asyncio.run(deployer.install())
    else:
        asyncio.run(deployer.install())
        assert deployer.actual_cli_version == cfg.cli_version
    if is_linux:
        assert installer.await_count == int(initial != cfg.cli_version)


def test_cursor_legacy_version_field_is_honored():
    assert CursorCliConfig(cursor_version="2026.05.28-a70ca7c").cli_version == "2026.05.28-a70ca7c"


def test_zcode_legacy_ref_is_honored():
    assert ZCodeConfig(cli_ref="previous-commit").cli_version == "previous-commit"


def test_forge_legacy_version_is_honored():
    assert ForgecodeConfig(forge_version="2.13.0").cli_version == "2.13.0"


@pytest.mark.parametrize("initial", ["1.0.62", "1.0.61", None])
@pytest.mark.parametrize("install_succeeds", [True, False])
def test_grok_checks_reinstalls_and_rechecks(tmp_path, monkeypatch, initial, install_succeeds):
    state = {"version": initial, "install_count": 0}
    config = GrokBuildConfig()
    executor = SimpleNamespace(
        config=config,
        sandbox=SimpleNamespace(is_linux=True, mcp_server_dir="/mcp"),
        work_dir=str(tmp_path),
        cua_bridge_url=lambda: "http://localhost:5000",
    )
    deployer = GrokBuildDeployer(executor)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "ale_run.agents._bootstrap.ensure_node_npm", AsyncMock(return_value=("node", "npm"))
    )
    monkeypatch.setattr("ale_run.agents._bootstrap.ensure_cua_mcp_server", AsyncMock())
    monkeypatch.setattr("ale_run.agents.grok_build.deployer._node_version", lambda path: (22, 0, 0))
    monkeypatch.setattr(
        "ale_run.agents.grok_build.deployer._native_binary", lambda *args: tmp_path / "grok"
    )
    monkeypatch.setattr(
        "ale_run.agents.grok_build.deployer._installed_package_version",
        lambda *args: state["version"],
    )
    monkeypatch.setattr(
        "ale_run.agents.grok_build.deployer._installed_binary_version",
        lambda *args: state["version"],
    )

    def install(argv, **kwargs):
        state["install_count"] += 1
        if install_succeeds:
            state["version"] = "1.0.62"
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("ale_run.agents.grok_build.deployer.subprocess.run", install)
    if initial != "1.0.62" and not install_succeeds:
        with pytest.raises(RuntimeError, match="version"):
            asyncio.run(deployer.install())
    else:
        asyncio.run(deployer.install())
        assert deployer.actual_cli_version == "1.0.62"
    assert state["install_count"] == int(initial != "1.0.62")


@pytest.mark.parametrize("initial", ["2.14.0", "2.13.0", None])
@pytest.mark.parametrize("install_succeeds", [True, False])
def test_forge_checks_reinstalls_and_rechecks(tmp_path, monkeypatch, initial, install_succeeds):
    state = {"version": initial}
    cfg = ForgecodeConfig()
    sandbox = SimpleNamespace(is_linux=True, node="node", mcp_server_dir="/mcp")
    executor = SimpleNamespace(
        config=cfg,
        sandbox=sandbox,
        work_dir=str(tmp_path),
        env={},
        cua_bridge_url=lambda: "http://localhost:5000",
    )
    deployer = ForgecodeDeployer(executor)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("ale_run.agents.forgecode.deployer.shutil.which", lambda name: "/forge")
    monkeypatch.setattr(deployer, "_installed_version", lambda path: state["version"])
    monkeypatch.setattr("ale_run.agents._bootstrap.ensure_cua_mcp_server", AsyncMock())
    monkeypatch.setattr("ale_run.agents._bootstrap.cua_bridge_env", lambda executor: {})

    async def install():
        if install_succeeds:
            state["version"] = cfg.cli_version

    installer = AsyncMock(side_effect=install)
    monkeypatch.setattr(deployer, "_auto_install_cli", installer)
    if initial != cfg.cli_version and not install_succeeds:
        with pytest.raises(RuntimeError, match="installed version"):
            asyncio.run(deployer.install())
    else:
        asyncio.run(deployer.install())
        assert deployer.actual_cli_version == cfg.cli_version
    assert installer.await_count == int(initial != cfg.cli_version)


@pytest.mark.parametrize("initial", ["1.16.0", "1.15.1", None])
@pytest.mark.parametrize("install_succeeds", [True, False])
def test_openhands_checks_reinstalls_and_rechecks(tmp_path, monkeypatch, initial, install_succeeds):
    state = {"version": initial}
    cfg = OpenHandsCliConfig(api_key="test")
    sandbox = SimpleNamespace(is_linux=True, node="node", mcp_server_dir="/mcp")
    executor = SimpleNamespace(
        config=cfg,
        sandbox=sandbox,
        work_dir=str(tmp_path),
        env={},
        cua_bridge_url=lambda: "http://localhost:5000",
    )
    deployer = OpenHandsCliDeployer(executor)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "ale_run.agents.openhands_cli.deployer.shutil.which", lambda name: "/openhands"
    )
    monkeypatch.setattr(deployer, "_installed_version", lambda path: state["version"])
    monkeypatch.setattr("ale_run.agents._bootstrap.ensure_cua_mcp_server", AsyncMock())
    monkeypatch.setattr("ale_run.agents._bootstrap.cua_bridge_env", lambda executor: {})

    async def install(version):
        if install_succeeds:
            state["version"] = version

    installer = AsyncMock(side_effect=install)
    monkeypatch.setattr(deployer, "_install_cli", installer)
    if initial != cfg.cli_version and not install_succeeds:
        with pytest.raises(RuntimeError, match="installed version"):
            asyncio.run(deployer.install())
    else:
        asyncio.run(deployer.install())
        assert deployer.actual_cli_version == cfg.cli_version
    assert installer.await_count == int(initial != cfg.cli_version)


@pytest.mark.parametrize("initial", ["1.0.32", "1.0.10", None])
@pytest.mark.parametrize("install_succeeds", [True, False])
def test_octavus_checks_reinstalls_and_rechecks(tmp_path, monkeypatch, initial, install_succeeds):
    state = {"version": initial, "install_count": 0}
    cfg = OctavusCliConfig(install_prereqs=False, chrome_path="chrome")
    executor = SimpleNamespace(config=cfg, work_dir=str(tmp_path))
    deployer = OctavusCliDeployer(executor)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "ale_run.agents._bootstrap.ensure_node_npm", AsyncMock(return_value=("node", "npm"))
    )
    monkeypatch.setattr(deployer, "_resolve_octoagent", lambda: "/octoagent")
    monkeypatch.setattr(deployer, "_installed_version", lambda path: state["version"])

    def install(argv, **kwargs):
        state["install_count"] += 1
        if install_succeeds:
            state["version"] = "1.0.32"
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("ale_run.agents.octavus_cli.deployer.subprocess.run", install)
    if initial != "1.0.32" and not install_succeeds:
        with pytest.raises(RuntimeError, match="installed version"):
            asyncio.run(deployer.install())
    else:
        asyncio.run(deployer.install())
        assert deployer.actual_cli_version == "1.0.32"
    assert state["install_count"] == int(initial != "1.0.32")
