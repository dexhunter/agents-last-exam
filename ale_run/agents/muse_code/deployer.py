"""Run the official Muse Code binary inside Linux and Windows sandboxes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import ClassVar

from ale_run.agents._cli_runtime import redact, secret_values
from ale_run.base_interface import AgentRunResult, BaseAgentDeployer, TrajectoryBuilder

from .config import MuseCodeConfig
from .install import install_release, installed_version
from .telemetry import NativeEventMirror, read_records, recover_telemetry_artifacts

logger = logging.getLogger(__name__)


class MuseCodeDeployer(BaseAgentDeployer):
    default_executor: ClassVar[str] = "sandbox"
    supported_executors: ClassVar[frozenset[str]] = frozenset({"sandbox"})
    hot_artifacts: ClassVar[tuple[str, ...]] = (
        "transcript.jsonl",
        "stderr.log",
        "native_events.jsonl",
    )

    @property
    def version(self) -> str:
        return self.config.cli_version

    async def install(self) -> None:
        from ale_run.agents._bootstrap import ensure_cua_mcp_server

        cfg = self.config
        sandbox = self.executor.sandbox
        directory = Path.home() / ".local" / "share" / "ale" / "muse" / cfg.cli_version
        binary = directory / ("muse" if sandbox.is_linux else "muse.exe")
        candidate = str(binary) if binary.is_file() else shutil.which("muse")
        actual = await asyncio.to_thread(installed_version, candidate) if candidate else None
        if actual != cfg.cli_version:
            binary = await asyncio.to_thread(
                install_release, cfg.cli_version, is_linux=sandbox.is_linux, directory=directory
            )
            candidate = str(binary)
            actual = await asyncio.to_thread(installed_version, candidate)
        if actual != cfg.cli_version:
            raise RuntimeError("muse_code: installed binary does not match cli_version")
        self._muse_path = candidate
        self.actual_cli_version = actual
        self.cli_source = "official"
        Path(self.executor.work_dir).mkdir(parents=True, exist_ok=True)
        await ensure_cua_mcp_server(sandbox)
        self._write_settings()

    def _write_settings(self) -> None:
        from ale_run.agents._bootstrap import cua_bridge_env

        sandbox = self.executor.sandbox
        cfg = self.config
        path_type = PurePosixPath if sandbox.is_linux else PureWindowsPath
        bridge = str(path_type(sandbox.mcp_server_dir) / "src" / "index.js")
        settings = {
            "schema_version": 1,
            "mcpServers": {
                "cua": {
                    "transport": "stdio",
                    "command": sandbox.node,
                    "args": [bridge],
                    "env": {
                        **cua_bridge_env(self.executor),
                        "CUA_COORDINATE_SPACE": cfg.coordinate_space,
                    },
                    "enabled": True,
                    "mode": "required",
                }
            },
        }
        env = {**os.environ, **(self.executor.env or {})}
        directory = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "muse"
        directory.mkdir(parents=True, exist_ok=True)
        content = json.dumps(settings, indent=2, ensure_ascii=False)
        (directory / "settings.json").write_text(content, encoding="utf-8")
        (Path(self.executor.work_dir) / "muse-settings.json").write_text(content, encoding="utf-8")

    def _build_env(self) -> dict[str, str]:
        cfg = self.config
        env = {**os.environ, **(self.executor.env or {})}
        key = cfg.api_key or env.get("META_API_KEY")
        if not key:
            raise RuntimeError("muse_code: set META_API_KEY or config.api_key before launch")
        env.update(
            {
                "META_API_KEY": key,
                "MUSE_NO_AUTO_UPDATE": "1",
                "MUSE_LOGIN": "0",
                "NO_COLOR": "1",
            }
        )
        return env

    def _build_argv(self) -> list[str]:
        cfg: MuseCodeConfig = self.config
        wd = Path(self.executor.work_dir)
        argv = [
            self._muse_path,
            "exec",
            "--json",
            "--model",
            cfg.model,
            "--reasoning-effort",
            cfg.reasoning_effort,
            "--workspace",
            str(wd),
            "--prompt-file",
            str(wd / "prompt.txt"),
            "--disable-request-user-input",
        ]
        if cfg.yolo:
            argv.append("--yolo")
        else:
            argv += ["--approval-mode", cfg.approval_mode, "--sandbox-network", cfg.sandbox_network]
            if cfg.trust_workspace:
                argv.append("--trust-workspace")
        if cfg.base_url:
            argv += ["--base-url", cfg.base_url]
        if cfg.max_turns is not None and cfg.max_turns > 0:
            argv += ["--max-model-steps", str(cfg.max_turns)]
        if cfg.max_tool_output_bytes is not None:
            argv += ["--max-tool-output-bytes", str(cfg.max_tool_output_bytes)]
        if cfg.parallel_tool_calls is not None:
            argv.append(
                "--parallel-tool-calls" if cfg.parallel_tool_calls else "--no-parallel-tool-calls"
            )
        for name in ("disable_web_tools", "disable_reminders", "no_foreign_personal_context"):
            if getattr(cfg, name):
                argv.append("--" + name.replace("_", "-"))
        settings = dict(cfg.settings_overrides)
        if cfg.context_window is not None:
            settings["context_compaction.provider_context_limit_tokens"] = cfg.context_window
        if cfg.auto_compact_token_limit is not None:
            settings["context_compaction.soft_threshold"] = (
                cfg.auto_compact_token_limit / cfg.context_window
            )
        for field in ("strategy", "soft_threshold", "hard_threshold"):
            value = getattr(cfg, "context_compaction_" + field)
            if value is not None:
                settings["context_compaction." + field] = value
        if cfg.execution_capacity is not None:
            settings["agents.execution_capacity"] = cfg.execution_capacity
        if cfg.toolset is not None:
            settings["run.toolset"] = list(dict.fromkeys([*cfg.toolset, "mcp__cua__*"]))
        for key, value in settings.items():
            argv += ["-c", key + "=" + json.dumps(value, ensure_ascii=False)]
        return argv

    async def launch(self, prompt: str) -> AgentRunResult:
        wd = Path(self.executor.work_dir)
        wd.mkdir(parents=True, exist_ok=True)
        self._data_dir = wd / "muse-data" / uuid.uuid4().hex
        (wd / "native_data_path.json").write_text(
            json.dumps(self._data_dir.relative_to(wd).as_posix()), encoding="utf-8"
        )
        env = self._build_env()
        secrets = secret_values(self.config, env)
        (wd / "prompt.txt").write_text(prompt, encoding="utf-8")
        for name in ("transcript.jsonl", "stderr.log", "native_events.jsonl", "muse.pid"):
            (wd / name).unlink(missing_ok=True)
        session_root = (
            Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "muse" / "sessions"
        )
        mirror = NativeEventMirror(
            wd, session_root, secrets, exclude=set(session_root.rglob("session.jsonl"))
        )
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *self._build_argv(),
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(wd),
            env=env,
            start_new_session=self.executor.sandbox.is_linux,
            limit=16 * 1024 * 1024,
        )
        (wd / "muse.pid").write_text(str(proc.pid), encoding="ascii")

        async def capture(stream: asyncio.StreamReader, path: Path) -> None:
            with path.open("w", encoding="utf-8") as output:
                while line := await stream.readline():
                    output.write(redact(line.decode("utf-8", errors="replace"), secrets))
                    output.flush()

        readers = [
            asyncio.create_task(capture(proc.stdout, wd / "transcript.jsonl")),
            asyncio.create_task(capture(proc.stderr, wd / "stderr.log")),
        ]
        try:
            while proc.returncode is None:
                await asyncio.to_thread(mirror.poll)
                for reader in readers:
                    if reader.done() and not reader.cancelled() and reader.exception():
                        raise reader.exception()
                await asyncio.sleep(0.5)
            await self._stop_tree(proc)
            await asyncio.wait_for(asyncio.gather(*readers), timeout=5)
        finally:
            # Reap the native CLI and its MCP subprocesses on cancellation too.
            cleanup = asyncio.create_task(self._stop_tree(proc))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            await asyncio.to_thread(mirror.poll)
            await asyncio.to_thread(mirror.snapshot, self._data_dir)
            if self.config.telemetry_enabled:
                await asyncio.to_thread(recover_telemetry_artifacts, wd)
        terminal = None
        for record in read_records(wd / "transcript.jsonl"):
            if str(record.get("payload_type", "")).startswith("run.terminal."):
                terminal = record.get("payload", {})
        completed = proc.returncode == 0 and terminal and terminal.get("terminal") == "completed"
        error = None
        if not completed:
            reason = (terminal or {}).get("reason") or "no successful terminal event"
            tail = (wd / "stderr.log").read_text(encoding="utf-8", errors="replace")[-1500:]
            error = redact(f"muse_code failed (rc={proc.returncode}): {reason}; {tail}", secrets)
        return AgentRunResult(
            status="completed" if completed else "failed",
            exit_code=proc.returncode,
            pid=proc.pid,
            transcript_path=str(wd / "transcript.jsonl"),
            stderr_path=str(wd / "stderr.log"),
            duration_s=time.monotonic() - started,
            error=error,
        )

    async def _stop_tree(self, proc: asyncio.subprocess.Process) -> None:
        if self.executor.sandbox.is_linux:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.05)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif proc.returncode is None:
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=30,
            )
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()

    @classmethod
    def parse_artifacts(
        cls,
        *,
        work_dir: Path,
        config: MuseCodeConfig,
        run_result: AgentRunResult,
        builder: TrajectoryBuilder,
    ) -> None:
        from .trajectory import parse_trajectory

        if config.telemetry_enabled:
            recover_telemetry_artifacts(work_dir)
        parse_trajectory(work_dir, builder)
        builder.trajectory.extra["muse_code"] = {
            "exit_code": run_result.exit_code,
            "transcript_path": str(work_dir / "transcript.jsonl"),
            "native_events_path": str(work_dir / "native_events.jsonl"),
            "telemetry_summary_path": str(work_dir / "telemetry_summary.json"),
        }
