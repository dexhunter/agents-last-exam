from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from ale_run.agents.muse_code import MuseCodeConfig, MuseCodeDeployer
from ale_run.agents.muse_code import install as installer
from ale_run.agents.muse_code.telemetry import (
    NativeEventMirror,
    native_events,
    read_records,
    recover_telemetry_artifacts,
)
from ale_run.base_interface import AgentRunResult, TrajectoryBuilder, persist_screenshots
from ale_run.executors._secrets import config_to_kwargs_and_secrets, restore_config_secrets
from ale_run.orchestration.experiment_spec import AgentSpec
from ale_run.orchestration.factory import build_config, resolve_agent

FIXTURE = Path(__file__).parent / "fixtures" / "muse_code_session.jsonl"


def adapter(tmp_path, *, is_linux=True, **kwargs):
    executor = SimpleNamespace(
        config=MuseCodeConfig(**kwargs),
        work_dir=str(tmp_path),
        env={
            "XDG_CONFIG_HOME": str(tmp_path / "config-root"),
            "XDG_DATA_HOME": str(tmp_path / "data-root"),
            "XDG_CACHE_HOME": str(tmp_path / "cache-root"),
        },
        sandbox=SimpleNamespace(
            is_linux=is_linux,
            node="/opt/node/bin/node" if is_linux else r"C:\Program Files\nodejs\node.exe",
            mcp_server_dir="/opt/cua" if is_linux else r"C:\Program Files\cua",
        ),
        cua_bridge_url=lambda: "http://127.0.0.1:8000",
    )
    deployer = MuseCodeDeployer(executor)
    deployer._muse_path = "muse" if is_linux else r"C:\Program Files\Muse\muse.exe"
    return deployer


def test_registry_config_and_preset():
    deployer, config = resolve_agent(AgentSpec(id="muse", class_="muse_code"))
    assert deployer is MuseCodeDeployer
    assert config is MuseCodeConfig
    assert config().model == "muse-wonder-1.0"
    assert deployer.supported_executors == {"sandbox"}
    root = Path(__file__).resolve().parents[2]
    preset = yaml.safe_load((root / "configs/agents/muse_code.yaml").read_text())
    assert asdict(build_config(config, {"model": preset["model"], **preset["config"]})) == asdict(
        config()
    )
    assert set(preset["config"]) == set(asdict(config())) - {"model"}
    with pytest.raises(ValueError, match="supported"):
        resolve_agent(AgentSpec(id="muse", class_="muse_code", executor="local"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cli_version": "latest"},
        {"cli_version": "../1.4.10"},
        {"model": ""},
        {"reasoning_effort": "invalid"},
        {"reasoning_effort": "none"},
        {"approval_mode": "ask"},
        {"sandbox_network": "invalid"},
        {"coordinate_space": "invalid"},
        {"context_window": 0},
        {"auto_compact_token_limit": -1},
        {"auto_compact_token_limit": 1000},
        {"context_window": 100, "auto_compact_token_limit": 100},
        {
            "context_window": 100,
            "auto_compact_token_limit": 50,
            "context_compaction_soft_threshold": 0.5,
        },
        {"context_compaction_soft_threshold": 0},
        {"context_compaction_hard_threshold": 1.1},
        {"context_compaction_soft_threshold": 0.9, "context_compaction_hard_threshold": 0.8},
        {"context_compaction_strategy": "invalid"},
        {"max_turns": 0},
        {"max_turns": -2},
        {"execution_capacity": 65},
        {"execution_capacity": 0},
        {"max_tool_output_bytes": 8},
        {"toolset": [""]},
        {"settings_overrides": {"mcpServers.cua.enabled": False}},
        {"settings_overrides": {"mcp_servers.cua": {}}},
        {"settings_overrides": {"telemetry.endpoint": "https://example.com"}},
    ],
)
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        MuseCodeConfig(**kwargs)


@pytest.mark.parametrize("is_linux", [True, False])
def test_gui_bridge_uses_sandbox_paths_and_endpoint(tmp_path, is_linux):
    deployer = adapter(tmp_path, is_linux=is_linux)
    deployer._write_settings()
    settings = json.loads((tmp_path / "muse-settings.json").read_text())
    cua = settings["mcpServers"]["cua"]
    assert cua["command"] == deployer.executor.sandbox.node
    assert cua["args"] == [
        "/opt/cua/src/index.js" if is_linux else r"C:\Program Files\cua\src\index.js"
    ]
    assert cua["env"] == {
        "CUA_SERVER_URL": "http://127.0.0.1:8000",
        "CUA_SERVER_PLATFORM": "linux" if is_linux else "win32",
        "CUA_COORDINATE_SPACE": "pixel",
    }
    assert cua["mode"] == "required" and cua["enabled"]
    assert cua["transport"] == "stdio"


def test_env_auth_isolation_and_secret_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv("META_API_KEY", "host-key")
    deployer = adapter(tmp_path, api_key="config-key")
    deployer.executor.env["META_API_KEY"] = "executor-key"
    assert deployer._build_env()["META_API_KEY"] == "config-key"
    assert "config-key" not in repr(deployer.config)
    kwargs, secrets = config_to_kwargs_and_secrets(deployer.config)
    assert "config-key" not in json.dumps(kwargs)
    restored, _ = restore_config_secrets(kwargs, secrets)
    assert restored["api_key"] == "config-key"
    deployer.config.api_key = None
    env = deployer._build_env()
    assert env["META_API_KEY"] == "executor-key"
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "config-root")
    assert env["XDG_DATA_HOME"] == str(tmp_path / "data-root")
    assert env["MUSE_NO_AUTO_UPDATE"] == "1"
    assert "executor-key" not in json.dumps(deployer._build_argv())
    monkeypatch.delenv("META_API_KEY")
    deployer.executor.env.clear()
    with pytest.raises(RuntimeError, match="META_API_KEY"):
        deployer._build_env()


def test_meta_key_forwarded_to_sandbox(monkeypatch):
    from ale_run.orchestration.lifecycle import _collect_env_passthrough

    monkeypatch.setenv("META_API_KEY", "test-meta-key")
    assert _collect_env_passthrough()["META_API_KEY"] == "test-meta-key"


def test_argv_consumes_native_knobs(tmp_path):
    deployer = adapter(
        tmp_path,
        max_turns=3,
        context_window=10000,
        auto_compact_token_limit=7000,
        context_compaction_hard_threshold=0.9,
        context_compaction_strategy="summary-preserved-suffix/v1",
        max_tool_output_bytes=1024,
        parallel_tool_calls=False,
        execution_capacity=4,
        disable_web_tools=True,
        disable_reminders=True,
        disable_request_user_input=True,
        base_url="http://localhost:1234/v1",
        toolset=["bash"],
        settings_overrides={"run.context_slimming": False},
    )
    argv = deployer._build_argv()
    assert ["--prompt-file", str(tmp_path / "prompt.txt")] == argv[argv.index("--prompt-file") :][
        :2
    ]
    assert "--yolo" in argv and "--disable-request-user-input" in argv
    assert "--disable-web-tools" in argv and "--disable-reminders" in argv
    assert "--no-parallel-tool-calls" in argv and "--no-foreign-personal-context" in argv
    assert argv[argv.index("--max-model-steps") + 1] == "3"
    options = dict(arg.split("=", 1) for i, arg in enumerate(argv) if i and argv[i - 1] == "-c")
    assert json.loads(options["context_compaction.provider_context_limit_tokens"]) == 10000
    assert json.loads(options["context_compaction.soft_threshold"]) == 0.7
    assert json.loads(options["context_compaction.hard_threshold"]) == 0.9
    assert json.loads(options["run.toolset"]) == ["bash", "mcp__cua__*"]
    assert json.loads(options["agents.execution_capacity"]) == 4
    deployer.config.yolo = False
    argv = deployer._build_argv()
    assert "--yolo" not in argv and "--approval-mode" in argv and "--trust-workspace" in argv


def test_argv_does_not_disable_tools_by_default(tmp_path):
    argv = adapter(tmp_path)._build_argv()
    assert "--disable-request-user-input" not in argv
    assert "--disable-web-tools" not in argv
    assert "--disable-reminders" not in argv


@pytest.mark.parametrize(
    "version,rc,expected",
    [
        ("Muse Code 1.4.10 (1.4.10-R6427.1)\n", 0, "1.4.10-R6427.1"),
        ("Muse Code 1.4.10 (1.4.10-R6427.1)\n", 1, None),
        ("1.4.10", 0, None),
        ("old wrapper", 0, None),
    ],
)
def test_version_probe_checks_exact_build(monkeypatch, version, rc, expected):
    def probe(argv, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["env"]["MUSE_NO_AUTO_UPDATE"] == "1"
        return subprocess.CompletedProcess(argv, rc, version, "")

    monkeypatch.setattr(installer.subprocess, "run", probe)
    assert installer.installed_version("muse") == expected


@pytest.mark.parametrize(
    "is_linux,machine,target",
    [
        (True, "x86_64", "x86_linux"),
        (False, "AMD64", "x86_windows"),
        (True, "aarch64", "aarch64_linux"),
        (False, "ARM64", "aarch64_windows"),
    ],
)
@pytest.mark.parametrize("corrupt", [False, True])
def test_installer_verifies_artifact_and_keeps_existing_binary(
    tmp_path, monkeypatch, is_linux, machine, target, corrupt
):
    version = MuseCodeConfig().cli_version
    payload = b"native-muse-binary"
    artifact = {
        "url": "https://download.example/muse",
        "size": len(payload),
        "checksum": hashlib.sha256(payload).hexdigest(),
    }
    manifest = {"version": version, "checksum_algorithm": "sha256", "artifacts": {target: artifact}}

    def download(url, **kwargs):
        if "manifest.json" in url:
            return io.BytesIO(json.dumps(manifest).encode())
        return io.BytesIO(b"damaged" if corrupt else payload)

    monkeypatch.setattr(installer.platform, "machine", lambda: machine)
    monkeypatch.setattr(installer.urllib.request, "urlopen", download)
    monkeypatch.setattr(installer, "installed_version", lambda path: version)
    binary = tmp_path / ("muse" if is_linux else "muse.exe")
    binary.write_bytes(b"previous")
    if corrupt:
        with pytest.raises(RuntimeError, match="mismatch"):
            installer.install_release(version, is_linux=is_linux, directory=tmp_path)
        assert binary.read_bytes() == b"previous"
    else:
        assert installer.install_release(version, is_linux=is_linux, directory=tmp_path) == binary
        assert binary.read_bytes() == payload
    assert not list(tmp_path.glob(".muse-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [True, False])
async def test_install_reuses_only_the_pinned_binary(tmp_path, monkeypatch, stale):
    from ale_run.agents import _bootstrap
    from ale_run.agents.muse_code import deployer as module

    instance = adapter(tmp_path)
    monkeypatch.setattr(module.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/stale/muse")
    monkeypatch.setattr(
        module,
        "installed_version",
        lambda path: "1.0.0-R1" if stale and path == "/stale/muse" else instance.version,
    )
    calls = []

    def install(version, **kwargs):
        calls.append(kwargs)
        return tmp_path / "pinned/muse"

    monkeypatch.setattr(module, "install_release", install)
    monkeypatch.setattr(_bootstrap, "ensure_cua_mcp_server", AsyncMock())
    await instance.install()
    assert bool(calls) is stale
    assert instance._muse_path == (str(tmp_path / "pinned/muse") if stale else "/stale/muse")
    assert instance.actual_cli_version == instance.version
    assert instance.cli_source == "official"


def test_real_native_fixture_preserves_tools_images_usage_and_timestamps(tmp_path):
    (tmp_path / "native_events.jsonl").write_text(FIXTURE.read_text())
    builder = TrajectoryBuilder(agent_name="muse-code", task_path="demo/hello", variant_index=0)
    MuseCodeDeployer.parse_artifacts(
        work_dir=tmp_path,
        config=MuseCodeConfig(),
        run_result=AgentRunResult(status="completed", exit_code=0),
        builder=builder,
    )
    trajectory = builder.finalize(reward=1)
    calls = [call for step in trajectory.steps for call in step.tool_calls]
    assert [c.name for c in calls] == ["mcp__cua__screenshot"]
    observations = [r for s in trajectory.steps if s.observation for r in s.observation.results]
    assert observations[0].tool_call_id == calls[0].id
    assert observations[0].content[1].image.media_type == "image/png"
    assert persist_screenshots(trajectory, tmp_path) == 1
    assert trajectory.final_metrics.total_input_tokens == 200
    assert trajectory.final_metrics.total_output_tokens == 40
    assert trajectory.final_metrics.total_cache_read_tokens == 20
    assert all(s.timestamp.startswith("2026-10-19") for s in trajectory.steps)
    summary = json.loads((tmp_path / "telemetry_summary.json").read_text())
    assert len(summary["api_calls"]) == 2
    assert summary["usage"]["reasoning_tokens"] == 10


def test_mirror_waits_for_complete_line_and_redacts(tmp_path):
    data = tmp_path / "data" / "session"
    data.mkdir(parents=True)
    source = data / "session.jsonl"
    record = {"id": "event", "payload": {"text": "private-key"}}
    line = json.dumps(record).encode()
    source.write_bytes(line[:20])
    mirror = NativeEventMirror(tmp_path, tmp_path / "data", ("private-key",))
    mirror.poll()
    assert mirror.path.read_text() == ""
    with source.open("ab") as handle:
        handle.write(line[20:] + b"\n")
    mirror.poll()
    mirror.poll()
    assert len(mirror.path.read_text().splitlines()) == 1
    assert "private-key" not in mirror.path.read_text()
    assert "[REDACTED]" in mirror.path.read_text()


def test_retained_frames_duplicates_and_truncation(tmp_path):
    records = list(read_records(FIXTURE))
    frame = {
        "retained_frame": "session_permission_transaction",
        "children": [
            {"record_json": json.dumps(records[0])},
            {"record_json": "invalid"},
        ],
    }
    path = tmp_path / "native_events.jsonl"
    path.write_text("\ufeff" + json.dumps(frame) + "\n" + FIXTURE.read_text() * 2 + '{"broken":')
    unique = list(native_events(tmp_path))
    assert len(unique) == len(list(native_events_from_fixture(tmp_path)))
    assert recover_telemetry_artifacts(tmp_path)["usage"]["input_tokens"] == 200


def native_events_from_fixture(tmp_path):
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "native_events.jsonl").write_text(FIXTURE.read_text())
    return native_events(clean)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rc,terminal,status",
    [
        (0, "completed", "completed"),
        (0, "failed", "failed"),
        (1, "failed", "failed"),
        (0, None, "failed"),
    ],
)
async def test_launch_checks_terminal_and_redacts_streams(
    tmp_path, monkeypatch, rc, terminal, status
):
    deployer = adapter(tmp_path, api_key="secret-fixture")
    event = {
        "payload_type": "run.terminal." + str(terminal),
        "payload": {
            "terminal": terminal,
            "reason": "secret-fixture" if terminal == "failed" else None,
        },
    }
    script = (
        "import sys; print('secret-fixture',file=sys.stderr); "
        + (f"print({json.dumps(event)!r}); " if terminal else "")
        + f"sys.exit({rc})"
    )
    monkeypatch.setattr(deployer, "_build_argv", lambda: [sys.executable, "-c", script])
    result = await deployer.launch("你好\nquoted ' and \" prompt")
    assert result.status == status
    assert "secret-fixture" not in (result.error or "")
    assert "secret-fixture" not in (tmp_path / "stderr.log").read_text()
    assert "secret-fixture" not in (tmp_path / "transcript.jsonl").read_text()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression")
@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_fails", [False, True])
async def test_cancellation_reaps_cli_and_child(tmp_path, monkeypatch, snapshot_fails):
    deployer = adapter(tmp_path, api_key="fixture")
    if snapshot_fails:

        def broken_snapshot(self, destination):
            raise OSError("snapshot unavailable")

        monkeypatch.setattr(NativeEventMirror, "snapshot", broken_snapshot)
    script = (
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])\n"
        "Path('child.pid').write_text(str(p.pid))\ntime.sleep(120)\n"
    )
    monkeypatch.setattr(deployer, "_build_argv", lambda: [sys.executable, "-c", script])
    task = asyncio.create_task(deployer.launch("test"))
    try:
        for _ in range(100):
            if (tmp_path / "child.pid").exists():
                break
            await asyncio.sleep(0.02)
        assert (tmp_path / "child.pid").exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert (tmp_path / "artifact_collection_error.json").exists() == snapshot_fails
        for name in ("muse.pid", "child.pid"):
            pid = int((tmp_path / name).read_text())
            stat = Path(f"/proc/{pid}/stat")
            assert not stat.exists() or stat.read_text().split()[2] == "Z"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_final_session_log_recovers_partial_mirror_and_excludes_prior_launch(tmp_path):
    records = FIXTURE.read_text().splitlines()
    (tmp_path / "native_events.jsonl").write_text("\n".join(records[:10]) + "\n")
    selected = tmp_path / "muse-data" / "current"
    selected.mkdir(parents=True)
    (selected / "session.jsonl").write_text(FIXTURE.read_text())
    stale = tmp_path / "muse-data" / "old"
    stale.mkdir()
    (stale / "session.jsonl").write_text(FIXTURE.read_text().replace('"id":', '"stale_id":'))
    (tmp_path / "native_data_path.json").write_text(json.dumps("muse-data/current"))
    assert recover_telemetry_artifacts(tmp_path)["usage"]["input_tokens"] == 200


def test_children_incomplete_calls_and_missing_logs(tmp_path):
    records = list(read_records(FIXTURE))
    root = records[0]["stream"]["id"]
    child = json.loads(json.dumps(records).replace(root, "child-session"))
    for record in child:
        record["id"] = "child-" + record["id"]
    child = [
        r
        for r in child
        if r.get("payload", {}).get("event", {}).get("kind")
        not in {
            "tool_result_batch_committed",
            "tool_result_model_visible_content",
            "terminal",
        }
    ]
    (tmp_path / "native_events.jsonl").write_text("\n".join(json.dumps(r) for r in records + child))
    builder = TrajectoryBuilder(agent_name="muse-code", task_path="test", variant_index=0)
    MuseCodeDeployer.parse_artifacts(
        work_dir=tmp_path,
        config=MuseCodeConfig(),
        run_result=AgentRunResult(status="completed"),
        builder=builder,
    )
    trajectory = builder.finalize(reward=None)
    assert trajectory.final_metrics.total_input_tokens == 400
    assert len(trajectory.subagent_trajectories) == 1
    nested = trajectory.subagent_trajectories[0]
    assert nested.final_metrics.total_input_tokens == 200
    assert nested.final_metrics.status == "failed"
    assert any(step.extra.get("status") == "incomplete" for step in nested.steps)
    (tmp_path / "native_events.jsonl").unlink()
    empty = TrajectoryBuilder(agent_name="muse-code", task_path="test", variant_index=0)
    MuseCodeDeployer.parse_artifacts(
        work_dir=tmp_path,
        config=MuseCodeConfig(telemetry_enabled=False),
        run_result=AgentRunResult(status="failed"),
        builder=empty,
    )
    assert empty.trajectory.steps[0].extra["reason"] == "no_native_events"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression")
@pytest.mark.asyncio
async def test_success_reaps_lingering_child_and_repeat_launch_isolates_logs(tmp_path, monkeypatch):
    deployer = adapter(tmp_path, api_key="fixture")
    script = (
        "import subprocess,sys,json,os\nfrom pathlib import Path\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])\n"
        "Path('child.pid').write_text(str(p.pid))\n"
        "Path('data_path.txt').write_text(os.environ['XDG_DATA_HOME'])\n"
        "print(json.dumps({'payload_type':'run.terminal.completed','payload':{'terminal':'completed'}}),flush=True)\n"
    )
    monkeypatch.setattr(deployer, "_build_argv", lambda: [sys.executable, "-c", script])
    result = await asyncio.wait_for(deployer.launch("test"), 15)
    assert result.status == "completed"
    old = (tmp_path / "native_data_path.json").read_text()
    (tmp_path / "artifact_collection_error.json").write_text("previous launch error")
    result = await asyncio.wait_for(deployer.launch("again"), 15)
    assert result.status == "completed"
    assert not (tmp_path / "artifact_collection_error.json").exists()
    assert old != (tmp_path / "native_data_path.json").read_text()
    assert (tmp_path / "data_path.txt").read_text() == str(tmp_path / "data-root")
    pid = int((tmp_path / "child.pid").read_text())
    stat = Path(f"/proc/{pid}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"


def test_mirror_excludes_old_sessions_and_snapshots_only_native_evidence(tmp_path):
    root = tmp_path / "native"
    old = root / "old" / "session.jsonl"
    old.parent.mkdir(parents=True)
    old.write_text("old session\n")
    current = root / "current" / "session.jsonl"
    mirror = NativeEventMirror(tmp_path, root, ("fixture-secret",), exclude={old})
    current.parent.mkdir()
    current.write_text('{"text":"fixture-secret"}\n')
    (current.parent / "trace.json").write_text("{}")
    mirror.poll()
    mirror.snapshot(tmp_path / "evidence")
    with zipfile.ZipFile(tmp_path / "evidence/native.zip") as archive:
        assert set(archive.namelist()) == {"current/trace.json", "current/session.jsonl"}
        assert "fixture-secret" not in archive.read("current/session.jsonl").decode()
    assert "old session" not in mirror.path.read_text()


def test_snapshot_retains_nested_evidence_and_recovers_partial_mirror(tmp_path):
    root = tmp_path / "native"
    session = root / "2026/10/19" / ("session-" + "a" * 36)
    nested = session / "subagent" / ("child-" + "b" * 36)
    nested.mkdir(parents=True)
    (session / "session.jsonl").write_text(FIXTURE.read_text())
    (nested / "session.jsonl").write_text('{"id":"child","payload":{"secret":"fixture-secret"}}\n')
    artifact = nested / "tool-outputs" / ("c" * 36) / "输出.png"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"\x89PNG\r\n")
    mirror = NativeEventMirror(tmp_path, root, ("fixture-secret",))
    mirror.poll()
    destination = tmp_path / "muse-data/current"
    mirror.snapshot(destination)
    with zipfile.ZipFile(destination / "native.zip") as archive:
        assert len(archive.namelist()) == len(set(archive.namelist())) == 3
        assert archive.read(artifact.relative_to(root).as_posix()) == artifact.read_bytes()
        assert b"fixture-secret" not in archive.read(
            (nested / "session.jsonl").relative_to(root).as_posix()
        )
    (tmp_path / "native_events.jsonl").write_text("")
    (tmp_path / "native_data_path.json").write_text(json.dumps("muse-data/current"))
    summary = recover_telemetry_artifacts(tmp_path)
    assert summary["usage"]["input_tokens"] == 200
    assert len(summary["api_calls"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("rc,terminal", [(0, "completed"), (1, "failed")])
async def test_snapshot_failure_preserves_agent_outcome(tmp_path, monkeypatch, rc, terminal):
    deployer = adapter(tmp_path, api_key="secret-fixture")
    event = {
        "payload_type": "run.terminal." + terminal,
        "payload": {"terminal": terminal, "reason": "provider failure"},
    }
    script = f"import sys; print({json.dumps(event)!r}); sys.exit({rc})"
    monkeypatch.setattr(deployer, "_build_argv", lambda: [sys.executable, "-c", script])

    def broken_snapshot(self, destination):
        raise OSError("snapshot unavailable secret-fixture")

    monkeypatch.setattr(NativeEventMirror, "snapshot", broken_snapshot)
    result = await deployer.launch("test")
    assert result.status == terminal
    assert result.exit_code == rc
    if rc:
        assert "provider failure" in result.error
    assert (tmp_path / "telemetry_summary.json").is_file()
    error = (tmp_path / "artifact_collection_error.json").read_text()
    assert "secret-fixture" not in error and "snapshot unavailable" in error
