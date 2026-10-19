"""Run official Qwen Code with required GUI tools on Linux and Windows."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import ClassVar

from ale_run.agents._cli_runtime import redact, secret_values
from ale_run.base_interface import (
    AgentRunResult,
    BaseAgentDeployer,
    ContentPart,
    ImageSource,
    Observation,
    StepMetrics,
    ToolCall,
    ToolResult,
    TrajectoryBuilder,
)

from .config import QwenCodeConfig
from .runtime import assign_job, install_cli, stop_process
from .telemetry import NativeChatMirror, TelemetryMirror, summarize_telemetry
from .trajectory import enrich_native

_TERM_GRACE_S = 1.0
_FINAL_EXIT_GRACE_S = 10.0
_GUI_INSTRUCTIONS = (
    "Desktop and browser UI access is already configured through the cua MCP server. "
    "Discover its mcp__cua__ tools with tool_search, then invoke them with tool_call. "
    "Use screenshots to observe the desktop and the provided click, typing, and key tools "
    "to interact with it. No additional computer-use SDK or MCP server installation is needed. "
    "Qwen may resize screenshots. Follow the coordinate space in the pointer tool schema. "
    "For pixel coordinates, call get_screen_size and scale positions from the displayed image "
    "to the full desktop dimensions: x * screen_width / image_width, "
    "y * screen_height / image_height. For normalized coordinates, map each image axis to 0-1000. "
    "After launching an app, inspect the desktop; a launcher process exiting does not mean "
    "the app window closed. Complete the task autonomously using the available tools."
)


class QwenCodeDeployer(BaseAgentDeployer):
    default_executor: ClassVar[str] = "sandbox"
    supported_executors: ClassVar[frozenset[str]] = frozenset({"sandbox"})
    hot_artifacts: ClassVar[tuple[str, ...]] = (
        "stdout.log",
        "stderr.log",
        "telemetry.jsonl",
        "event_times.jsonl",
        "native_chats.jsonl",
    )

    @property
    def version(self) -> str:
        return self.config.cli_version

    async def install(self) -> None:
        from ale_run.agents._bootstrap import ensure_cua_mcp_server, ensure_node_npm

        node, npm = await ensure_node_npm()
        self._node_path, self._qwen_path = await install_cli(node, npm, self.config.cli_version)
        self.actual_cli_version = self.config.cli_version
        self.cli_source = "official"
        await ensure_cua_mcp_server(self.executor.sandbox)
        bridge = Path(self.executor.sandbox.mcp_server_dir) / "src" / "index.js"
        if not bridge.is_file():
            raise RuntimeError(f"qwen_code: required CUA bridge is missing: {bridge}")

    def _prepare_run(self) -> None:
        from ale_run.agents._bootstrap import cua_bridge_env

        cfg = self.config
        wd = Path(self.executor.work_dir)
        wd.mkdir(parents=True, exist_ok=True)
        self._session_id = str(uuid.uuid4())
        # Qwen percent-encodes cwd in native chat paths. Keep Windows paths short.
        self._state_dir = Path.home() / ".ale-qwen-code" / "runs" / self._session_id[:12]
        self._state_dir.mkdir(parents=True)
        self._cwd = wd if self.executor.sandbox.is_linux else self._state_dir / "cwd"
        self._cwd.mkdir(parents=True, exist_ok=True)
        (self._state_dir / "empty.json").write_text("{}", encoding="utf-8")
        settings = {
            "security": {"auth": {"selectedType": cfg.auth_type}},
            "general": {"chatRecording": True},
            "tools": {"sandbox": False},
            "skills": {"disabled": ["computer-use", "browser-use"]},
            "privacy": {"usageStatisticsEnabled": False},
            "model": {"generationConfig": {}},
            "telemetry": {
                "enabled": cfg.telemetry_enabled,
                "target": "local",
                "outfile": str(self._state_dir / "telemetry.raw"),
                "logPrompts": False,
                "includeSensitiveSpanAttributes": False,
            },
        }
        generation = settings["model"]["generationConfig"]
        if cfg.context_window is not None:
            generation["contextWindowSize"] = cfg.context_window
        if cfg.max_output_tokens is not None:
            generation["samplingParams"] = {"max_tokens": cfg.max_output_tokens}
        if cfg.reasoning_effort is not None:
            settings["model"]["reasoningEffort"] = cfg.reasoning_effort
        if cfg.auto_compact_token_limit is not None:
            settings["context"] = {
                "autoCompactThreshold": cfg.auto_compact_token_limit / cfg.context_window,
            }
        path_type = PurePosixPath if self.executor.sandbox.is_linux else PureWindowsPath
        bridge = str(path_type(self.executor.sandbox.mcp_server_dir) / "src" / "index.js")
        settings["mcpServers"] = {
            "cua": {
                "command": self.executor.sandbox.node,
                "args": [bridge],
                "env": {
                    **cua_bridge_env(self.executor),
                    "CUA_COORDINATE_SPACE": cfg.coordinate_space,
                },
                "trust": True,
            }
        }
        content = json.dumps(settings, indent=2)
        (self._state_dir / "settings.json").write_text(content, encoding="utf-8")
        (wd / "qwen-settings.json").write_text(content, encoding="utf-8")

    def _build_env(self, cfg: QwenCodeConfig) -> dict[str, str]:
        env = {**os.environ, **(self.executor.env or {}), **cfg.extra_envs}
        key = cfg.api_key or env.get(cfg.api_key_env)
        if not key:
            raise RuntimeError(f"qwen_code: set config.api_key or {cfg.api_key_env}")
        for name in list(env):
            if name.startswith(("QWEN_TELEMETRY_", "OTEL_")):
                env.pop(name)
        env.update(
            {
                "OPENAI_API_KEY": key,
                "OPENAI_BASE_URL": cfg.base_url,
                "OPENAI_MODEL": cfg.model,
                "QWEN_CODE_SUPPRESS_YOLO_WARNING": "1",
                "QWEN_CODE_SKIP_UPDATE_CHECK_ONCE": "true",
                "NO_COLOR": "1",
            }
        )
        if hasattr(self, "_state_dir"):
            env.update(
                {
                    "QWEN_HOME": str(self._state_dir),
                    "QWEN_RUNTIME_DIR": str(self._state_dir),
                    "QWEN_CODE_SYSTEM_SETTINGS_PATH": str(self._state_dir / "empty.json"),
                    "QWEN_CODE_SYSTEM_DEFAULTS_PATH": str(self._state_dir / "empty.json"),
                }
            )
        if hasattr(self, "_node_path"):
            env["PATH"] = str(Path(self._node_path).parent) + os.pathsep + env.get("PATH", "")
        return env

    def _build_argv(self, prompt: str = "") -> list[str]:
        cfg = self.config
        argv = [
            self._node_path,
            self._qwen_path,
            "--approval-mode",
            "yolo",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--auth-type",
            cfg.auth_type,
            "--model",
            cfg.model,
            "--session-id",
            self._session_id,
            "--allowed-mcp-server-names",
            "cua",
            "--append-system-prompt",
            _GUI_INSTRUCTIONS,
        ]
        disabled = tuple(
            dict.fromkeys(
                (
                    "ask_user_question",
                    "enter_plan_mode",
                    "exit_plan_mode",
                    *cfg.disabled_tools,
                )
            )
        )
        argv += ["--exclude-tools", ",".join(disabled)]
        for field, flag in (
            ("max_turns", "--max-session-turns"),
            ("max_tool_calls", "--max-tool-calls"),
            ("max_subagent_depth", "--max-subagent-depth"),
        ):
            if getattr(cfg, field) is not None:
                argv += [flag, str(getattr(cfg, field))]
        return argv

    async def launch(self, prompt: str) -> AgentRunResult:
        self._prepare_run()
        wd = Path(self.executor.work_dir)
        env = self._build_env(self.config)
        secrets = secret_values(self.config, env)
        prompt_path = self._state_dir / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        for name in (*self.hot_artifacts, "qwen_code.pid"):
            (wd / name).unlink(missing_ok=True)
        mirror = TelemetryMirror(self._state_dir / "telemetry.raw", wd / "telemetry.jsonl", secrets)
        native = NativeChatMirror(self._state_dir, wd / "native_chats.jsonl", secrets)
        started = time.monotonic()
        job = None
        if os.name != "nt":
            from ale_run.executors._sandbox_entry import _LinuxProcessTree

            job = _LinuxProcessTree()
        with prompt_path.open("rb") as source:
            proc = await asyncio.create_subprocess_exec(
                *self._build_argv(),
                stdin=source,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._cwd),
                env=env,
                start_new_session=os.name != "nt",
                limit=64 * 1024 * 1024,
            )
        (wd / "qwen_code.pid").write_text(str(proc.pid), encoding="ascii")
        state = {"terminal": None, "terminal_time": None, "mcp": False, "error": None}
        readers = []
        natural_exit = None

        async def capture(stream, path: Path, events: bool = False):
            with (
                path.open("w", encoding="utf-8") as out,
                (wd / "event_times.jsonl").open("a", encoding="utf-8") as times,
            ):
                index = 0
                while line := await stream.readline():
                    text = redact(line.decode("utf-8", "replace"), secrets)
                    out.write(text)
                    out.flush()
                    if not events:
                        continue
                    try:
                        event = json.loads(text)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    times.write(
                        json.dumps({"index": index, "timestamp": datetime.now(UTC).isoformat()})
                        + "\n"
                    )
                    times.flush()
                    index += 1
                    if event.get("parent_tool_use_id"):
                        continue
                    if event.get("type") == "system" and event.get("subtype") == "init":
                        state["mcp"] = any(
                            server.get("name") == "cua" and server.get("status") == "connected"
                            for server in event.get("mcp_servers", [])
                        )
                        if not state["mcp"]:
                            state["error"] = "qwen_code: required CUA MCP server did not connect"
                    if event.get("type") == "result":
                        state["terminal"] = event
                        state["terminal_time"] = time.monotonic()

        try:
            if os.name == "nt":
                job = assign_job(proc.pid)
            readers = [
                asyncio.create_task(capture(proc.stdout, wd / "stdout.log", True)),
                asyncio.create_task(capture(proc.stderr, wd / "stderr.log")),
            ]
            while proc.returncode is None:
                for reader in readers:
                    if reader.done() and not reader.cancelled() and reader.exception():
                        raise reader.exception()
                if state["error"] or (
                    state["terminal_time"]
                    and time.monotonic() - state["terminal_time"] > _FINAL_EXIT_GRACE_S
                ):
                    break
                await asyncio.to_thread(mirror.poll)
                await asyncio.to_thread(native.poll)
                await asyncio.sleep(0.1)
        finally:
            natural_exit = proc.returncode
            cleanup = asyncio.create_task(stop_process(proc, job, _TERM_GRACE_S))
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            try:
                await asyncio.wait_for(asyncio.gather(*readers), timeout=5)
            finally:
                for reader in readers:
                    if not reader.done():
                        reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
                await asyncio.to_thread(mirror.poll)
                await asyncio.to_thread(native.poll)
            if cancelled:
                raise asyncio.CancelledError
        terminal = state["terminal"] or {}
        error = state["error"]
        if not error and not state["mcp"]:
            error = "qwen_code: no connected CUA MCP initialization event"
        if not error and (terminal.get("subtype") != "success" or terminal.get("is_error")):
            error = "qwen_code: missing successful terminal result: " + json.dumps(terminal)
        if not error and natural_exit not in (None, 0):
            error = f"qwen_code: process exited {proc.returncode}"
        if error:
            error += " | stderr: " + (wd / "stderr.log").read_text(encoding="utf-8")[-1500:]
        return AgentRunResult(
            status="failed" if error else "completed",
            pid=proc.pid,
            exit_code=proc.returncode,
            transcript_path=str(wd / "stdout.log"),
            stderr_path=str(wd / "stderr.log"),
            duration_s=time.monotonic() - started,
            error=redact(error, secrets),
        )

    @classmethod
    def parse_artifacts(
        cls,
        *,
        work_dir: Path,
        config: QwenCodeConfig,
        run_result: AgentRunResult,
        builder: TrajectoryBuilder,
    ) -> None:
        """Parse NDJSON messages and authoritative cumulative usage."""
        stdout_log = work_dir / "stdout.log"
        if not stdout_log.exists():
            builder.add_step(
                source="system",
                message=f"qwen_code: no stdout at {stdout_log}",
                extra={"reason": "no_transcript"},
            )
            return

        raw = redact(
            stdout_log.read_text(encoding="utf-8", errors="replace"), secret_values(config)
        )
        events, result_summary = cls._parse_stream_json(raw)

        transcript_jsonl = work_dir / "transcript.jsonl"
        transcript_jsonl.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + ("\n" if events else ""),
            encoding="utf-8",
        )

        if not events and not result_summary:
            builder.add_step(
                source="system",
                message="qwen_code: no JSON events parsed from stdout",
                extra={"reason": "no_events"},
            )
            builder.trajectory.extra.setdefault("qwen_code", {}).update(
                {
                    "exit_code": run_result.exit_code,
                    "transcript_path": str(stdout_log),
                }
            )
            return

        times = {}
        timing_file = work_dir / "event_times.jsonl"
        if timing_file.exists():
            for line in timing_file.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                    times[entry["index"]] = entry["timestamp"]
                except (ValueError, KeyError):
                    continue
        children = {}
        child_results = {}
        for index, ev in enumerate(events):
            parent = ev.get("parent_tool_use_id")
            target = builder
            if parent:
                key = parent
                if key not in children:
                    children[key] = TrajectoryBuilder(
                        agent_name="qwen_code",
                        model=config.model,
                        task_path=builder.trajectory.task_path,
                        variant_index=builder.trajectory.variant_index,
                    )
                    children[key].trajectory.extra.update(
                        {
                            "parent_tool_use_id": parent,
                            "session_id": ev.get("session_id"),
                        }
                    )
                target = children[key]
                if ev.get("type") == "result":
                    child_results[key] = ev
            before = len(target.trajectory.steps)
            cls._consume_event(ev, target)
            for step in target.trajectory.steps[before:]:
                step.extra.update(
                    {
                        k: ev[k]
                        for k in ("uuid", "session_id", "parent_tool_use_id", "partial")
                        if k in ev
                    }
                )
                if index in times:
                    step.timestamp = times[index]
                    step.extra["timestamp_source"] = "stdout_received"
                else:
                    step.extra["timestamp_source"] = "parse_time"
        for key, child in children.items():
            terminal = child_results.get(key)
            if terminal:
                child.finalize(
                    reward=None,
                    status="completed"
                    if terminal.get("subtype") == "success" and not terminal.get("is_error")
                    else "failed",
                )
            else:
                child.trajectory.extra["completion_status"] = "unreported"
            builder.trajectory.subagent_trajectories.append(child.trajectory)

        if result_summary:
            usage = (
                result_summary.get("usage")
                if isinstance(result_summary.get("usage"), dict)
                else None
            )
            metrics = None
            if usage:
                metrics = StepMetrics(
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    cache_read_tokens=usage.get("cache_read_input_tokens"),
                    cache_creation_tokens=usage.get("cache_creation_input_tokens"),
                    cost_usd=result_summary.get("total_cost_usd"),
                )
            if metrics:
                builder.override_final_metrics(
                    total_input_tokens=metrics.input_tokens,
                    total_output_tokens=metrics.output_tokens,
                    total_cache_read_tokens=metrics.cache_read_tokens,
                    total_cache_creation_tokens=metrics.cache_creation_tokens,
                    total_cost_usd=metrics.cost_usd,
                )
            builder.add_step(
                source="system",
                message=None,
                extra={
                    "result_subtype": result_summary.get("subtype"),
                    "num_turns": result_summary.get("num_turns"),
                    "duration_ms": result_summary.get("duration_ms"),
                    "is_error": result_summary.get("is_error"),
                },
            )

            response = result_summary.get("result")
            if (
                isinstance(response, str)
                and response
                and not any(
                    step.source == "agent" and step.message == response
                    for step in builder.trajectory.steps
                )
            ):
                builder.add_step(source="agent", message=response)

        native_stats = enrich_native(work_dir, builder)
        trajectories = [builder.trajectory, *builder.trajectory.subagent_trajectories]
        calls = {
            (i, call.id)
            for i, traj in enumerate(trajectories)
            for step in traj.steps
            for call in step.tool_calls
        }
        results = {
            (i, result.tool_call_id)
            for i, traj in enumerate(trajectories)
            for step in traj.steps
            if step.observation
            for result in step.observation.results
        }
        status_only = {
            (i, result.tool_call_id)
            for i, traj in enumerate(trajectories)
            for step in traj.steps
            if step.observation and step.extra.get("result_content_available") is False
            for result in step.observation.results
        }
        builder.trajectory.extra.setdefault("qwen_code", {}).update(
            {
                "telemetry": summarize_telemetry(work_dir) if config.telemetry_enabled else None,
                "tool_completeness": {
                    **native_stats,
                    "calls": len(calls),
                    "matched_results": len(calls & results),
                    "status_only_results": len(calls & status_only),
                    "results_with_content": len(calls & (results - status_only)),
                    "missing_results": sorted(calls - results),
                    "orphan_results": sorted(results - calls),
                },
                "exit_code": run_result.exit_code,
                "transcript_path": str(stdout_log),
                "cost_available": bool(
                    result_summary and result_summary.get("total_cost_usd") is not None
                ),
                "event_count": len(events),
                "result_summary": result_summary,
            }
        )

    @staticmethod
    def _parse_stream_json(stdout_text: str) -> tuple[list[dict], dict | None]:
        """Separate stream events from the terminal result summary."""
        events: list[dict] = []
        result_summary: dict | None = None
        pending: dict[tuple, dict] = {}
        for line in stdout_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            key = (parsed.get("session_id"), parsed.get("parent_tool_use_id"))
            if parsed.get("type") == "stream_event":
                delta = parsed.get("event", {})
                kind = delta.get("type")
                if kind == "message_start":
                    pending[key] = {
                        **parsed,
                        "type": "assistant",
                        "partial": True,
                        "message": {"content": []},
                    }
                current = pending.get(key)
                if current is not None:
                    blocks = current["message"]["content"]
                    if kind == "content_block_start":
                        blocks.append(dict(delta.get("content_block", {})))
                    elif kind == "content_block_delta" and blocks:
                        value = delta.get("delta", {})
                        block = blocks[min(delta.get("index", len(blocks) - 1), len(blocks) - 1)]
                        field = {
                            "text_delta": "text",
                            "thinking_delta": "thinking",
                            "input_json_delta": "partial_json",
                        }.get(value.get("type"))
                        if field:
                            block[field] = block.get(field, "") + value.get(field, "")
            elif parsed.get("type") == "assistant":
                pending.pop(key, None)
            if parsed.get("type") == "result" and not parsed.get("parent_tool_use_id"):
                result_summary = parsed
                continue
            events.append(parsed)
        for partial in pending.values():
            for block in partial["message"]["content"]:
                if "partial_json" in block:
                    try:
                        block["input"] = json.loads(block["partial_json"])
                    except ValueError:
                        block["input"] = {"partial_json": block["partial_json"]}
            events.append(partial)
        return events, result_summary

    @classmethod
    def _consume_event(cls, event: dict, builder: TrajectoryBuilder) -> None:
        top_type = str(event.get("type") or "").strip()

        if top_type == "stream_event":
            return
        if top_type == "result" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
            builder.override_final_metrics(
                total_input_tokens=usage.get("input_tokens"),
                total_output_tokens=usage.get("output_tokens"),
                total_cache_read_tokens=usage.get("cache_read_input_tokens"),
                total_cache_creation_tokens=usage.get("cache_creation_input_tokens"),
                total_cost_usd=event.get("total_cost_usd"),
            )

        message = event.get("message") if isinstance(event.get("message"), dict) else None
        if top_type == "assistant" and message:
            cls._consume_assistant_message(message, builder)
            return

        if top_type == "user" and message:
            cls._consume_user_message(message, builder)
            return

        builder.add_step(
            source="system",
            message="",
            extra={"kind": top_type or "unknown", "raw": event},
        )

    @classmethod
    def _consume_assistant_message(cls, message: dict, builder: TrajectoryBuilder) -> None:
        content = message.get("content")
        if not isinstance(content, list):
            return
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
        metrics = None
        if usage and (usage.get("input_tokens") or usage.get("output_tokens")):
            metrics = StepMetrics(
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                cache_read_tokens=usage.get("cache_read_input_tokens") or None,
            )

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif btype == "thinking" and isinstance(block.get("thinking"), str):
                reasoning_parts.append(block["thinking"])
            elif btype == "tool_use":
                name = str(block.get("name") or "unknown")
                arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
                if name == "tool_call" and isinstance(arguments.get("name"), str):
                    name, arguments = arguments["name"], arguments.get("arguments", {})
                tool_calls.append(
                    ToolCall(
                        id=str(block.get("id") or ""),
                        name=name,
                        arguments=arguments if isinstance(arguments, dict) else {},
                    )
                )

        text = "\n".join(text_parts).strip()
        reasoning = "\n".join(reasoning_parts).strip()
        if not (text or reasoning or tool_calls):
            return
        builder.add_step(
            source="agent",
            message=text or None,
            reasoning=reasoning or None,
            tool_calls=tool_calls or None,
            metrics=metrics,
        )

    @classmethod
    def _consume_user_message(cls, message: dict, builder: TrajectoryBuilder) -> None:
        content = message.get("content")
        if not isinstance(content, list):
            return
        results: list[ToolResult] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            raw_content = block.get("content")
            text = (
                raw_content
                if isinstance(raw_content, str)
                else (
                    json.dumps(raw_content, ensure_ascii=False) if raw_content is not None else ""
                )
            )
            parts = []
            if isinstance(raw_content, list):
                for item in raw_content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text" and isinstance(item.get("text"), str):
                        parts.append(ContentPart(type="text", text=item["text"]))
                    elif item.get("type") == "image" and isinstance(item.get("source"), dict):
                        source = item["source"]
                        if source.get("type") == "base64" and isinstance(source.get("data"), str):
                            parts.append(
                                ContentPart(
                                    type="image",
                                    image=ImageSource(
                                        type="base64",
                                        data=source["data"],
                                        media_type=source.get("media_type") or "image/png",
                                    ),
                                )
                            )
            elif text:
                parts = [ContentPart(type="text", text=text)]
            results.append(
                ToolResult(
                    tool_call_id=str(block.get("tool_use_id") or ""),
                    content=parts,
                    is_error=bool(block.get("is_error")),
                )
            )
        if not results:
            return
        builder.add_step(
            source="environment",
            observation=Observation(results=results),
        )
