from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from types import ModuleType

from ale_run.base_interface import AgentRunResult
from ale_run.executors._sandbox_entry import run
from ale_run.executors.docker import DockerExecutor
from ale_run.orchestration.experiment_spec import AgentSpec, RunUnit
from ale_run.orchestration.lifecycle import _build_run_meta


@dataclass
class Config:
    model: str = "test"
    cli_version: str = "configured"
    api_key: str = "private-api-key"
    extra_envs: dict = field(default_factory=lambda: {
        "HF_TOKEN": "private-token", "X-API-Key": "private-header-key", "MODE": "test",
    })


class Deployer:
    def __init__(self, executor):
        self.executor = executor

    async def install(self):
        self.actual_cli_version = "actual"
        self.cli_source = "official"

    async def launch(self, prompt):
        return AgentRunResult(status="completed")


def test_entry_writes_actual_identity_after_install(tmp_path, monkeypatch):
    module = ModuleType("test_cli_identity_adapter")
    module.Config = Config
    module.Deployer = Deployer
    monkeypatch.setitem(sys.modules, module.__name__, module)
    spec = {
        "config_module": module.__name__, "config_class": "Config", "config_kwargs": {},
        "deployer_module": module.__name__, "deployer_class": "Deployer",
        "install_agent_deps": False, "sandbox_local": False,
        "sandbox_kwargs": {"id": "test", "endpoint": "", "os": "linux", "work_dir_base": "",
                           "task_data_root": "", "node": "", "python": "", "mcp_server_dir": ""},
        "work_dir": str(tmp_path), "prompt": "test", "timeout_s": 10,
    }
    assert run(spec)["ok"] is True
    identity = json.loads((tmp_path / "_cli_identity.json").read_text())
    assert identity == {"cli_version": "actual", "source": "official"}


def test_run_meta_keeps_actual_identity_and_redacts_expanded_config():
    unit = RunUnit("test", AgentSpec("test", "test", {}), "demo/test", 0)
    identity = {"cli_version": "actual", "source": "official"}
    meta = _build_run_meta(
        run_id="test", unit=unit, config=Config(), executor_type="sandbox",
        status="completed", score=1, phase=None, error_obj=None, error_str=None,
        total_s=0, trajectory=None, category=None, cli_identity=identity,
    )
    assert meta["agent"]["cli_identity"] == identity
    assert meta["agent"]["version"] == "configured"
    assert meta["agent"]["config_repr"]["extra_envs"]["MODE"] == "test"
    assert "private-api-key" not in json.dumps(meta)
    assert "private-token" not in json.dumps(meta)
    assert "private-header-key" not in json.dumps(meta)


async def test_docker_clears_stale_identity_when_startup_fails(tmp_path, monkeypatch):
    identity = tmp_path / "_cli_identity.json"
    identity.write_text('{"cli_version":"old","source":"official"}')
    monkeypatch.setattr("ale_run.executors.docker._docker_available", lambda: False)
    executor = DockerExecutor(config=Config(), work_dir=str(tmp_path), sandbox=None)
    result = await executor.run_deployer(deployer_cls=Deployer, prompt="test", timeout_s=1)
    assert result.status == "failed"
    assert not identity.exists()
