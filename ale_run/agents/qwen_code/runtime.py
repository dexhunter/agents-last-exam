"""Cross-platform process ownership and official npm installation."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from pathlib import Path


async def stop_process(proc: asyncio.subprocess.Process, job=None, grace_s: float = 1) -> None:
    if job is not None and os.name == "nt":
        await asyncio.to_thread(job.stop)
    elif os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            await asyncio.sleep(grace_s)
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif proc.returncode is None:
        await asyncio.to_thread(
            subprocess.run,
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            timeout=30,
        )
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()
    if job is not None and os.name != "nt":
        await asyncio.to_thread(job.stop)


def assign_job(pid: int):
    if os.name != "nt":
        return None
    # Reuse the sandbox supervisor's nested, kill-on-close Windows Job Object.
    from ale_run.executors._sandbox_entry import _WindowsJob

    job = _WindowsJob()
    try:
        job.assign(pid)
    except BaseException:
        job.stop()
        raise
    return job


async def command(argv: list[str], *, env=None, timeout: float = 300):
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=os.name != "nt",
    )
    job = None
    try:
        job = assign_job(proc.pid)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        return subprocess.CompletedProcess(
            argv,
            proc.returncode,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
        )
    finally:
        cleanup = asyncio.create_task(stop_process(proc, job))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError


async def install_cli(node: str, npm: str, version: str) -> tuple[str, str]:
    prefix = Path.home() / ".ale-qwen-code" / "cli" / version
    package = prefix / "node_modules" / "@qwen-code" / "qwen-code"
    entry = package / "cli-entry.js"
    if os.name == "nt":
        npm_js = Path(npm).parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        npm_command = [node, str(npm_js)]
    else:
        npm_command = [npm]
    probe = await command([node, "--version"], timeout=30)
    major = int(probe.stdout.strip().lstrip("v").split(".")[0]) if probe.returncode == 0 else 0
    if major < 22:
        runtime = prefix.parent.parent / "node-24.14.0"
        private_node = (
            runtime / "node_modules" / "node" / "bin" / ("node.exe" if os.name == "nt" else "node")
        )
        cached = False
        if private_node.is_file():
            check = await command([str(private_node), "--version"], timeout=30)
            cached = check.returncode == 0 and check.stdout.strip() == "v24.14.0"
        if not cached:
            runtime.mkdir(parents=True, exist_ok=True)
            result = await command(
                npm_command
                + [
                    "install",
                    "--prefix",
                    str(runtime),
                    "--no-save",
                    "--package-lock=false",
                    "--no-audit",
                    "--no-fund",
                    "node@24.14.0",
                ]
            )
            if result.returncode != 0:
                raise RuntimeError("qwen_code: private Node installation failed")
        node = str(private_node)
        check = await command([node, "--version"], timeout=30)
        if check.returncode != 0 or check.stdout.strip() != "v24.14.0":
            raise RuntimeError("qwen_code: private Node version mismatch")
        if os.name == "nt":
            npm_command[0] = node
    env = {**os.environ, "PATH": str(Path(node).parent) + os.pathsep + os.environ.get("PATH", "")}
    try:
        metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        metadata = {}
    if metadata.get("version") != version or not entry.is_file():
        prefix.mkdir(parents=True, exist_ok=True)
        result = await command(
            npm_command
            + [
                "install",
                "--prefix",
                str(prefix),
                "--no-save",
                "--package-lock=false",
                "--no-audit",
                "--no-fund",
                f"@qwen-code/qwen-code@{version}",
            ],
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError("qwen_code: official npm installation failed")
    metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
    probe = await command([node, str(entry), "--version"], env=env, timeout=60)
    if (
        metadata.get("name") != "@qwen-code/qwen-code"
        or metadata.get("version") != version
        or probe.returncode != 0
        or probe.stdout.strip() != version
    ):
        raise RuntimeError("qwen_code: installed CLI does not match cli_version")
    return node, str(entry)
