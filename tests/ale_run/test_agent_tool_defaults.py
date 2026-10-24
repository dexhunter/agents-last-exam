from pathlib import Path
from types import SimpleNamespace
import tomllib

import pytest
import yaml

from ale_run.agents.ale_claw.config import AleClawConfig
from ale_run.agents.claude_code.config import ClaudeCodeConfig
from ale_run.agents.cursor_cli.config import CursorCliConfig
from ale_run.agents.droid.config import DroidConfig
from ale_run.agents.forgecode.config import ForgecodeConfig
from ale_run.agents.gemini_cli.config import GeminiCliConfig
from ale_run.agents.gemini_cli.deployer import _YOLO_POLICY
from ale_run.agents.grok_build.config import GrokBuildConfig
from ale_run.agents.grok_cli.config import GrokCliConfig
from ale_run.agents.hermes.config import HermesConfig
from ale_run.agents.hermes.deployer import HermesDeployer
from ale_run.agents.openclaw_cli.config import OpenClawCliConfig
from ale_run.agents.pi_cli.config import PiCliConfig
from ale_run.agents.qwen_code.config import QwenCodeConfig


@pytest.mark.parametrize(
    "config_class",
    [
        AleClawConfig,
        ClaudeCodeConfig,
        CursorCliConfig,
        DroidConfig,
        ForgecodeConfig,
        GeminiCliConfig,
        GrokBuildConfig,
        GrokCliConfig,
        PiCliConfig,
        QwenCodeConfig,
    ],
)
def test_default_exclusions_are_empty(config_class):
    assert not config_class().disabled_tools


@pytest.mark.parametrize(
    "name",
    [
        "ale_claw",
        "claude_code",
        "cursor_cli",
        "droid",
        "forgecode",
        "gemini_cli",
        "grok_build",
        "grok_cli",
        "pi_cli",
    ],
)
def test_shipped_yaml_exclusions_are_empty(name):
    root = Path(__file__).resolve().parents[2]
    data = yaml.safe_load((root / "configs" / "agents" / f"{name}.yaml").read_text())
    assert not data["config"]["disabled_tools"]


def test_openclaw_default_has_no_deny_policy():
    config = OpenClawCliConfig()
    assert not config.tools_deny
    assert not config.plugins_deny


def test_gemini_default_policy_has_no_deny_rule():
    assert all(rule["decision"] != "deny" for rule in tomllib.loads(_YOLO_POLICY)["rule"])


@pytest.mark.parametrize("toolsets", [None, (), ("terminal", "mcp-cua")])
def test_hermes_toolsets_are_only_restricted_when_explicit(tmp_path, toolsets):
    bridge = tmp_path / "cua"
    (bridge / "src").mkdir(parents=True)
    (bridge / "src" / "index.js").touch()
    (bridge / "node_modules" / "@modelcontextprotocol" / "sdk").mkdir(parents=True)
    sandbox = SimpleNamespace(mcp_server_dir=str(bridge), node="node")
    config = HermesConfig(toolsets_enabled=toolsets)
    rendered = yaml.safe_load(
        HermesDeployer._build_config_yaml(config, sandbox, "http://127.0.0.1:8000")
    )

    assert "cua" in rendered["mcp_servers"]
    if toolsets:
        assert rendered["platform_toolsets"]["cli"] == list(toolsets)
    else:
        assert "platform_toolsets" not in rendered
