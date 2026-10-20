from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from ale_run.agents.openclaw_cli import runtime
from ale_run.agents.openclaw_cli.deployer import _serialize_cua_tools


def test_cua_tools_are_registered_as_sequential(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.cjs").write_text(
        'exports.default = api => api.registerTool('
        '{name: "click", execute: () => api.pluginConfig.value}, {optional: true});'
    )
    _serialize_cua_tools(tmp_path)
    script = '''
const plugin = require(process.argv[1]);
const api = {pluginConfig: {value: 42}, registerTool(tool, options) {
  console.log(JSON.stringify({name: tool.name, mode: tool.executionMode,
                              value: tool.execute(), optional: options.optional}));
}};
plugin.default(api);
'''
    result = subprocess.run(
        [node, "-e", script, str(dist / "index.cjs")],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout) == {
        "name": "click", "mode": "sequential", "value": 42, "optional": True,
    }


@pytest.mark.parametrize("reported_version", ["2026.10.5", "2026.9.9"])
def test_cached_install_verifies_running_cli_version(tmp_path, monkeypatch, reported_version):
    monkeypatch.setenv("HOME", str(tmp_path))
    prefix = tmp_path / ".ale-openclaw/cli/2026.10.5"
    package = prefix / "node_modules/openclaw"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"name": "openclaw", "version": "2026.10.5"}))
    (package / "openclaw.mjs").touch()
    (prefix / "ale-source.json").write_text(json.dumps({"source": "openclaw@2026.10.5"}))
    calls = []

    async def fake_command(args, **kwargs):
        calls.append(args)
        if args[-1] == "--version" and len(args) == 2:
            return SimpleNamespace(returncode=0, stdout="v24.22.0\n", stderr="")
        return SimpleNamespace(returncode=0, stdout=f"OpenClaw {reported_version}\n", stderr="")

    monkeypatch.setattr(runtime, "command", fake_command)
    if reported_version == "2026.10.5":
        node, entry = asyncio.run(runtime.install_cli("node", "npm", "2026.10.5"))
        assert node == "node" and entry == str(package / "openclaw.mjs")
        assert len(calls) == 2
    else:
        with pytest.raises(RuntimeError, match="does not match cli_version"):
            asyncio.run(runtime.install_cli("node", "npm", "2026.10.5"))
