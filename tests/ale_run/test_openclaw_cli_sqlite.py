from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest

from ale_run.agents.openclaw_cli import OpenClawCliConfig, OpenClawCliDeployer
from ale_run.base_interface.agent_deployer import AgentRunResult
from ale_run.base_interface.trajectory import TrajectoryBuilder


def test_sqlite_export_preserves_compressed_screenshot(tmp_path, monkeypatch):
    node = os.environ.get("ALE_OPENCLAW_TEST_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Node with sqlite and zstd support is unavailable")
    probe = subprocess.run(
        [node, "-e", "require('node:sqlite'); require('node:zlib').zstdCompressSync('test')"],
        capture_output=True,
    )
    if probe.returncode:
        pytest.skip("Node with sqlite and zstd support is unavailable")

    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / ".openclaw/agents/main/agent/openclaw-agent.sqlite"
    db_path.parent.mkdir(parents=True)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+"
        "nWb0AAAAASUVORK5CYII="
    )
    call = {
        "type": "message",
        "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "call-1", "name": "screenshot", "arguments": {}}
        ]},
    }
    result = {
        "type": "message",
        "message": {"role": "toolResult", "toolCallId": "call-1", "content": [
            {"type": "image", "mimeType": "image/png", "data": base64.b64encode(png).decode()}
        ]},
    }
    compressed = subprocess.run(
        [node, "-e", "process.stdout.write(require('node:zlib').zstdCompressSync(process.argv[1]))", json.dumps(result)],
        capture_output=True, check=True,
    ).stdout
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, event_json TEXT, event_zstd BLOB)")
        db.execute("INSERT INTO transcript_events VALUES (?, ?, ?, ?)", ("session-1", 1, json.dumps(call), None))
        db.execute("INSERT INTO transcript_events VALUES (?, ?, ?, ?)", ("session-1", 2, None, compressed))
        db.execute("INSERT INTO transcript_events VALUES (?, ?, ?, ?)", ("other", 1, "{}", None))

    config = OpenClawCliConfig()
    deployer = OpenClawCliDeployer(SimpleNamespace(config=config))
    deployer._node_path = node
    work_dir = tmp_path / "run" / "origin_log" / "openclaw-cli"
    work_dir.mkdir(parents=True)
    transcript = work_dir / "transcript.jsonl"
    asyncio.run(deployer._export_session_transcript("session-1", transcript))
    assert [json.loads(line) for line in transcript.read_text().splitlines()] == [call, result]

    builder = TrajectoryBuilder(agent_name="openclaw-cli", model="test", task_path="demo", variant_index=0)
    deployer.parse_artifacts(
        work_dir=work_dir, config=config,
        run_result=AgentRunResult(status="completed", exit_code=0), builder=builder,
    )
    steps = builder.trajectory.steps
    assert steps[0].tool_calls[0].id == steps[1].observation.results[0].tool_call_id == "call-1"
    image = steps[1].observation.results[0].content[0].image
    assert image.type == "path"
    assert (tmp_path / "run" / image.path).read_bytes() == png


def test_deleted_nested_subagent_archives_are_exported(tmp_path, monkeypatch):
    node = os.environ.get("ALE_OPENCLAW_TEST_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Node with sqlite and zstd support is unavailable")
    probe = subprocess.run(
        [node, "-e", "require('node:sqlite'); require('node:zlib').zstdCompressSync('test')"],
        capture_output=True,
    )
    if probe.returncode:
        pytest.skip("Node with sqlite and zstd support is unavailable")
    monkeypatch.setenv("HOME", str(tmp_path))
    database = tmp_path / ".openclaw/agents/main/agent/openclaw-agent.sqlite"
    database.parent.mkdir(parents=True)
    main = "agent:main:explicit:parent"
    child = "agent:main:subagent:child"
    grandchild = "agent:main:subagent:grandchild"
    child_id, grandchild_id = str(uuid.uuid4()), str(uuid.uuid4())

    def spawn(key):
        return {"type": "message", "message": {
            "role": "toolResult", "details": {"result": {"details": {
                "status": "accepted", "childSessionKey": key,
            }}}, "content": [],
        }}

    def compressed(events):
        content = "\n".join(json.dumps(event) for event in events) + "\n"
        return subprocess.run(
            [node, "-e", "process.stdout.write(require('node:zlib').zstdCompressSync(process.argv[1]))", content],
            capture_output=True, check=True,
        ).stdout

    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE session_nodes (session_key TEXT, current_session_id TEXT, "
                   "parent_session_key TEXT, status TEXT, entry_json TEXT, created_at INTEGER)")
        db.execute("CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, event_json TEXT, event_zstd BLOB)")
        db.execute("CREATE TABLE session_transcript_archives (session_key TEXT, session_id TEXT, "
                   "reason TEXT, generation INTEGER, encoding TEXT, archive_blob BLOB, created_at INTEGER)")
        db.execute("INSERT INTO session_transcript_archives VALUES (?, ?, 'deleted', 1, 'zstd', ?, 1)",
                   (child, child_id, compressed([spawn(grandchild)])))
        db.execute("INSERT INTO session_transcript_archives VALUES (?, ?, 'deleted', 1, 'zstd', ?, 2)",
                   (grandchild, grandchild_id, compressed([{"type": "message", "message": {
                       "role": "assistant", "content": [{"type": "text", "text": "finished"}],
                   }}])))
    work_dir = tmp_path / "run"
    work_dir.mkdir()
    (work_dir / "transcript.jsonl").write_text(json.dumps(spawn(child)) + "\n")
    deployer = OpenClawCliDeployer(SimpleNamespace(config=OpenClawCliConfig()))
    deployer._node_path = node
    asyncio.run(deployer._export_child_transcripts(work_dir, {
        "meta": {"agentMeta": {"sessionFile": main}},
        "acceptedSessionSpawns": [{"childSessionKey": child}],
    }))
    records = {record["session_key"]: record for record in json.loads(
        (work_dir / "subagents/index.json").read_text()
    )}
    assert records[child]["parent_session_key"] == main
    assert records[grandchild]["parent_session_key"] == child
    assert json.loads((work_dir / "subagents" / records[grandchild]["transcript"]).read_text())[
        "message"
    ]["content"][0]["text"] == "finished"


async def test_timeout_recovery_exports_main_and_child_usage(tmp_path, monkeypatch):
    node = os.environ.get("ALE_OPENCLAW_TEST_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Node with sqlite and zstd support is unavailable")
    probe = await asyncio.to_thread(
        subprocess.run, [node, "-e", "require('node:sqlite'); require('node:zlib').zstdCompressSync('test')"],
        capture_output=True, check=False,
    )
    if probe.returncode:
        pytest.skip("Node with sqlite and zstd support is unavailable")
    state = tmp_path / "state"
    database = state / "agents/main/agent/openclaw-agent.sqlite"
    database.parent.mkdir(parents=True)
    work_dir = tmp_path / "work dir"
    work_dir.mkdir()
    main_id, child_id = str(uuid.uuid4()), str(uuid.uuid4())
    (work_dir / "_openclaw_session.json").write_text(json.dumps({
        "session_id": main_id, "state_dir": str(state), "node_path": node,
    }))
    (work_dir / "transcript.jsonl").write_text('{"stale": true}\n')
    (work_dir / "gateway.log").write_text("[model-fetch] response status=200\n")
    (work_dir / "subagents").mkdir()
    (work_dir / "subagents" / "stale.jsonl").write_text('{"stale": true}\n')

    def message(input_tokens, output_tokens, cache_read, cache_write):
        return {"type": "message", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "working"}],
            "usage": {"input": input_tokens, "output": output_tokens,
                      "cacheRead": cache_read, "cacheWrite": cache_write},
        }}

    main_event = message(123, 12, 45, 6)
    child_event = message(30, 4, 8, 2)
    compressed = (await asyncio.to_thread(
        subprocess.run,
        [node, "-e", "process.stdout.write(require('node:zlib').zstdCompressSync(process.argv[1]))",
         json.dumps(child_event)], capture_output=True, check=True,
    )).stdout
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, event_json TEXT, event_zstd BLOB)")
        db.execute("CREATE TABLE session_nodes (session_key TEXT, current_session_id TEXT, "
                   "parent_session_key TEXT, status TEXT, entry_json TEXT, created_at INTEGER)")
        db.execute("CREATE TABLE session_transcript_archives (session_key TEXT, session_id TEXT, "
                   "reason TEXT, generation INTEGER, encoding TEXT, archive_blob BLOB, created_at INTEGER)")
        db.execute("INSERT INTO transcript_events VALUES (?, 1, ?, NULL)", (main_id, json.dumps(main_event)))
        db.execute("INSERT INTO transcript_events VALUES (?, 1, NULL, ?)", (child_id, compressed))
        db.execute("INSERT INTO session_nodes VALUES (?, ?, ?, 'running', '{}', 1)",
                   ("agent:main:subagent:child", child_id, "agent:main:parent"))

    (tmp_path / ".ale-src").symlink_to(os.getcwd(), target_is_directory=True)

    class Sandbox:
        is_linux = True
        python = sys.executable
        work_dir_base = str(tmp_path / ".ale")

        async def run_command(self, command, *, timeout):
            return await asyncio.to_thread(
                subprocess.run, command, shell=True, capture_output=True, text=True, timeout=timeout,
            )

    manifest = await OpenClawCliDeployer.recover_stopped_artifacts(Sandbox(), str(work_dir))
    OpenClawCliDeployer.validate_recovered_artifacts(work_dir, manifest)
    assert set(manifest) == {"transcript.jsonl", "subagents/index.json", f"subagents/{child_id}.jsonl", "gateway.log"}
    assert json.loads((work_dir / "transcript.jsonl").read_text()) == main_event
    assert not (work_dir / "subagents" / "stale.jsonl").exists()
    builder = TrajectoryBuilder(agent_name="openclaw-cli", task_path="demo", variant_index=0)
    OpenClawCliDeployer.parse_artifacts(
        work_dir=work_dir, config=OpenClawCliConfig(),
        run_result=AgentRunResult(status="timeout"), builder=builder,
    )
    metrics = builder.finalize(reward=0, status="timeout").final_metrics
    assert (metrics.total_input_tokens, metrics.total_output_tokens) == (153, 16)
    assert (metrics.total_cache_read_tokens, metrics.total_cache_creation_tokens) == (53, 8)


async def test_timeout_recovery_fails_when_session_export_is_unavailable(tmp_path, monkeypatch):
    session_id = str(uuid.uuid4())
    state = tmp_path / "state"
    database = state / "agents/main/agent/openclaw-agent.sqlite"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, event_json TEXT, event_zstd BLOB)")
        db.execute("INSERT INTO transcript_events VALUES (?, 1, '{}', NULL)", (session_id,))
    (tmp_path / "_openclaw_session.json").write_text(json.dumps({
        "session_id": session_id, "state_dir": str(state), "node_path": "missing-node",
    }))
    (tmp_path / "transcript.jsonl").write_text('{"previous": true}\n')

    async def failed_export(*args):
        return None

    monkeypatch.setattr(OpenClawCliDeployer, "_export_session_transcript", failed_export)
    with pytest.raises(RuntimeError, match="session transcript could not be recovered"):
        await OpenClawCliDeployer._recover_session(tmp_path)
    assert json.loads((tmp_path / "transcript.jsonl").read_text()) == {"previous": True}


async def test_timeout_recovery_rejects_missing_transcript_after_agent_started(tmp_path):
    (tmp_path / "_openclaw_session.json").write_text(json.dumps({
        "session_id": str(uuid.uuid4()), "state_dir": str(tmp_path / "state"), "node_path": "node",
    }))
    (tmp_path / "openclaw.pid").write_text("123")
    with pytest.raises(RuntimeError, match="session transcript could not be recovered"):
        await OpenClawCliDeployer._recover_session(tmp_path)


async def test_recovery_rejects_gateway_failure_before_agent_starts(tmp_path):
    (tmp_path / "_openclaw_session.json").write_text(json.dumps({
        "session_id": str(uuid.uuid4()), "state_dir": str(tmp_path / "state"), "node_path": "node",
    }))
    with pytest.raises(RuntimeError, match="agent process did not start"):
        await OpenClawCliDeployer._recover_session(tmp_path)


async def test_timeout_recovery_rejects_missing_usage_after_model_response(tmp_path, monkeypatch):
    (tmp_path / "_openclaw_session.json").write_text(json.dumps({
        "session_id": str(uuid.uuid4()), "state_dir": str(tmp_path / "state"), "node_path": "node",
    }))
    (tmp_path / "gateway.log").write_text("[model-fetch] response status=200\n")

    async def save_without_usage(self, wd, session_id, envelope):
        (wd / "transcript.jsonl").write_text(json.dumps({"type": "message", "message": {"role": "assistant"}}))
        (wd / "subagents").mkdir()
        (wd / "subagents/index.json").write_text("[]")

    monkeypatch.setattr(OpenClawCliDeployer, "_save_transcripts", save_without_usage)
    with pytest.raises(RuntimeError, match="model usage could not be recovered"):
        await OpenClawCliDeployer._recover_session(tmp_path)


async def test_recovery_detects_missing_child_from_transcript_without_envelope(tmp_path):
    state = tmp_path / "state"
    database = state / "agents/main/agent/openclaw-agent.sqlite"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE session_nodes (session_key TEXT, current_session_id TEXT, "
                   "parent_session_key TEXT, status TEXT, entry_json TEXT, created_at INTEGER)")
    session_id = str(uuid.uuid4())
    (tmp_path / "_openclaw_session.json").write_text(json.dumps({
        "session_id": session_id, "state_dir": str(state), "node_path": "node",
    }))
    session = state / "agents/main/sessions" / f"{session_id}.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(json.dumps({"type": "message", "message": {
        "role": "toolResult", "details": {"result": {"details": {
            "status": "accepted", "childSessionKey": "agent:main:subagent:missing",
        }}},
    }}) + "\n")
    with pytest.raises(RuntimeError, match="subagent transcripts could not be recovered"):
        await OpenClawCliDeployer._recover_session(tmp_path)


@pytest.mark.parametrize("contents", ["{invalid\n", "[]\n", ""])
async def test_recovery_rejects_invalid_or_empty_transcript(tmp_path, monkeypatch, contents):
    (tmp_path / "_openclaw_session.json").write_text(json.dumps({
        "session_id": str(uuid.uuid4()), "state_dir": str(tmp_path / "state"), "node_path": "node",
    }))

    async def save_invalid(self, wd, session_id, envelope):
        (wd / "transcript.jsonl").write_text(contents)
        (wd / "subagents").mkdir()
        (wd / "subagents/index.json").write_text("[]")

    monkeypatch.setattr(OpenClawCliDeployer, "_save_transcripts", save_invalid)
    with pytest.raises((RuntimeError, ValueError)):
        await OpenClawCliDeployer._recover_session(tmp_path)


async def test_recovery_rejects_missing_usage_in_later_child(tmp_path, monkeypatch):
    (tmp_path / "_openclaw_session.json").write_text(json.dumps({
        "session_id": str(uuid.uuid4()), "state_dir": str(tmp_path / "state"), "node_path": "node",
    }))

    async def save_partial(self, wd, session_id, envelope):
        (wd / "transcript.jsonl").write_text(json.dumps({"type": "message", "message": {
            "role": "assistant", "model": "test", "usage": {"input": 2, "output": 1},
        }}))
        (wd / "subagents").mkdir()
        (wd / "subagents/index.json").write_text(json.dumps([{
            "session_key": "child", "transcript": "child.jsonl",
        }]))
        (wd / "subagents/child.jsonl").write_text(json.dumps({"type": "message", "message": {
            "role": "assistant", "model": "test", "usage": {"input": 4},
        }}))

    monkeypatch.setattr(OpenClawCliDeployer, "_save_transcripts", save_partial)
    with pytest.raises(RuntimeError, match="model usage could not be recovered"):
        await OpenClawCliDeployer._recover_session(tmp_path)


async def test_timeout_recovery_quotes_windows_paths():
    class Sandbox:
        is_linux = False
        python = r"C:\Program Files\Python\python.exe"
        work_dir_base = r"C:\Users\Test User\.ale"

        async def run_command(self, command, *, timeout):
            assert command.startswith('"C:\\Program Files\\Python\\python.exe"')
            assert '"C:\\Users\\Test User\\.ale-src"' in command
            assert '"C:\\Users\\Test User\\.ale\\run folder"' in command
            assert timeout == 180
            return subprocess.CompletedProcess(command, 0, "OPENCLAW_TIMEOUT_ARTIFACTS={}\n", "")

    await OpenClawCliDeployer.recover_stopped_artifacts(
        Sandbox(), r"C:\Users\Test User\.ale\run folder",
    )


@pytest.mark.parametrize(("output", "error"), [
    ("", RuntimeError),
    ("OPENCLAW_TIMEOUT_ARTIFACTS=broken\n", RuntimeError),
    ("OPENCLAW_TIMEOUT_ARTIFACTS=[]\n", TypeError),
])
async def test_timeout_recovery_rejects_missing_or_invalid_manifest(output, error):
    class Sandbox:
        is_linux = True
        python = "python3"
        work_dir_base = "/home/user/.ale"

        async def run_command(self, command, *, timeout):
            return subprocess.CompletedProcess(command, 0, output, "")

    with pytest.raises(error, match="manifest"):
        await OpenClawCliDeployer.recover_stopped_artifacts(Sandbox(), "/home/user/.ale/work")


@pytest.mark.parametrize("changed", ["transcript.jsonl", "subagents/index.json", "subagents/child.jsonl", "vision-usage.jsonl", "stdout.log", "stderr.log", "gateway.log"])
@pytest.mark.parametrize("mode", ["missing", "truncated"])
def test_timeout_recovery_rejects_missing_or_truncated_gather(tmp_path, changed, mode):
    for name in ("transcript.jsonl", "subagents/index.json", "subagents/child.jsonl", "vision-usage.jsonl", "stdout.log", "stderr.log", "gateway.log"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("complete")
    manifest = OpenClawCliDeployer._recovered_artifact_manifest(tmp_path)
    path = tmp_path / changed
    if mode == "missing":
        path.unlink()
    else:
        path.write_text("part")
    with pytest.raises(RuntimeError, match="artifact gather failed"):
        OpenClawCliDeployer.validate_recovered_artifacts(tmp_path, manifest)


def test_timeout_recovery_allows_no_session(tmp_path):
    assert OpenClawCliDeployer._recovered_artifact_manifest(tmp_path) == {}
    OpenClawCliDeployer.validate_recovered_artifacts(tmp_path, {})


async def test_timeout_recovery_reads_wal_after_writer_is_killed(tmp_path):
    node = os.environ.get("ALE_OPENCLAW_TEST_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Node with sqlite and zstd support is unavailable")
    probe = await asyncio.to_thread(
        subprocess.run, [node, "-e", "require('node:sqlite'); require('node:zlib').zstdCompressSync('test')"],
        capture_output=True, check=False,
    )
    if probe.returncode:
        pytest.skip("Node with sqlite and zstd support is unavailable")
    session_id = str(uuid.uuid4())
    state = tmp_path / "state"
    database = state / "agents/main/agent/openclaw-agent.sqlite"
    database.parent.mkdir(parents=True)
    (tmp_path / "_openclaw_session.json").write_text(json.dumps({
        "session_id": session_id, "state_dir": str(state), "node_path": node,
    }))
    message = {"type": "message", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "working"}],
        "usage": {"input": 37, "output": 5},
    }}
    script = """
import json, sqlite3, sys, time
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA journal_mode=WAL')
db.execute('PRAGMA wal_autocheckpoint=0')
db.execute('CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, event_json TEXT, event_zstd BLOB)')
db.execute('CREATE TABLE session_nodes (session_key TEXT, current_session_id TEXT, parent_session_key TEXT, status TEXT, entry_json TEXT, created_at INTEGER)')
db.execute('INSERT INTO transcript_events VALUES (?, 1, ?, NULL)', (sys.argv[2], sys.argv[3]))
db.commit()
print('ready', flush=True)
time.sleep(300)
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, str(database), session_id, json.dumps(message),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b"ready\n"
    finally:
        process.kill()
        await asyncio.wait_for(process.communicate(), 10)
    assert database.with_name(database.name + "-wal").exists()
    await OpenClawCliDeployer._recover_session(tmp_path)
    assert json.loads((tmp_path / "transcript.jsonl").read_text()) == message
