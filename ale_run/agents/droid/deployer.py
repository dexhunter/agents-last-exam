"""DroidDeployer — drives the Factory.ai ``droid`` CLI.

Pre-built binary; NOT installable via npm.  The deployer expects the
binary to exist on PATH (baked into the sandbox image).  If missing,
attempts a download from the official install script.

Factory authentication is separate from the model provider credentials
configured in ``settings.json``.

MCP config at ``~/.factory/mcp.json``.  Headless via
``--skip-permissions-unsafe``.  Output: stream-json NDJSON.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import ClassVar

from ale_run.base_interface import (
    AgentRunResult,
    BaseAgentDeployer,
    ContentPart,
    Observation,
    StepMetrics,
    ToolCall,
    ToolResult,
    TrajectoryBuilder,
)

from .config import DroidConfig

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 2.0
_TERM_GRACE_S = 2.0


class DroidDeployer(BaseAgentDeployer):
    """Stdlib-only deployer for the Factory.ai ``droid`` CLI."""

    default_executor: ClassVar[str] = "sandbox"
    supported_executors: ClassVar[frozenset[str]] = frozenset({"sandbox"})
    hot_artifacts: ClassVar[tuple[str, ...]] = ("transcript.jsonl", "stderr.log")

    @property
    def version(self) -> str | None:
        return self.config.cli_version

    # =========================================================================
    # install
    # =========================================================================

    async def _auto_install_cli(self) -> None:
        """Install the exact configured official release."""
        version = self.config.cli_version
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ValueError(f"droid: invalid CLI version {version!r}")
        home = os.path.expanduser("~")
        bin_dir = f"{home}/.local/bin"
        os.makedirs(bin_dir, exist_ok=True)

        direct = await asyncio.to_thread(
            subprocess.run,
            [
                "bash", "-c",
                f'curl -fsSL "https://downloads.factory.ai/factory-cli/releases/'
                f'{version}/linux/x64/droid" '
                f'-o "{bin_dir}/droid" && chmod +x "{bin_dir}/droid"',
            ],
            capture_output=True, text=True, timeout=180,
        )
        if direct.returncode == 0:
            logger.info("droid: installed via pinned download (v%s)", version)
        else:
            raise RuntimeError(
                f"droid: pinned download failed (rc={direct.returncode}): "
                f"{(direct.stderr or '')[:300]}"
            )

        os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"

    @staticmethod
    def _installed_version(path: str) -> str | None:
        try:
            probe = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        match = re.search(r"\b\d+\.\d+\.\d+\b", probe.stdout or "")
        return match.group() if probe.returncode == 0 and match else None

    async def install(self) -> None:
        cfg: DroidConfig = self.config  # type: ignore[assignment]
        sandbox = self.executor.sandbox

        droid_path = shutil.which("droid")
        installed = await asyncio.to_thread(self._installed_version, droid_path) if droid_path else None
        if installed != cfg.cli_version:
            logger.info("droid: installed version %s; installing %s", installed, cfg.cli_version)
            await self._auto_install_cli()
            droid_path = shutil.which("droid")
            if not droid_path:
                raise RuntimeError(
                    "DroidDeployer: 'droid' still not found after install"
                )
        self._droid_path = droid_path
        installed = await asyncio.to_thread(self._installed_version, droid_path)
        if installed != cfg.cli_version:
            raise RuntimeError(f"droid: installed version {installed!r} != {cfg.cli_version!r}")
        self.actual_cli_version = installed
        self.cli_source = "official"
        logger.info("droid: CLI ok — %s", installed)

        wd = Path(self.executor.work_dir)
        wd.mkdir(parents=True, exist_ok=True)

        home = os.path.expanduser("~")
        factory_home = Path(home) / ".factory"
        factory_home.mkdir(parents=True, exist_ok=True)

        # Ensure the cua MCP bridge is installed at sandbox.mcp_server_dir
        # (idempotent: no-op when prebaked, install when missing).
        from ale_run.agents._bootstrap import cua_bridge_env, ensure_cua_mcp_server
        await ensure_cua_mcp_server(sandbox)

        # MCP config
        mcp_config = {
            "mcpServers": {
                "cua": {
                    "type": "stdio",
                    "command": sandbox.node,
                    "args": [self._join(sandbox.mcp_server_dir, "src", "index.js",
                                        is_linux=sandbox.is_linux)],
                    "env": cua_bridge_env(self.executor),
                    "disabled": False,
                },
            },
        }
        (factory_home / "mcp.json").write_text(
            json.dumps(mcp_config, indent=2), encoding="utf-8",
        )

        # Settings with OpenRouter BYOK
        or_key = os.environ.get("OPENROUTER_API_KEY") or ""
        for k, v in (self.executor.env or {}).items():
            if k == "OPENROUTER_API_KEY":
                or_key = v
        # A literal cfg.api_key (travels with the serialized config) takes
        # precedence and avoids env collisions.
        api_key = cfg.api_key or or_key
        if not api_key:
            raise RuntimeError(
                "DroidDeployer: neither config api_key nor OPENROUTER_API_KEY "
                "is set. Set one before install()."
            )
        settings = {
            "customModels": [
                {
                    "model": cfg.model,
                    "displayName": f"{cfg.model} [OpenRouter]",
                    "baseUrl": cfg.base_url or "https://openrouter.ai/api/v1",
                    "apiKey": api_key,
                    "provider": cfg.byok_provider,
                    "maxOutputTokens": cfg.max_output_tokens,
                },
            ],
        }
        (factory_home / "settings.json").write_text(
            json.dumps(settings, indent=2), encoding="utf-8",
        )
        logger.info("droid: config staged at %s", factory_home)
        self._disabled_tools = await self._resolve_disabled_tools(cfg, wd)

    async def _resolve_disabled_tools(self, cfg: DroidConfig, work_dir: Path) -> tuple[str, ...]:
        if "squad-board" not in cfg.disabled_tools:
            return tuple(cfg.disabled_tools)
        probe = await asyncio.to_thread(
            subprocess.run,
            [
                self._droid_path,
                "exec",
                "--list-tools",
                "--output-format",
                "json",
                "--model",
                cfg.model,
                "--cwd",
                str(work_dir),
                "--disabled-tools",
                "squad-board",
            ],
            capture_output=True,
            text=True,
            timeout=45,
            env=self._build_env(cfg),
            cwd=str(work_dir),
        )
        if probe.returncode != 0 and "Unknown tool identifier(s)" in (probe.stderr or ""):
            logger.info("droid: squad-board is unavailable in the installed CLI")
            return tuple(tool for tool in cfg.disabled_tools if tool != "squad-board")
        return tuple(cfg.disabled_tools)

    # =========================================================================
    # launch
    # =========================================================================

    async def launch(self, prompt: str) -> AgentRunResult:
        cfg: DroidConfig = self.config  # type: ignore[assignment]
        wd = Path(self.executor.work_dir)
        wd.mkdir(parents=True, exist_ok=True)

        prompt_file = wd / "prompt.txt"
        transcript_file = wd / "transcript.jsonl"
        stderr_log = wd / "stderr.log"
        pid_file = wd / "droid.pid"

        for f in (transcript_file, stderr_log, pid_file):
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass

        prompt_file.write_text(prompt, encoding="utf-8")

        argv = self._build_argv(cfg, str(prompt_file))
        env = self._build_env(cfg)

        t0 = time.monotonic()
        with open(transcript_file, "wb") as tout, \
             open(stderr_log, "wb") as terr:
            proc = await asyncio.to_thread(
                subprocess.Popen,
                argv,
                stdin=subprocess.DEVNULL,
                stdout=tout,
                stderr=terr,
                env=env,
                cwd=str(wd),
                start_new_session=True if hasattr(os, "setsid") else False,
            )
        pid_file.write_text(str(proc.pid), encoding="ascii")
        logger.info("droid: spawned pid=%s", proc.pid)

        # The episode wall budget is orchestration-owned: the executor
        # wraps launch() in asyncio.wait_for(timeout=timeout_s) (derived
        # from the task), so we just wait for the child here. If that
        # budget fires we are cancelled mid-await; reap the child before
        # propagating so it cannot outlive the run.
        try:
            while proc.poll() is None:
                await asyncio.sleep(_POLL_INTERVAL_S)
        except asyncio.CancelledError:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(proc.wait), timeout=_TERM_GRACE_S,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            raise

        duration_s = time.monotonic() - t0
        exit_code = proc.returncode
        status = "completed" if exit_code == 0 else "failed"
        error: str | None = None
        if status == "failed":
            error = _diagnose_failure(stderr_log, transcript_file, exit_code)
        return AgentRunResult(
            status=status,
            pid=proc.pid,
            exit_code=exit_code,
            transcript_path=str(transcript_file),
            stderr_path=str(stderr_log),
            duration_s=duration_s,
            error=error,
        )

    # =========================================================================
    # internals
    # =========================================================================

    @staticmethod
    def _join(*parts: str, is_linux: bool) -> str:
        sep = "/" if is_linux else "\\"
        head = parts[0].rstrip("/\\")
        tail = sep.join(p.strip("/\\") for p in parts[1:])
        return f"{head}{sep}{tail}" if tail else head

    def _build_argv(self, cfg: DroidConfig, prompt_file: str) -> list[str]:
        argv = [
            self._droid_path,
            "exec",
            "-f", prompt_file,
            "-m", cfg.model,
            "--output-format", "stream-json",
            "--cwd", str(Path(self.executor.work_dir)),
        ]
        if cfg.skip_permissions_unsafe:
            argv.append("--skip-permissions-unsafe")
        if cfg.reasoning_effort and cfg.reasoning_effort != "off":
            argv.extend(["--reasoning-effort", cfg.reasoning_effort])
        disabled_tools = getattr(self, "_disabled_tools", cfg.disabled_tools)
        if disabled_tools:
            argv.extend(["--disabled-tools", ",".join(disabled_tools)])
        # Deny-only tool policy: no --enabled-tools allow list.
        return argv

    def _build_env(self, cfg: DroidConfig) -> dict[str, str]:
        env = os.environ.copy()
        for k, v in (self.executor.env or {}).items():
            env[k] = v
        env["NO_COLOR"] = "1"
        return env

    # =========================================================================
    # parse_artifacts
    # =========================================================================

    @classmethod
    def parse_artifacts(
        cls,
        *,
        work_dir: Path,
        config: DroidConfig,
        run_result: AgentRunResult,
        builder: TrajectoryBuilder,
    ) -> None:
        transcript_file = work_dir / "transcript.jsonl"
        if not transcript_file.exists():
            builder.add_step(
                source="system",
                message=f"droid: no transcript at {transcript_file}",
                extra={"reason": "no_transcript"},
            )
            return

        raw = transcript_file.read_text(encoding="utf-8", errors="replace")
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            cls._consume_event(event, builder)

        builder.trajectory.extra.setdefault("droid", {}).update({
            "exit_code": run_result.exit_code,
            "transcript_path": str(transcript_file),
        })

    @classmethod
    def _consume_event(cls, event: dict, builder: TrajectoryBuilder) -> None:
        etype = event.get("type")
        if etype == "message":
            role = event.get("role")
            if role == "assistant":
                builder.add_step(source="agent", message=event.get("text", ""))
            elif role == "user":
                builder.add_step(source="user", message=event.get("text", ""))
        elif etype == "tool_call":
            cls._consume_tool_call(event, builder)
        elif etype == "tool_result":
            cls._consume_tool_result(event, builder)
        elif etype == "completion":
            cls._consume_completion(event, builder)
        elif etype == "error":
            builder.add_step(
                source="system",
                message=event.get("message", "unknown error"),
            )

    @staticmethod
    def _consume_tool_call(event: dict, builder: TrajectoryBuilder) -> None:
        params = event.get("parameters", {})
        builder.add_step(
            source="agent",
            tool_calls=[ToolCall(
                id=event.get("id", ""),
                name=event.get("toolName") or event.get("toolId", ""),
                arguments=params if isinstance(params, dict) else {"raw": str(params)},
            )],
        )

    @staticmethod
    def _consume_tool_result(event: dict, builder: TrajectoryBuilder) -> None:
        value = event.get("value", "")
        builder.add_step(
            source="environment",
            observation=Observation(results=[
                ToolResult(
                    tool_call_id=event.get("id", ""),
                    content=[ContentPart(type="text", text=str(value))],
                    is_error=bool(event.get("isError")),
                ),
            ]),
        )

    @staticmethod
    def _consume_completion(event: dict, builder: TrajectoryBuilder) -> None:
        usage = event.get("usage", {})
        metrics = StepMetrics(
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_input_tokens"),
            cache_creation_tokens=usage.get("cache_creation_input_tokens"),
        )
        builder.trajectory.extra.setdefault("droid", {})["completion"] = event
        final_text = event.get("finalText", "")
        if final_text:
            builder.add_step(source="agent", message=final_text, metrics=metrics)


def _diagnose_failure(stderr_log: Path, transcript: Path, exit_code: int | None) -> str:
    parts = [f"agent failed (rc={exit_code})"]
    stderr_text = _read_text_tolerant(stderr_log)
    tx_text = _read_text_tolerant(transcript)
    if stderr_text.strip():
        parts.append(f"stderr tail: ...{stderr_text[-800:]}")
    if tx_text.strip():
        parts.append(f"transcript tail: ...{tx_text[-800:]}")
    return " | ".join(parts)


def _read_text_tolerant(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return ""
