"""Install and verify the OpenClaw CLI and its Node runtime."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from ale_run.agents.qwen_code.runtime import command

_NODE_VERSION = "24.22.0"


async def install_cli(node: str, npm: str, version: str, tarball: str = "") -> tuple[str, str]:
    """Return verified Node and OpenClaw entry paths, using private installs."""
    if os.name == "nt":
        candidates = (
            Path(node).parent / "node_modules" / "npm" / "bin" / "npm-cli.js",
            Path(npm).parent / "node_modules" / "npm" / "bin" / "npm-cli.js",
        )
        npm_js = next((path for path in candidates if path.is_file()), None)
        if npm_js is None:
            raise RuntimeError("openclaw_cli: cannot locate npm-cli.js")
        npm_command = [node, str(npm_js)]
    else:
        npm_command = [npm]

    probe = await command([node, "--version"], timeout=30)
    try:
        node_version = tuple(int(part) for part in probe.stdout.strip().lstrip("v").split("."))
    except ValueError:
        node_version = ()
    compatible = probe.returncode == 0 and (
        (24, 16, 0) <= node_version < (25, 0, 0) or node_version >= (26, 1, 0)
    )
    root = Path.home() / ".ale-openclaw"
    if not compatible:
        runtime = root / "node" / _NODE_VERSION
        private_node = runtime / "node_modules" / "node" / "bin" / (
            "node.exe" if os.name == "nt" else "node"
        )
        if private_node.is_file():
            check = await command([str(private_node), "--version"], timeout=30)
            cached = check.returncode == 0 and check.stdout.strip() == f"v{_NODE_VERSION}"
        else:
            cached = False
        if not cached:
            runtime.mkdir(parents=True, exist_ok=True)
            result = await command(
                npm_command + ["install", "--prefix", str(runtime), "--no-save",
                               "--package-lock=false", "--no-audit", "--no-fund",
                               f"node@{_NODE_VERSION}"],
                timeout=600,
            )
            if result.returncode != 0:
                raise RuntimeError("openclaw_cli: private Node installation failed")
        node = str(private_node)
        check = await command([node, "--version"], timeout=30)
        if check.returncode != 0 or check.stdout.strip() != f"v{_NODE_VERSION}":
            raise RuntimeError("openclaw_cli: private Node version mismatch")
        if os.name == "nt":
            npm_command[0] = node

    env = {**os.environ, "PATH": str(Path(node).parent) + os.pathsep + os.environ.get("PATH", "")}
    prefix = root / "cli" / version
    package = prefix / "node_modules" / "openclaw"
    entry = package / "openclaw.mjs"
    marker = prefix / "ale-source.json"
    source = f"openclaw@{version}"
    if tarball:
        source = "sha256:" + hashlib.sha256(Path(tarball).read_bytes()).hexdigest()
    try:
        metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
        recorded = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        metadata, recorded = {}, {}
    if metadata.get("version") != version or not entry.is_file() or recorded.get("source") != source:
        prefix.mkdir(parents=True, exist_ok=True)
        result = await command(
            npm_command + ["install", "--prefix", str(prefix), "--no-save",
                           "--package-lock=false", "--no-audit", "--no-fund",
                           tarball or f"openclaw@{version}"],
            env=env, timeout=900,
        )
        if result.returncode != 0:
            raise RuntimeError("openclaw_cli: npm installation failed: " + result.stderr[-1000:])
        marker.write_text(json.dumps({"source": source}), encoding="utf-8")

    try:
        metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("openclaw_cli: installed package metadata is missing") from exc
    probe = await command([node, str(entry), "--version"], env=env, timeout=60)
    words = probe.stdout.strip().split()
    if (metadata.get("name") != "openclaw" or metadata.get("version") != version
            or probe.returncode != 0 or len(words) < 2 or words[:2] != ["OpenClaw", version]):
        raise RuntimeError("openclaw_cli: installed CLI does not match cli_version")
    os.environ["PATH"] = env["PATH"]
    return node, str(entry)
