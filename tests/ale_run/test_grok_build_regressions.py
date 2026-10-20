"""Regression tests for Grok Build 1.x desktop and trajectory behavior."""

import base64
import json
from types import SimpleNamespace

import pytest

from ale_run.agents.grok_build.config import GrokBuildConfig
from ale_run.agents.grok_build import deployer
from ale_run.base_interface import AgentRunResult, TrajectoryBuilder


@pytest.mark.asyncio
@pytest.mark.parametrize(("os_name", "expected_flags"), [("nt", 0x08000000), ("posix", 0)])
async def test_launch_suppresses_windows_console(monkeypatch, tmp_path, os_name, expected_flags):
    config = GrokBuildConfig(otel_enabled=False)
    executor = SimpleNamespace(
        config=config, work_dir=str(tmp_path), sandbox=SimpleNamespace(is_linux=True)
    )
    agent = deployer.GrokBuildDeployer(executor)
    monkeypatch.setattr(agent, "_build_argv", lambda *args, **kwargs: ["grok"])
    monkeypatch.setattr(agent, "_build_env", lambda *args, **kwargs: {})
    monkeypatch.setattr(agent, "_export_session_artifacts", lambda *args: None)
    monkeypatch.setattr(deployer, "os", SimpleNamespace(name=os_name))
    monkeypatch.setattr(deployer.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    launch_options = {}

    def popen(*args, **kwargs):
        launch_options.update(kwargs)
        return SimpleNamespace(pid=123, returncode=0, poll=lambda: 0)

    monkeypatch.setattr(deployer.subprocess, "Popen", popen)
    result = await agent.launch("hello")
    assert result.status == "completed"
    assert launch_options["creationflags"] == expected_flags


def test_extracted_user_screenshot_is_attached_to_tool_result(tmp_path):
    image = base64.b64encode(b"image bytes").decode()
    events = [
        {
            "type": "assistant",
            "tool_calls": [{"id": "c1", "name": "cua__screenshot", "arguments": "{}"}],
        },
        {"type": "tool_result", "tool_call_id": "c1", "content": "Screenshot captured"},
        {
            "type": "user",
            "content": [
                {"type": "text", "text": "[Image extracted from tool result above]"},
                {"type": "image", "url": f"data:image/png;base64,{image}"},
            ],
        },
    ]
    history = tmp_path / "chat_history.jsonl"
    history.write_text("\n".join(json.dumps(event) for event in events))
    builder = TrajectoryBuilder(agent_name="grok_build", task_path="demo", variant_index=0)
    assert deployer.GrokBuildDeployer._parse_chat_history(
        history, builder, tool_errors={}, session_dir=None, media_results={}
    )
    assert len(builder.trajectory.steps) == 2
    result = builder.trajectory.steps[-1].observation.results[0]
    assert result.tool_call_id == "c1"
    assert result.content[-1].image.data == image


def test_otel_recovers_later_calls_without_using_partial_cost(tmp_path):
    calls = [
        {"session.id": "s1", "input_tokens": 100, "cache_read_tokens": 80, "output_tokens": 10}
        for _ in range(3)
    ]
    (tmp_path / "telemetry_summary.json").write_text(json.dumps({"api_calls": calls}))
    terminal = {
        "sessionId": "s1",
        "num_turns": 2,
        "usage": {"input_tokens": 40, "cache_read_input_tokens": 160, "output_tokens": 20},
        "total_cost_usd": 1.0,
    }
    usage = deployer.GrokBuildDeployer._later_otel_usage(tmp_path, terminal)
    assert usage == {"input_tokens": 60, "cache_read_input_tokens": 240, "output_tokens": 30}


def test_otel_does_not_replace_terminal_when_incomplete_or_wrong_session(tmp_path):
    terminal = {
        "sessionId": "s1",
        "num_turns": 1,
        "usage": {"input_tokens": 100, "cache_read_input_tokens": 200, "output_tokens": 30},
    }
    call = {"session.id": "s2", "input_tokens": 100, "cache_read_tokens": 50, "output_tokens": 5}
    (tmp_path / "telemetry_summary.json").write_text(json.dumps({"api_calls": [call, call]}))
    assert deployer.GrokBuildDeployer._later_otel_usage(tmp_path, terminal) is None
    call["session.id"] = "s1"
    (tmp_path / "telemetry_summary.json").write_text(json.dumps({"api_calls": [call, call]}))
    assert deployer.GrokBuildDeployer._later_otel_usage(tmp_path, terminal) is None


@pytest.mark.parametrize("reported_cost", [None, 0.0, 0.25])
def test_terminal_cost_distinguishes_unavailable_from_zero(tmp_path, reported_cost):
    terminal = {
        "type": "end", "sessionId": "s1", "num_turns": 1,
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 20, "output_tokens": 3},
    }
    if reported_cost is not None:
        terminal["total_cost_usd"] = reported_cost
    (tmp_path / "transcript.jsonl").write_text(json.dumps(terminal) + "\n")
    builder = TrajectoryBuilder(agent_name="grok_build", task_path="demo", variant_index=0)
    deployer.GrokBuildDeployer.parse_artifacts(
        work_dir=tmp_path, config=GrokBuildConfig(),
        run_result=AgentRunResult(status="completed", exit_code=0), builder=builder,
    )
    metrics = builder.finalize(reward=None).final_metrics
    assert metrics.total_cost_usd == reported_cost
    assert metrics.total_input_tokens == 10


def test_later_otel_calls_make_terminal_cost_unavailable(tmp_path, monkeypatch):
    terminal = {
        "type": "end", "sessionId": "s1", "num_turns": 1,
        "usage": {"input_tokens": 20, "cache_read_input_tokens": 80, "output_tokens": 10},
        "total_cost_usd": 1.0,
    }
    (tmp_path / "transcript.jsonl").write_text(json.dumps(terminal) + "\n")
    calls = [
        {"session.id": "s1", "input_tokens": 100, "cache_read_tokens": 80, "output_tokens": 10}
        for _ in range(2)
    ]
    (tmp_path / "telemetry_summary.json").write_text(json.dumps({"api_calls": calls}))
    monkeypatch.setattr(deployer, "recover_otel_artifacts", lambda work_dir: None)
    builder = TrajectoryBuilder(agent_name="grok_build", task_path="demo", variant_index=0)
    deployer.GrokBuildDeployer.parse_artifacts(
        work_dir=tmp_path, config=GrokBuildConfig(),
        run_result=AgentRunResult(status="completed", exit_code=0), builder=builder,
    )
    metrics = builder.finalize(reward=None).final_metrics
    assert metrics.total_cost_usd is None
    assert (metrics.total_input_tokens, metrics.total_cache_read_tokens) == (40, 160)
