from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from ale_run.agents.openclaw_cli import OpenClawCliConfig, OpenClawCliDeployer
from ale_run.agents.openclaw_cli import deployer as module
from ale_run.base_interface.agent_deployer import AgentRunResult
from ale_run.base_interface.trajectory import TrajectoryBuilder


def _deployer(tmp_path):
    deployer = OpenClawCliDeployer(SimpleNamespace(config=OpenClawCliConfig()))
    deployer._episode_state_dir = tmp_path / "state"
    return deployer


def _databases(deployer):
    state = deployer._state_dir / "state" / "openclaw.sqlite"
    agent = deployer._state_dir / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
    state.parent.mkdir(parents=True)
    agent.parent.mkdir(parents=True)
    with sqlite3.connect(state) as db:
        db.execute("CREATE TABLE subagent_runs (run_id TEXT, payload_json TEXT)")
    with sqlite3.connect(agent) as db:
        db.execute("CREATE TABLE session_nodes (status TEXT, session_key TEXT, entry_json TEXT)")
        db.execute("CREATE TABLE session_transcript_archives (session_key TEXT, reason TEXT)")
        db.execute("CREATE TABLE transcript_events (event_json TEXT)")
    return state, agent


def test_wait_for_child_and_requester_continuation(tmp_path, monkeypatch):
    deployer = _deployer(tmp_path)
    state, agent = _databases(deployer)
    running = {"execution": {"status": "running"}, "delivery": {"status": "pending"}}
    with sqlite3.connect(state) as db:
        db.execute("INSERT INTO subagent_runs VALUES (?, ?)", ("run-1", json.dumps(running)))
    monkeypatch.setattr(module, "_SUBAGENT_POLL_S", 0.01)
    monkeypatch.setattr(module, "_SUBAGENT_SETTLE_S", 0.04)

    async def exercise():
        waiter = asyncio.create_task(deployer._wait_for_subagents({
            "acceptedSessionSpawns": [{"runId": "run-1"}],
        }))
        await asyncio.sleep(0.03)
        assert not waiter.done()
        with sqlite3.connect(state) as db:
            db.execute("UPDATE subagent_runs SET payload_json = ?", (json.dumps({
                "execution": {"status": "terminal"}, "delivery": {"status": "delivered"},
            }),))
        with sqlite3.connect(agent) as db:
            db.execute("INSERT INTO session_nodes (status) VALUES ('running')")
        await asyncio.sleep(0.07)
        assert not waiter.done()
        with sqlite3.connect(agent) as db:
            db.execute("UPDATE session_nodes SET status = 'done'")
        await asyncio.wait_for(waiter, 1)

    asyncio.run(exercise())


def test_wait_for_deleted_child_and_parent_continuation(tmp_path, monkeypatch):
    deployer = _deployer(tmp_path)
    _, agent = _databases(deployer)
    with sqlite3.connect(agent) as db:
        db.execute("INSERT INTO session_transcript_archives VALUES (?, 'deleted')", ("child-key",))
        db.execute("INSERT INTO session_nodes VALUES ('done', 'main', '{}')")
    monkeypatch.setattr(module, "_SUBAGENT_POLL_S", 0.01)
    monkeypatch.setattr(module, "_SUBAGENT_SETTLE_S", 0.04)

    async def exercise():
        waiter = asyncio.create_task(deployer._wait_for_subagents({
            "acceptedSessionSpawns": [{
                "runId": "run-1", "childSessionKey": "child-key", "expectsCompletionMessage": True,
            }],
        }))
        await asyncio.sleep(0.08)
        assert not waiter.done()
        with sqlite3.connect(agent) as db:
            db.execute("UPDATE session_nodes SET status = 'running', entry_json = ?", (
                json.dumps({"activeWriterRunId": "announce:run-1"}),
            ))
        await asyncio.sleep(0.05)
        assert not waiter.done()
        with sqlite3.connect(agent) as db:
            db.execute("UPDATE session_nodes SET status = 'done', entry_json = ?", (
                json.dumps({"lastRunId": "announce:run-1"}),
            ))
        await asyncio.wait_for(waiter, 1)

    asyncio.run(exercise())


def test_unreadable_subagent_state_fails_instead_of_hanging(tmp_path, monkeypatch):
    deployer = _deployer(tmp_path)
    monkeypatch.setattr(module, "_SUBAGENT_POLL_S", 0.01)
    monkeypatch.setattr(module, "_STATE_READ_TIMEOUT_S", 0.03)
    with pytest.raises(RuntimeError, match="subagent state could not be read"):
        asyncio.run(asyncio.wait_for(deployer._wait_for_subagents(None), 1))


def test_missing_accepted_child_is_recorded(tmp_path):
    deployer = _deployer(tmp_path)
    database = deployer._state_dir / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE session_nodes (session_key TEXT, current_session_id TEXT, "
                   "parent_session_key TEXT, status TEXT, entry_json TEXT, created_at INTEGER)")
    asyncio.run(deployer._export_child_transcripts(tmp_path, {
        "acceptedSessionSpawns": [{"childSessionKey": "agent:main:subagent:missing"}],
    }))
    assert json.loads((tmp_path / "subagents" / "index.json").read_text()) == [
        {"session_key": "agent:main:subagent:missing", "transcript": None},
    ]


def _message(tokens, cost):
    return {"type": "message", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "done"}],
        "usage": {"input": tokens, "output": 1, "cost": {"total": cost}},
    }}


@pytest.mark.parametrize("envelope_cost", [0.7, None])
def test_child_trajectory_and_usage_include_continuation(tmp_path, envelope_cost):
    (tmp_path / "transcript.jsonl").write_text("\n".join(json.dumps(_message(n, c)) for n, c in [
        (10, 0.5), (3, 0.2),
    ]))
    usage = {"input": 10, "output": 1}
    if envelope_cost is not None:
        usage["cost"] = {"total": envelope_cost}
    wrapped = {"status": "ok", "result": {"payloads": [], "meta": {
        "durationMs": 1, "agentMeta": {"usage": usage},
    }}}
    (tmp_path / "stdout.log").write_text(json.dumps(wrapped))
    children = tmp_path / "subagents"
    children.mkdir()
    (children / "index.json").write_text(json.dumps([
        {"session_key": "child", "parent_session_key": "main", "transcript": "child.jsonl"},
        {"session_key": "grandchild", "parent_session_key": "child", "transcript": "grandchild.jsonl"},
    ]))
    (children / "child.jsonl").write_text(json.dumps(_message(4, 0.3)))
    (children / "grandchild.jsonl").write_text(json.dumps(_message(2, 0.1)))
    builder = TrajectoryBuilder(agent_name="openclaw-cli", task_path="demo", variant_index=0)
    OpenClawCliDeployer.parse_artifacts(
        work_dir=tmp_path, config=OpenClawCliConfig(),
        run_result=AgentRunResult(status="completed", exit_code=0), builder=builder,
    )
    trajectory = builder.finalize(reward=None)
    assert trajectory.final_metrics.total_input_tokens == 19
    assert trajectory.final_metrics.total_output_tokens == 4
    assert trajectory.final_metrics.total_cost_usd == pytest.approx(1.1)
    child = trajectory.subagent_trajectories[0]
    assert child.extra["openclaw_cli"]["session_key"] == "child"
    assert child.subagent_trajectories[0].extra["openclaw_cli"]["session_key"] == "grandchild"


def test_unavailable_child_index_marks_cost_unknown(tmp_path):
    (tmp_path / "transcript.jsonl").write_text(json.dumps(_message(10, 0.5)))
    children = tmp_path / "subagents"
    children.mkdir()
    (children / "index.json").write_text('{"error": "unavailable"}')
    builder = TrajectoryBuilder(agent_name="openclaw-cli", task_path="demo", variant_index=0)
    OpenClawCliDeployer.parse_artifacts(
        work_dir=tmp_path, config=OpenClawCliConfig(),
        run_result=AgentRunResult(status="completed", exit_code=0), builder=builder,
    )
    assert builder.finalize(reward=None).final_metrics.total_cost_usd is None


def test_archived_child_uses_actual_model_from_transcript(tmp_path):
    children = tmp_path / "subagents"
    children.mkdir()
    (children / "index.json").write_text(json.dumps([
        {"session_key": "child", "status": "archived", "transcript": "child.jsonl"},
    ]))
    message = _message(4, 0.3)
    message["message"].update({"model": "vendor/child-model", "provider": "openrouter"})
    (children / "child.jsonl").write_text(json.dumps(message))
    builder = TrajectoryBuilder(
        agent_name="openclaw-cli", model="vendor/parent-model", task_path="demo", variant_index=0,
    )
    OpenClawCliDeployer.parse_artifacts(
        work_dir=tmp_path, config=OpenClawCliConfig(),
        run_result=AgentRunResult(status="completed", exit_code=0), builder=builder,
    )
    child = builder.finalize(reward=None).subagent_trajectories[0]
    assert child.agent.model == "vendor/child-model"
    assert child.extra["openclaw_cli"]["provider"] == "openrouter"
