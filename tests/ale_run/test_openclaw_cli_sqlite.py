from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import sqlite3
import subprocess
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
