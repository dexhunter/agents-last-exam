"""Opt-in contract tests against an unmodified, pinned Muse executable.

Set ALE_MUSE_TEST_BINARY to the native executable. Provider responses and MCP
are local deterministic fixtures; these tests need no model credentials.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from ale_run.agents.muse_code import MuseCodeConfig, MuseCodeDeployer
from ale_run.base_interface import TrajectoryBuilder, persist_screenshots

BINARY = os.environ.get("ALE_MUSE_TEST_BINARY")
pytestmark = pytest.mark.skipif(not BINARY, reason="set ALE_MUSE_TEST_BINARY for native CLI tests")
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1kAAAAASUVORK5CYII="


@pytest.fixture
def native_backend():
    class Requests(list):
        block = False
        release = threading.Event()

    requests = Requests()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"data": [{"id": "muse-spark-1.3", "object": "model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if requests.block:
                requests.release.wait(timeout=60)
                return
            n = len(requests)
            tool_calls = [
                ("mcp__cua__screenshot", {}),
                ("mcp__cua__click", {"x": 1, "y": 1}),
                (
                    "write_file",
                    {"path": "native proof 中文.txt", "content": "Muse native tools OK\n"},
                ),
            ]
            if n <= len(tool_calls):
                name, arguments = tool_calls[n - 1]
                item = {
                    "type": "function_call",
                    "id": f"fc_{n}",
                    "call_id": f"call_{n}",
                    "name": name,
                    "namespace": "muse",
                    "arguments": json.dumps(arguments),
                    "status": "completed",
                }
            else:
                item = {
                    "type": "message",
                    "id": "msg_final",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "Native test done", "annotations": []}
                    ],
                    "status": "completed",
                }
            response = {
                "id": f"resp_{n}",
                "object": "response",
                "created_at": 1792380000,
                "model": "muse-spark-1.3",
                "status": "completed",
                "output": [item],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                    "input_tokens_details": {"cached_tokens": 10},
                    "output_tokens_details": {"reasoning_tokens": 5},
                },
            }
            events = [
                {
                    "type": "response.created",
                    "response": {**response, "status": "in_progress", "output": []},
                },
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**item, "status": "in_progress"},
                },
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            if n == 1:
                reasoning = {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"type": "summary_text", "text": "Inspect the screen first."}],
                    "status": "completed",
                }
                response["output"].insert(0, reasoning)
                events.insert(
                    1, {"type": "response.output_item.added", "output_index": 1, "item": reasoning}
                )
                events.insert(
                    2, {"type": "response.output_item.done", "output_index": 1, "item": reasoning}
                )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i, event in enumerate(events):
                event["sequence_number"] = i
                self.wfile.write(
                    ("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode()
                )
                self.wfile.flush()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        requests.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def native_deployer(tmp_path, cfg):
    bridge = tmp_path / "bridge" / "src" / "index.js"
    bridge.parent.mkdir(parents=True)
    bridge.write_text(
        "import sys,json,os\nfrom pathlib import Path\n"
        "Path(__file__).with_name('mcp.pid').write_text(str(os.getpid()))\n"
        f"png={PNG!r}\n"
        "for line in sys.stdin:\n"
        " q=json.loads(line); m=q.get('method')\n"
        " if 'id' not in q: continue\n"
        " if m=='initialize': r={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}\n"
        " elif m=='tools/list': r={'tools':[{'name':n,'description':n,'inputSchema':{'type':'object','properties':{}}} for n in ['screenshot','click']]}\n"
        " elif m=='tools/call':\n"
        "  bad=q['params']['name']=='click'\n"
        "  r={'content':[{'type':'text','text':'fixture error' if bad else 'fixture screenshot'}],'isError':bad}\n"
        "  if not bad: r['content'].append({'type':'image','data':png,'mimeType':'image/png'})\n"
        " else: r={}\n"
        " print(json.dumps({'jsonrpc':'2.0','id':q['id'],'result':r}),flush=True)\n",
        encoding="utf-8",
    )
    deployer = MuseCodeDeployer(
        SimpleNamespace(
            config=cfg,
            env={
                "XDG_CONFIG_HOME": str(tmp_path / "config-root"),
                "XDG_DATA_HOME": str(tmp_path / "data-root"),
                "XDG_CACHE_HOME": str(tmp_path / "cache-root"),
            },
            work_dir=str(tmp_path),
            sandbox=SimpleNamespace(
                is_linux=os.name != "nt",
                node=sys.executable,
                mcp_server_dir=str(bridge.parent.parent),
            ),
            cua_bridge_url=lambda: "http://127.0.0.1:8000",
        )
    )
    deployer._muse_path = BINARY
    deployer._write_settings()
    return deployer


@pytest.mark.asyncio
@pytest.mark.parametrize("toolset", [None, ("write_file",)])
async def test_native_cli_mcp_images_tools_and_usage(tmp_path, native_backend, toolset):
    endpoint, requests = native_backend
    cfg = MuseCodeConfig(
        model="muse-spark-1.3",
        api_key="fixture-secret",
        base_url=endpoint,
        disable_reminders=True,
        max_turns=6,
        toolset=toolset,
        settings_overrides={"provider_retry.max_retries": 0},
        context_window=100000,
        auto_compact_token_limit=70000,
        context_compaction_hard_threshold=0.9,
        context_compaction_strategy="summary-preserved-suffix/v1",
        execution_capacity=4,
        parallel_tool_calls=False,
        max_tool_output_bytes=100000,
    )
    deployer = native_deployer(tmp_path, cfg)
    result = await deployer.launch("Exercise native tools, including 中文 and a screenshot.")
    assert result.status == "completed", result.error

    assert (tmp_path / "native proof 中文.txt").read_text() == "Muse native tools OK\n"
    assert len(requests) == 4
    tools = [tool["name"] for group in requests[0]["tools"] for tool in group.get("tools", [group])]
    assert "request_user_input" not in tools
    assert "screenshot" in tools
    assert requests[0]["reasoning"]["effort"] == "high"
    builder = TrajectoryBuilder(agent_name="muse-code", task_path="demo/native", variant_index=0)
    deployer.parse_artifacts(work_dir=tmp_path, config=cfg, run_result=result, builder=builder)
    trajectory = builder.finalize(reward=None)
    assert trajectory.final_metrics.total_input_tokens == 400
    assert trajectory.final_metrics.total_output_tokens == 80
    assert trajectory.final_metrics.total_cache_read_tokens == 40
    assert persist_screenshots(trajectory, tmp_path) == 1
    assert any(step.message == "Native test done" for step in trajectory.steps)
    assert any(step.reasoning == "Inspect the screen first." for step in trajectory.steps)
    results = [
        result
        for step in trajectory.steps
        if step.observation
        for result in step.observation.results
    ]
    assert next(result for result in results if result.tool_call_id == "call_2").is_error
    summary = json.loads((tmp_path / "telemetry_summary.json").read_text())
    assert len(summary["tool_executions"]) == 3
    assert summary["tool_executions"][1]["status"] == "failed"
    assert len(summary["provider_events"]) == 8
    assert "fixture-secret" not in (tmp_path / "native_events.jsonl").read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_mcp", "step_limit"])
async def test_native_cli_failure_and_step_limit(tmp_path, native_backend, failure):
    endpoint, requests = native_backend
    cfg = MuseCodeConfig(
        model="muse-spark-1.3",
        api_key="fixture-secret",
        base_url=endpoint,
        max_turns=1,
        disable_reminders=True,
        settings_overrides={"provider_retry.max_retries": 0},
    )
    deployer = native_deployer(tmp_path, cfg)
    if failure == "missing_mcp":
        (tmp_path / "bridge/src/index.js").unlink()
    result = await deployer.launch("Exercise the failure path.")
    assert result.status == "failed", result
    if failure == "missing_mcp":
        assert not requests
        assert "cua" in result.error
    else:
        assert len(requests) == 1
        summary = json.loads((tmp_path / "telemetry_summary.json").read_text())
        assert summary["usage"]["input_tokens"] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"])
async def test_native_cli_explicit_permissions_and_reasoning(tmp_path, native_backend, effort):
    endpoint, requests = native_backend
    requests.extend([{}, {}, {}])
    cfg = MuseCodeConfig(
        model="muse-spark-1.3",
        api_key="fixture-secret",
        base_url=endpoint,
        yolo=False,
        sandbox_network="enabled",
        reasoning_effort=effort,
        disable_reminders=True,
        disable_web_tools=True,
    )
    deployer = native_deployer(tmp_path, cfg)
    result = await deployer.launch("Validate native settings without tool execution.")
    assert result.status == "completed", result.error


@pytest.mark.asyncio
async def test_native_cancellation_reaps_cli_and_mcp(tmp_path, native_backend):
    endpoint, requests = native_backend
    requests.block = True
    deployer = native_deployer(
        tmp_path,
        MuseCodeConfig(
            model="muse-spark-1.3",
            api_key="fixture-secret",
            base_url=endpoint,
            disable_reminders=True,
        ),
    )
    task = asyncio.create_task(deployer.launch("Wait for cancellation."))
    try:
        child_path = tmp_path / "bridge/src/mcp.pid"
        for _ in range(200):
            if (child_path.exists() and requests) or task.done():
                break
            await asyncio.sleep(0.1)
        assert child_path.exists(), task.result() if task.done() else "MCP did not start"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=40)
        for path in (child_path, tmp_path / "muse.pid"):
            pid = int(path.read_text())
            if os.name == "nt":
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert not any(
                    len(row) > 1 and row[1] == str(pid)
                    for row in csv.reader(result.stdout.splitlines())
                )
            else:
                stat = Path(f"/proc/{pid}/stat")
                assert not stat.exists() or stat.read_text().split()[2] == "Z"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
