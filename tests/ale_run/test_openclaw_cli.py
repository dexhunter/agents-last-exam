from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from ale_run.agents.openclaw_cli import OpenClawCliConfig, OpenClawCliDeployer


def test_default_preset_uses_official_cli_and_local_desktop_tools() -> None:
    preset = yaml.safe_load(Path("configs/agents/openclaw_cli.yaml").read_text())
    config = preset["config"]
    assert config["cli_version"] == OpenClawCliConfig().cli_version
    assert not config["tarball_path"] and not config["tarball_url"]
    assert not config["tools_deny"] and not OpenClawCliConfig().tools_deny
    assert not config["plugins_allow"] and not OpenClawCliConfig().plugins_allow


def test_episode_state_isolated_from_existing_openclaw_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", "unrelated-config.json")
    config = OpenClawCliConfig()
    executor = SimpleNamespace(
        config=config,
        env={"OPENROUTER_API_KEY": "episode-key"},
        cua_bridge_url=lambda: "http://127.0.0.1:5000",
    )
    deployer = OpenClawCliDeployer(executor)
    deployer._episode_state_dir = tmp_path / "episode-state"
    deployer._write_config(config)

    assert not (tmp_path / ".openclaw").exists()
    generated = json.loads((deployer._state_dir / "openclaw.json").read_text())
    assert "deny" not in generated["tools"]
    assert "toolSearch" not in generated["tools"]
    assert "allow" not in generated["plugins"]
    assert generated["agents"]["defaults"]["modelPolicy"] == {"allow": []}
    env = deployer._build_env(config, deployer._state_dir / ".env")
    assert env["OPENCLAW_STATE_DIR"] == str(deployer._state_dir)
    assert "OPENCLAW_CONFIG_PATH" not in env


def test_write_config_supports_zai_primary_and_direct_openai_vision(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = OpenClawCliConfig(
        model="glm-5.2",
        provider="zai",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model_params={
            "temperature": 1.0,
            "extra_body": {
                "thinking": {"type": "enabled"},
                "reasoning_effort": "max",
            },
        },
        thinking="max",
        vision_model="openai/gpt-5.4",
        vision_provider="direct",
    )
    executor = SimpleNamespace(
        config=cfg,
        env={
            "GLM_API_KEY": "zai-test-key",
            "OPENAI_API_KEY": "openai-test-key",
        },
        cua_bridge_url=lambda: "http://127.0.0.1:5000",
    )
    deployer = OpenClawCliDeployer(executor)

    deployer._write_config(cfg)

    openclaw_config = json.loads(
        (tmp_path / ".openclaw" / "openclaw.json").read_text()
    )
    defaults = openclaw_config["agents"]["defaults"]
    assert defaults["model"]["primary"] == "zai/glm-5.2"
    assert defaults["imageModel"]["primary"] == "openai/gpt-5.4"
    assert "llm" not in defaults
    assert defaults["models"]["zai/glm-5.2"]["params"] == cfg.model_params
    assert defaults["modelPolicy"] == {"allow": []}
    assert defaults["models"]["openai/gpt-5.4"] == {}
    assert "media" not in openclaw_config["tools"]
    assert "allow" not in openclaw_config["plugins"]
    assert openclaw_config["models"]["providers"] == {
        "zai": {
            "baseUrl": "https://open.bigmodel.cn/api/paas/v4",
            "api": "openai-completions",
            "models": [{"id": "glm-5.2", "name": "glm-5.2"}],
        }
    }

    auth = json.loads(
        (
            tmp_path
            / ".openclaw"
            / "agents"
            / "main"
            / "agent"
            / "auth-profiles.json"
        ).read_text()
    )
    assert auth["profiles"] == {
        "zai:default": {
            "provider": "zai",
            "type": "api_key",
            "key": "zai-test-key",
        },
        "openai:default": {
            "provider": "openai",
            "type": "api_key",
            "key": "openai-test-key",
        },
    }
    assert auth["lastGood"] == {
        "zai": "zai:default",
        "openai": "openai:default",
    }


def test_write_config_supports_custom_primary_and_direct_openai_vision(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = OpenClawCliConfig(
        model="Azure-Code",
        provider="custom",
        provider_id="azure-code",
        model_id="rc6-for-vlm-team",
        base_url="https://example.openai.azure.com/openai/v1",
        api_key_env="AZURE_CODE_API_KEY",
        provider_api="openai-completions",
        model_params={"temperature": 1.0},
        thinking="off",
        vision_model="openai/gpt-5.4",
        vision_provider="direct",
        plugins_allow=("cua", "memory-core"),
    )
    executor = SimpleNamespace(
        config=cfg,
        env={
            "AZURE_CODE_API_KEY": "azure-test-key",
            "OPENAI_API_KEY": "openai-test-key",
        },
        cua_bridge_url=lambda: "http://127.0.0.1:5000",
    )
    deployer = OpenClawCliDeployer(executor)

    deployer._write_config(cfg)

    openclaw_config = json.loads(
        (tmp_path / ".openclaw" / "openclaw.json").read_text()
    )
    defaults = openclaw_config["agents"]["defaults"]
    assert defaults["model"]["primary"] == "azure-code/rc6-for-vlm-team"
    assert defaults["imageModel"]["primary"] == "openai/gpt-5.4"
    assert defaults["models"]["azure-code/rc6-for-vlm-team"]["params"] == {
        "temperature": 1.0,
    }
    assert "media" not in openclaw_config["tools"]
    assert "openai" in openclaw_config["plugins"]["allow"]
    assert "azure-code" not in openclaw_config["plugins"]["allow"]
    assert openclaw_config["models"]["providers"] == {
        "azure-code": {
            "baseUrl": "https://example.openai.azure.com/openai/v1",
            "api": "openai-completions",
            "models": [
                {
                    "id": "rc6-for-vlm-team",
                    "name": "Azure-Code",
                    "compat": {
                        "maxTokensField": "max_completion_tokens",
                        "supportsUsageInStreaming": True,
                    },
                },
            ],
        },
    }

    auth = json.loads(
        (
            tmp_path
            / ".openclaw"
            / "agents"
            / "main"
            / "agent"
            / "auth-profiles.json"
        ).read_text()
    )
    assert auth["profiles"] == {
        "azure-code:default": {
            "provider": "azure-code",
            "type": "api_key",
            "key": "azure-test-key",
        },
        "openai:default": {
            "provider": "openai",
            "type": "api_key",
            "key": "openai-test-key",
        },
    }


def test_write_config_supports_custom_native_vision_model(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = OpenClawCliConfig(
        model="Kimi 2.7",
        provider="custom",
        provider_id="moonshot",
        model_id="kimi-k2.7-code",
        base_url="https://api.moonshot.ai/v1",
        api_key_env="MOONSHOT_API_KEY",
        model_input_types=("text", "image"),
        vision_model="kimi-k2.7-code",
        plugins_allow=("cua", "memory-core"),
    )
    executor = SimpleNamespace(
        config=cfg,
        env={"MOONSHOT_API_KEY": "moonshot-test-key"},
        cua_bridge_url=lambda: "http://127.0.0.1:5000",
    )
    deployer = OpenClawCliDeployer(executor)

    deployer._write_config(cfg)

    openclaw_config = json.loads(
        (tmp_path / ".openclaw" / "openclaw.json").read_text()
    )
    defaults = openclaw_config["agents"]["defaults"]
    assert defaults["model"]["primary"] == "moonshot/kimi-k2.7-code"
    assert defaults["imageModel"]["primary"] == "moonshot/kimi-k2.7-code"
    assert "media" not in openclaw_config["tools"]
    assert openclaw_config["models"]["providers"] == {
        "moonshot": {
            "baseUrl": "https://api.moonshot.ai/v1",
            "api": "openai-completions",
            "models": [
                {
                    "id": "kimi-k2.7-code",
                    "name": "Kimi 2.7",
                    "input": ["text", "image"],
                    "compat": {
                        "maxTokensField": "max_completion_tokens",
                        "supportsUsageInStreaming": True,
                    },
                },
            ],
        },
    }


def test_custom_primary_compat_proxy_routes_primary_and_vision(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = OpenClawCliConfig(
        model="Kimi 2.7",
        provider="custom",
        provider_id="moonshot",
        model_id="kimi-k2.7-code",
        base_url="https://api.moonshot.ai/v1",
        api_key_env="MOONSHOT_API_KEY",
        model_input_types=("text", "image"),
        sanitize_empty_text_blocks=True,
        vision_model="kimi-k2.7-code",
        plugins_allow=("cua", "memory-core"),
    )
    executor = SimpleNamespace(
        config=cfg,
        env={"MOONSHOT_API_KEY": "moonshot-test-key"},
        cua_bridge_url=lambda: "http://127.0.0.1:5000",
    )
    deployer = OpenClawCliDeployer(executor)
    deployer._primary_compat_proxy_provider = "moonshot"
    deployer._primary_compat_proxy_url = "http://127.0.0.1:43210/v1"

    deployer._write_config(cfg)

    openclaw_config = json.loads(
        (tmp_path / ".openclaw" / "openclaw.json").read_text()
    )
    provider = openclaw_config["models"]["providers"]["moonshot"]
    assert provider["baseUrl"] == "http://127.0.0.1:43210/v1"
    assert provider["request"] == {"allowPrivateNetwork": True}


def test_write_config_routes_direct_openai_vision_through_usage_proxy(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = OpenClawCliConfig(
        model="glm-5.2",
        provider="zai",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        vision_model="openai/gpt-5.4",
        vision_provider="direct",
    )
    executor = SimpleNamespace(
        config=cfg,
        env={
            "GLM_API_KEY": "zai-test-key",
            "OPENAI_API_KEY": "openai-test-key",
        },
        cua_bridge_url=lambda: "http://127.0.0.1:5000",
    )
    deployer = OpenClawCliDeployer(executor)
    deployer._vision_usage_proxy_provider = "openai"
    deployer._vision_usage_proxy_url = "http://127.0.0.1:43210/v1"

    deployer._write_config(cfg)

    openclaw_config = json.loads(
        (tmp_path / ".openclaw" / "openclaw.json").read_text()
    )
    assert openclaw_config["models"]["providers"]["openai"] == {
        "baseUrl": "http://127.0.0.1:43210/v1",
        "models": [
            {
                "id": "gpt-5.4",
                "name": "openai/gpt-5.4",
                "input": ["text", "image"],
            }
        ],
        "request": {"allowPrivateNetwork": True},
    }


def test_direct_openai_primary_does_not_start_image_usage_proxy(
    tmp_path,
    monkeypatch,
) -> None:
    cfg = OpenClawCliConfig(
        model="openai/gpt-5.4",
        provider="direct",
        vision_model="openai/gpt-5.4",
        vision_provider="direct",
    )
    executor = SimpleNamespace(config=cfg)
    deployer = OpenClawCliDeployer(executor)

    class UnexpectedProxy:
        def __init__(self, **kwargs) -> None:
            raise AssertionError(f"proxy should not start: {kwargs}")

    monkeypatch.setattr(
        "ale_run.agents.openclaw_cli.deployer.VisionUsageProxy",
        UnexpectedProxy,
    )

    deployer._start_vision_usage_proxy(cfg, tmp_path)

    assert not hasattr(deployer, "_vision_usage_proxy")
