"""Qwen harness contracts, including an opt-in unmodified CLI integration test."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ale_run.agents.qwen_code import QwenCodeConfig, QwenCodeDeployer, runtime
from ale_run.agents.qwen_code.telemetry import TelemetryMirror
from ale_run.base_interface import AgentRunResult, TrajectoryBuilder


def adapter(tmp_path, **kwargs):
    cfg = QwenCodeConfig(api_key="test-secret-credential", **kwargs)
    executor = SimpleNamespace(
        config=cfg,
        work_dir=str(tmp_path / "work"),
        env={},
        sandbox=SimpleNamespace(
            is_linux=os.name != "nt", node=sys.executable, mcp_server_dir=str(tmp_path / "bridge")
        ),
        cua_bridge_url=lambda: "http://127.0.0.1:8000",
    )
    return QwenCodeDeployer(executor)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cli_version": "latest"},
        {"auth_type": "oauth"},
        {"coordinate_space": "guess"},
        {"max_turns": 0},
        {"max_tool_calls": -1},
        {"context_window": True},
        {"reasoning_effort": "unknown"},
        {"auto_compact_token_limit": 50},
        {"auto_compact_token_limit": 110, "context_window": 100},
    ],
)
def test_invalid_config(kwargs):
    with pytest.raises(ValueError):
        QwenCodeConfig(**kwargs)


@pytest.mark.parametrize("linux", [True, False])
def test_settings_and_environment(tmp_path, monkeypatch, linux):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", "real-desktop-profile")
    monkeypatch.setenv("QWEN_TELEMETRY_OTLP_ENDPOINT", "https://unwanted.invalid")
    d = adapter(
        tmp_path,
        context_window=100000,
        auto_compact_token_limit=80000,
        max_output_tokens=1024,
        reasoning_effort="high",
        auth_type="openai-responses",
        extra_envs={"OPENAI_BASE_URL": "https://unwanted.invalid", "CUSTOM": "ok"},
    )
    d.executor.sandbox.is_linux = linux
    if not linux:
        d.executor.sandbox.mcp_server_dir = r"C:\Users\User\cua_mcp_server"
        d.executor.sandbox.node = r"C:\Node\node.exe"
    d._prepare_run()
    d._node_path = "/private/node"
    d._qwen_path = "/private/cli.js"
    env = d._build_env(d.config)
    assert env["HOME"] == "real-desktop-profile"
    assert "QWEN_TELEMETRY_OTLP_ENDPOINT" not in env
    assert env["OPENAI_BASE_URL"] == d.config.base_url
    assert env["CUSTOM"] == "ok"
    settings = json.loads((d._state_dir / "settings.json").read_text(encoding="utf-8"))
    assert settings["context"]["autoCompactThreshold"] == 0.8
    assert settings["model"]["generationConfig"]["contextWindowSize"] == 100000
    assert settings["model"]["generationConfig"]["samplingParams"]["max_tokens"] == 1024
    assert settings["model"]["reasoningEffort"] == "high"
    cua = settings["mcpServers"]["cua"]
    assert cua["env"]["CUA_SERVER_PLATFORM"] == ("linux" if linux else "win32")
    assert cua["env"]["CUA_COORDINATE_SPACE"] == "pixel"
    assert cua["env"]["CUA_SERVER_URL"].endswith(":8000")
    assert d.config.api_key not in json.dumps(settings)
    assert len(" ".join(d._build_argv("x" * 100000))) < 2000
    assert "get_goal" not in " ".join(d._build_argv())


async def test_missing_gui_bridge_fails_install(tmp_path, monkeypatch):
    from ale_run.agents import _bootstrap
    from ale_run.agents.qwen_code import deployer as module

    d = adapter(tmp_path)
    monkeypatch.setattr(_bootstrap, "ensure_node_npm", AsyncMock(return_value=("node", "npm")))
    monkeypatch.setattr(_bootstrap, "ensure_cua_mcp_server", AsyncMock())
    monkeypatch.setattr(module, "install_cli", AsyncMock(return_value=("node", "cli.js")))
    with pytest.raises(RuntimeError, match="required CUA bridge"):
        await d.install()


@pytest.mark.parametrize(
    "terminal,mcp,rc,status",
    [
        ({"subtype": "success", "is_error": False}, True, 0, "completed"),
        ({"subtype": "error_max_turns", "is_error": True}, True, 0, "failed"),
        (None, True, 0, "failed"),
        ({"subtype": "success", "is_error": False}, False, 0, "failed"),
        ({"subtype": "success", "is_error": False}, True, 7, "failed"),
    ],
)
async def test_launch_requires_native_success(tmp_path, monkeypatch, terminal, mcp, rc, status):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    d = adapter(tmp_path)
    events = [
        {
            "type": "system",
            "subtype": "init",
            "mcp_servers": [{"name": "cua", "status": "connected" if mcp else "disconnected"}],
        }
    ]
    if terminal:
        events.append({"type": "result", **terminal})
    source = (
        "import sys;sys.stdin.read();print("
        + repr("\n".join(map(json.dumps, events)))
        + ");sys.exit("
        + str(rc)
        + ")"
    )
    monkeypatch.setattr(d, "_build_argv", lambda: [sys.executable, "-c", source])
    result = await d.launch("Unicode 中文 " + "long prompt\n" * 10000)
    assert result.status == status
    assert result.error is None or d.config.api_key not in result.error


@pytest.mark.parametrize("detached", [False, True])
async def test_terminal_result_reaps_lingering_child(tmp_path, monkeypatch, detached):
    from ale_run.agents.qwen_code import deployer as module

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(module, "_FINAL_EXIT_GRACE_S", 0.2)
    monkeypatch.setattr(module, "_TERM_GRACE_S", 0.05)
    d = adapter(tmp_path)
    child_pid = tmp_path / "child.pid"
    source = (
        "import sys,subprocess,time,json;from pathlib import Path;sys.stdin.read();"
        f"p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session={detached and os.name != 'nt'!r});"
        f"Path({str(child_pid)!r}).write_text(str(p.pid));"
        "print(json.dumps({'type':'system','subtype':'init','mcp_servers':[{'name':'cua','status':'connected'}]}),flush=True);"
        "print(json.dumps({'type':'result','subtype':'success','is_error':False}),flush=True);time.sleep(60)"
    )
    monkeypatch.setattr(d, "_build_argv", lambda: [sys.executable, "-c", source])
    result = await asyncio.wait_for(d.launch("test"), 15)
    assert result.status == "completed"
    pid = int(child_pid.read_text(encoding="utf-8"))
    if os.name == "nt":
        out = await asyncio.to_thread(
            subprocess.check_output, ["tasklist", "/FI", f"PID eq {pid}", "/NH"], text=True
        )
        assert str(pid) not in out
    else:
        stat = Path(f"/proc/{pid}/stat")
        assert (
            not stat.exists() or stat.read_text(encoding="utf-8").split(")", 1)[1].split()[0] == "Z"
        )


def test_incremental_telemetry_and_redaction(tmp_path):
    source, target = tmp_path / "native", tmp_path / "mirror.jsonl"
    mirror = TelemetryMirror(source, target, ("private-secret",))
    first = json.dumps(
        {"attributes": {"event.name": "qwen-code.api_response", "text": "private-secret"}}, indent=2
    )
    source.write_text(first[:20], encoding="utf-8")
    mirror.poll()
    assert target.read_text(encoding="utf-8") == ""
    with source.open("a") as handle:
        handle.write(first[20:] + '\n{"metrics": []}\n{"unfinished":')
    mirror.poll()
    mirror.poll()
    records = list(map(json.loads, target.read_text(encoding="utf-8").splitlines()))
    assert len(records) == 2
    assert records[0]["attributes"]["text"] == "[REDACTED]"


def test_children_tool_wrappers_and_incomplete_calls(tmp_path):
    d = adapter(tmp_path)
    wd = Path(d.executor.work_dir)
    wd.mkdir()
    events = [
        {
            "type": "assistant",
            "session_id": "main",
            "message": {
                "content": [{"type": "tool_use", "id": "outer", "name": "task", "input": {}}]
            },
        },
        {
            "type": "assistant",
            "session_id": "child",
            "parent_tool_use_id": "outer",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "inner",
                        "name": "tool_call",
                        "input": {"name": "mcp__cua__screenshot", "arguments": {}},
                    }
                ]
            },
        },
        {
            "type": "user",
            "session_id": "child",
            "parent_tool_use_id": "outer",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "inner", "content": "ok"}]
            },
        },
        {
            "type": "result",
            "parent_tool_use_id": "outer",
            "subtype": "success",
            "usage": {"input_tokens": 9999},
        },
        {
            "type": "result",
            "subtype": "error_max_turns",
            "usage": {"input_tokens": 42, "output_tokens": 0},
        },
    ]
    (wd / "stdout.log").write_text("\n".join(map(json.dumps, events)))
    b = TrajectoryBuilder(agent_name="qwen_code", task_path="test", variant_index=0)
    d.parse_artifacts(
        work_dir=wd, config=d.config, run_result=AgentRunResult(status="failed"), builder=b
    )
    trajectory = b.finalize(reward=None)
    assert trajectory.final_metrics.total_input_tokens == 42
    assert len(trajectory.subagent_trajectories) == 1
    assert trajectory.subagent_trajectories[0].final_metrics.status == "completed"
    assert trajectory.subagent_trajectories[0].final_metrics.total_input_tokens == 9999
    assert trajectory.subagent_trajectories[0].steps[0].tool_calls[0].name == "mcp__cua__screenshot"
    assert trajectory.extra["qwen_code"]["tool_completeness"]["missing_results"] == [(0, "outer")]


@pytest.mark.parametrize("stale", [False, True])
async def test_private_install_rejects_path_shadowing(tmp_path, monkeypatch, stale):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PATH", "/shadow")
    package = tmp_path / ".ale-qwen-code/cli/0.25.3/node_modules/@qwen-code/qwen-code"
    package.mkdir(parents=True)
    if stale:
        (package / "package.json").write_text(json.dumps({"version": "0.1.0"}))
        (package / "cli-entry.js").touch()
    commands = []

    async def command(argv, **kwargs):
        commands.append(argv)
        output = "v24.14.0\n" if argv == ["node", "--version"] else "0.25.3\n"
        if "install" in argv:
            (package / "package.json").write_text(
                json.dumps({"name": "@qwen-code/qwen-code", "version": "0.25.3"})
            )
            (package / "cli-entry.js").touch()
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(runtime, "command", command)
    node, entry = await runtime.install_cli("node", "npm", "0.25.3")
    assert entry == str(package / "cli-entry.js")
    assert any("@qwen-code/qwen-code@0.25.3" in c for c in commands)
    assert commands[-1] == [node, entry, "--version"]


@pytest.mark.skipif(
    not os.environ.get("QWEN_TEST_CLI"),
    reason="set QWEN_TEST_CLI and QWEN_TEST_NODE for the official CLI contract",
)
@pytest.mark.parametrize("image_size", [(1, 1), (1920, 1080)])
async def test_official_cli_mcp_images_and_native_telemetry(tmp_path, monkeypatch, image_size):
    import base64
    import io
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from PIL import Image

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    d = adapter(
        tmp_path, model="gpt-4.1", context_window=100000, max_output_tokens=1024, max_turns=5
    )
    bridge = Path(d.executor.sandbox.mcp_server_dir) / "src/index.js"
    bridge.parent.mkdir(parents=True)
    image_buffer = io.BytesIO()
    Image.new("RGB", image_size, "green").save(image_buffer, format="PNG")
    image_data = base64.b64encode(image_buffer.getvalue()).decode()
    bridge.write_text(
        """import json,sys
for line in sys.stdin:
 r=json.loads(line);m=r.get("method");data={}
 if m=="initialize":data={"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"audit","version":"1"}}
 elif m=="tools/list":data={"tools":[{"name":"screenshot","description":"Audit screenshot","inputSchema":{"type":"object","properties":{}}}]}
 elif m=="tools/call":data={"content":[{"type":"text","text":"IMAGE_SENTINEL"},{"type":"image","mimeType":"image/png","data":"IMAGE_DATA"}]}
 if "id" in r:print(json.dumps({"jsonrpc":"2.0","id":r["id"],"result":data}),flush=True)
""".replace("IMAGE_DATA", image_data),
        encoding="utf-8",
    )
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            number = len(requests)
            if number == 1:
                delta = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "search-1",
                            "type": "function",
                            "function": {
                                "name": "tool_search",
                                "arguments": json.dumps({"query": "select:mcp__cua__screenshot"}),
                            },
                        }
                    ],
                }
            elif number == 2:
                delta = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "screen-1",
                            "type": "function",
                            "function": {
                                "name": "tool_call",
                                "arguments": json.dumps(
                                    {"name": "mcp__cua__screenshot", "arguments": {}}
                                ),
                            },
                        }
                    ],
                }
            else:
                delta = {"role": "assistant", "content": "IMAGE_SENTINEL confirmed"}
            chunks = [
                {
                    "id": f"chat-{number}",
                    "object": "chat.completion.chunk",
                    "model": "gpt-4.1",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                },
                {
                    "id": f"chat-{number}",
                    "object": "chat.completion.chunk",
                    "model": "gpt-4.1",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "tool_calls" if number < 3 else "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                },
            ]
            payload = (
                "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload.encode())))
            self.end_headers()
            self.wfile.write(payload.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    d.config.base_url = f"http://127.0.0.1:{server.server_port}/v1"
    d._node_path = os.environ["QWEN_TEST_NODE"]
    d._qwen_path = os.environ["QWEN_TEST_CLI"]
    try:
        result = await asyncio.wait_for(
            d.launch("Capture the audit screenshot, then report its sentinel."), 90
        )
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)
    assert result.status == "completed", result.error
    assert len(requests) >= 3
    assert requests[0]["max_tokens"] == 1024
    assert "image_url" in json.dumps(requests[2]["messages"])
    assert "screen_width / image_width" in json.dumps(requests[0]["messages"])
    sent_image = next(
        part["image_url"]["url"]
        for message in requests[2]["messages"]
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if part.get("type") == "image_url"
    )
    sent_bytes = base64.b64decode(sent_image.split(",", 1)[1])
    expected_size = (1456, 819) if image_size == (1920, 1080) else image_size
    assert Image.open(io.BytesIO(sent_bytes)).size == expected_size
    wd = Path(d.executor.work_dir)
    b = TrajectoryBuilder(agent_name="qwen_code", task_path="audit", variant_index=0)
    d.parse_artifacts(work_dir=wd, config=d.config, run_result=result, builder=b)
    trajectory = b.finalize(reward=None)
    images = [
        p
        for s in trajectory.steps
        if s.observation
        for r in s.observation.results
        for p in r.content
        if p.type == "image"
    ]
    assert images
    assert base64.b64decode(images[0].image.data) == sent_bytes
    stats = trajectory.extra["qwen_code"]
    assert stats["tool_completeness"]["matched_results"] == 2
    assert stats["telemetry"]["events"]["qwen-code.api_response"] >= 3
    assert stats["telemetry"]["events"]["qwen-code.tool_call"] >= 2
    assert d.config.api_key not in (wd / "stdout.log").read_text(encoding="utf-8")
    assert d.config.api_key not in (wd / "telemetry.jsonl").read_text(encoding="utf-8")


def test_telemetry_only_child_does_not_claim_full_result_or_completion(tmp_path):
    d = adapter(tmp_path)
    wd = Path(d.executor.work_dir)
    wd.mkdir()
    (wd / "stdout.log").write_text('{"type":"system"}\n', encoding="utf-8")
    event = {
        "attributes": {
            "event.name": "qwen-code.tool_call",
            "prompt_id": "root#memory-child#1",
            "call_id": "internal-1",
            "function_name": "read_file",
            "function_args": '{"path":"memory"}',
            "success": True,
            "execution_status": "success",
        }
    }
    (wd / "telemetry.jsonl").write_text(json.dumps(event), encoding="utf-8")
    b = TrajectoryBuilder(agent_name="qwen_code", task_path="test", variant_index=0)
    d.parse_artifacts(
        work_dir=wd, config=d.config, run_result=AgentRunResult(status="timeout"), builder=b
    )
    child = b.trajectory.subagent_trajectories[0]
    assert child.final_metrics is None
    assert child.extra["completion_status"] == "unreported"
    stats = b.trajectory.extra["qwen_code"]["tool_completeness"]
    assert stats["matched_results"] == 1
    assert stats["status_only_results"] == 1
    assert stats["results_with_content"] == 0


def test_truncated_stream_preserves_reasoning_text_and_tool_arguments(tmp_path):
    d = adapter(tmp_path)
    wd = Path(d.executor.work_dir)
    wd.mkdir()
    payloads = [
        {"type": "message_start", "message": {"id": "m1"}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "unfinished reason"},
        },
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "call", "name": "write_file", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"file_path":"partial'},
        },
    ]
    (wd / "stdout.log").write_text(
        "\n".join(
            json.dumps({"type": "stream_event", "session_id": "root", "event": p}) for p in payloads
        )
    )
    b = TrajectoryBuilder(agent_name="qwen_code", task_path="test", variant_index=0)
    d.parse_artifacts(
        work_dir=wd, config=d.config, run_result=AgentRunResult(status="timeout"), builder=b
    )
    agent = next(s for s in b.trajectory.steps if s.source == "agent")
    assert agent.reasoning == "unfinished reason"
    assert agent.extra["partial"] is True
    assert agent.tool_calls[0].arguments["partial_json"] == '{"file_path":"partial'


async def test_cancellation_during_cleanup_is_preserved(tmp_path, monkeypatch):
    from ale_run.agents.qwen_code import deployer as module

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    d = adapter(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = module.stop_process

    async def delayed_stop(*args):
        entered.set()
        await release.wait()
        await original(*args)

    monkeypatch.setattr(module, "stop_process", delayed_stop)
    monkeypatch.setattr(d, "_build_argv", lambda: [sys.executable, "-c", "pass"])
    task = asyncio.create_task(d.launch("test"))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


def test_native_chat_mirror_preserves_partial_records(tmp_path):
    from ale_run.agents.qwen_code.telemetry import NativeChatMirror

    source = tmp_path / "projects/a/chats/session.jsonl"
    source.parent.mkdir(parents=True)
    target = tmp_path / "native_chats.jsonl"
    mirror = NativeChatMirror(tmp_path, target, ("credential",))
    source.write_bytes(b'{"text":"credential ')
    mirror.poll()
    assert target.read_text(encoding="utf-8") == ""
    with source.open("ab") as output:
        output.write('中文"}\n'.encode())
    mirror.poll()
    mirror.poll()
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"]["text"] == "[REDACTED] 中文"
