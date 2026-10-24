import json
import os
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tasks.engineering.openroad_sky130_ibex_pnr_signoff import main as task
from tasks.engineering.openroad_sky130_ibex_pnr_signoff.scripts import verify_submission as verifier

NATIVE_POPEN = subprocess.Popen


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM openroad/orfs@sha256:" + "a" * 64 + "\n")
    return tmp_path


@pytest.fixture
def docker(monkeypatch):
    state = {"Running": False, "OOMKilled": False, "Error": ""}
    commands = []

    def control(command, **kwargs):
        commands.append(command)
        assert kwargs["timeout"] == verifier.DOCKER_CONTROL_TIMEOUT_S
        return subprocess.CompletedProcess(command, 0, json.dumps(state), "")

    monkeypatch.setattr(verifier.subprocess, "run", control)
    process = Mock(pid=123, wait=Mock(return_value=0), poll=Mock(return_value=0))
    launch = Mock(return_value=process)
    monkeypatch.setattr(verifier.subprocess, "Popen", launch)
    return SimpleNamespace(state=state, commands=commands, process=process, launch=launch)


def test_flow_uses_only_remaining_shared_budget_and_keeps_logs(workspace, docker, monkeypatch):
    monkeypatch.setattr(verifier.time, "monotonic", lambda: 6000.0)
    assert verifier._run_docker_flow_once(workspace, deadline=7200.0) == 0
    docker.process.wait.assert_called_once_with(timeout=1200.0)
    command = docker.launch.call_args.args[0]
    assert "--pull=never" in command
    assert "--rm" not in command
    assert docker.launch.call_args.kwargs["start_new_session"]
    name = command[command.index("--name") + 1]
    assert (workspace / f"{name}.stdout").exists()
    assert (workspace / f"{name}.stderr").exists()
    assert docker.commands[-1][-3:] == ["rm", "--force", name]


def test_slow_state_collection_after_completed_flow_does_not_trigger_zero(
    workspace, docker, monkeypatch
):
    clock = [100.0]
    monkeypatch.setattr(verifier.time, "monotonic", lambda: clock[0])
    control = verifier.subprocess.run

    def wait(*, timeout):
        clock[0] = 999.0
        return 0

    def collect(command, **kwargs):
        if "inspect" in command and "--format" in command:
            clock[0] = 1010.0
        return control(command, **kwargs)

    docker.process.wait.side_effect = wait
    monkeypatch.setattr(verifier.subprocess, "run", collect)
    assert verifier._run_docker_flow_once(workspace, deadline=1000.0) == 0


def test_expired_shared_budget_never_starts_another_flow(workspace, docker, monkeypatch):
    monkeypatch.setattr(verifier.time, "monotonic", lambda: 7200.0)
    with pytest.raises(verifier.FlowBudgetExceeded):
        verifier._run_docker_flow_once(workspace, deadline=7200.0)
    docker.launch.assert_not_called()


@pytest.mark.parametrize("recovered", [True, False])
def test_stopped_docker_is_recovered_once_or_remains_infrastructure_error(
    workspace, docker, monkeypatch, recovered,
):
    control = verifier.subprocess.run
    probes = 0
    starts = []

    def stopped_daemon(command, **kwargs):
        nonlocal probes
        if "image" in command:
            probes += 1
            if probes == 1:
                return subprocess.CompletedProcess(command, 1, "", "daemon unavailable")
        if "systemctl" in command:
            starts.append(command)
            if not recovered:
                return subprocess.CompletedProcess(command, 1, "", "service is masked")
        return control(command, **kwargs)

    monkeypatch.setattr(verifier.subprocess, "run", stopped_daemon)
    if recovered:
        assert verifier._run_docker_flow_once(workspace) == 0
        assert probes == 2
    else:
        with pytest.raises(RuntimeError, match="service recovery failed"):
            verifier._run_docker_flow_once(workspace)
        docker.launch.assert_not_called()
    assert len(starts) == 1


@pytest.mark.parametrize("state_key,state_value", [("OOMKilled", True), ("Error", "disk failure")])
def test_container_infrastructure_failure_is_not_budget_zero(
    workspace, docker, state_key, state_value
):
    docker.state[state_key] = state_value
    docker.process.wait.side_effect = subprocess.TimeoutExpired("docker run", 1)
    with pytest.raises(RuntimeError, match="infrastructure failure"):
        verifier._run_docker_flow_once(workspace, deadline=time.monotonic() + 1)
    assert "rm" in docker.commands[-1]


def test_timeout_without_running_computation_is_infrastructure_error(workspace, docker):
    docker.process.wait.side_effect = subprocess.TimeoutExpired("docker run", 1)
    with pytest.raises(RuntimeError, match="without a running computation"):
        verifier._run_docker_flow_once(workspace, deadline=time.monotonic() + 1)


@pytest.mark.parametrize("failed_command", ["image", "inspect", "rm"])
@pytest.mark.parametrize("timeout", [False, True])
def test_docker_control_failure_remains_error(
    workspace, docker, monkeypatch, failed_command, timeout
):
    control = verifier.subprocess.run

    def fail(command, **kwargs):
        if failed_command in command:
            if timeout:
                raise subprocess.TimeoutExpired(command, 30)
            return subprocess.CompletedProcess(command, 1, "", "daemon unavailable")
        return control(command, **kwargs)

    monkeypatch.setattr(verifier.subprocess, "run", fail)
    with pytest.raises((RuntimeError, subprocess.TimeoutExpired)):
        verifier._run_docker_flow_once(workspace)
    if failed_command == "image":
        docker.launch.assert_not_called()


@pytest.mark.parametrize("returncode", [125, 126, 127, 137, 143, -9])
def test_docker_launch_failure_is_not_a_submission_score(workspace, docker, returncode):
    docker.process.wait.return_value = returncode
    with pytest.raises(RuntimeError, match="Docker execution failed"):
        verifier._run_docker_flow_once(workspace)


def test_failed_engineering_flow_keeps_existing_partial_credit_path(workspace, docker):
    docker.process.wait.return_value = 2
    assert verifier._run_docker_flow_once(workspace) == 2


@pytest.mark.parametrize(
    "message",
    [
        "No space left on device",
        "Read-only file system",
        "Cannot allocate memory",
        "Disk quota exceeded",
    ],
)
def test_resource_failure_in_flow_logs_is_error(workspace, docker, message):
    def launch(command, **kwargs):
        kwargs["stderr"].write(message)
        kwargs["stderr"].flush()
        return docker.process

    docker.process.wait.return_value = 2
    docker.launch.side_effect = launch
    with pytest.raises(RuntimeError, match="infrastructure resource failure"):
        verifier._run_docker_flow_once(workspace)


def test_cleanup_failure_takes_precedence_over_budget_zero(workspace, docker, monkeypatch):
    docker.state["Running"] = True
    docker.process.wait.side_effect = subprocess.TimeoutExpired("docker run", 1)
    control = verifier.subprocess.run

    def fail_cleanup(command, **kwargs):
        if "rm" in command:
            return subprocess.CompletedProcess(command, 1, "", "daemon unavailable")
        return control(command, **kwargs)

    monkeypatch.setattr(verifier.subprocess, "run", fail_cleanup)
    with pytest.raises(RuntimeError, match="container cleanup failed"):
        verifier._run_docker_flow_once(workspace)


def test_cts_retry_cannot_reset_budget(workspace, monkeypatch):
    run = Mock(side_effect=[2, verifier.FlowBudgetExceeded("budget exhausted")])
    monkeypatch.setattr(verifier, "_run_docker_flow_once", run)
    monkeypatch.setattr(verifier, "_cts_illegal_instruction_detected", lambda path: True)
    with pytest.raises(verifier.FlowBudgetExceeded):
        verifier.run_docker_flow(workspace, routing_seed=42, deadline=123.0)
    assert len(run.call_args_list) == 2
    assert all(call.kwargs["deadline"] == 123.0 for call in run.call_args_list)
    assert all(call.kwargs["routing_seed"] == 42 for call in run.call_args_list)


def test_real_process_timeout_cleans_up_before_budget_exception(workspace, monkeypatch, docker):
    processes = []

    def launch(command, **kwargs):
        process = NATIVE_POPEN(
            [sys.executable, "-c", "import time; print('running', flush=True); time.sleep(30)"],
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(verifier.subprocess, "Popen", launch)
    docker.state["Running"] = True
    started = time.monotonic()
    try:
        with pytest.raises(verifier.FlowBudgetExceeded):
            verifier._run_docker_flow_once(workspace, deadline=started + 0.2)
        assert len(processes) == 1
        assert processes[0].returncode == -signal.SIGTERM
        assert "running" in next(workspace.glob("*.stdout")).read_text()
        assert time.monotonic() - started < 5
        assert "rm" in docker.commands[-1]
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


@pytest.fixture
def evaluation(tmp_path, monkeypatch):
    starter = tmp_path / "starter_project"
    configuration = starter / "flow/designs/sky130hd/ibex/config.mk"
    configuration.parent.mkdir(parents=True)
    exports = {**verifier.CONFIG_MK_IMMUTABLE_KEYS, "OR_SEED": "42", "CORE_UTILIZATION": "70"}
    configuration.write_text("".join(f"export {key} = {value}\n" for key, value in exports.items()))
    (starter / "Dockerfile").write_text("FROM openroad/orfs@sha256:" + "a" * 64 + "\n")
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "frozen_hashes.json").write_text("{}")
    (reference / "reference_metrics.json").write_text("{}")
    with zipfile.ZipFile(reference / "starter_project.zip", "w") as archive:
        for path in starter.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(tmp_path))
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "config.mk").write_text(configuration.read_text().replace("= 70", "= 45"))
    (submission / "JOURNAL.md").write_text("## Pass 1\nCORE_UTILIZATION: 70 -> 45\n")
    audit = submission / verifier.PASS_LOG_DIR_REL
    audit.mkdir(parents=True)
    (audit / "config.mk.pass0").write_text((submission / "config.mk").read_text())
    (audit / "pass0.stamp").write_text("done")
    work = tmp_path / "work"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verifier",
            "--submission-dir",
            str(submission),
            "--reference-dir",
            str(reference),
            "--work-dir",
            str(work),
        ],
    )
    return SimpleNamespace(submission=submission, reference=reference, work=work)


def write_flow_outputs(directory):
    files = {
        verifier.FINAL_GDS_REL: "GDS",
        verifier.FINAL_DEF_REL: "UNITS DISTANCE MICRONS 1 ;\nDIEAREA ( 0 0 ) ( 580 500 ) ;\n",
        verifier.FINISH_RPT_REL: (
            "wns max 0\ntns max 0\nTotal Power: 0.023 W\n"
            "=======\nfinish report_checks -path_delay min\n-------\n0.01 slack (MET)\n"
        ),
        verifier.DRC_COUNT_REL: "0 violations\n",
        verifier.LVS_LOG_REL: "Congratulations! Netlists match\n",
        verifier.SYNTH_STAT_REL: "synthesis complete\n",
    }
    for relative, text in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def test_two_clean_seeded_runs_share_one_deadline_and_keep_gates(evaluation, monkeypatch, capsys):
    clock = [100.0]
    monkeypatch.setattr(verifier.time, "monotonic", lambda: clock[0])
    calls = []

    def flow(directory, env=None, *, routing_seed=None, deadline=None):
        assert not (directory / verifier.FINAL_GDS_REL).exists()
        assert not (directory / verifier.FINISH_RPT_REL).exists()
        assert (
            "CORE_UTILIZATION = 45"
            in (directory / "flow/designs/sky130hd/ibex/config.mk").read_text()
        )
        calls.append((directory.name, routing_seed, deadline))
        clock[0] += 3000
        write_flow_outputs(directory)
        return 0

    monkeypatch.setattr(verifier, "_run_docker_flow_once", flow)
    assert verifier.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert calls == [("graded", None, 7300.0), ("reseed", 20260417, 7300.0)]
    gates = {gate["id"]: gate for gate in payload["gates"]}
    assert set(gates) == set(verifier.RUBRIC)
    assert all(gates[f"G{index}"]["passed"] for index in range(2, 10))
    assert gates["G12"]["passed"]
    assert gates["G11"]["passed"]
    assert payload["normalized_score"] == 1.0


@pytest.mark.parametrize("expired_run", [1, 2])
def test_exhaustion_in_either_run_is_whole_submission_zero(
    evaluation, monkeypatch, capsys, expired_run
):
    run_count = 0
    deadlines = []

    def flow(directory, env=None, *, routing_seed=None, deadline=None):
        nonlocal run_count
        run_count += 1
        deadlines.append(deadline)
        if run_count == expired_run:
            raise verifier.FlowBudgetExceeded("combined budget exhausted")
        write_flow_outputs(directory)
        return 0

    monkeypatch.setattr(verifier, "_run_docker_flow_once", flow)
    assert verifier.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["normalized_score"] == payload["total_score"] == 0
    assert payload["status"] == "computation_budget_exhausted"
    assert payload["flow_wall_budget_s"] == 7200
    assert "error" not in payload
    assert run_count == expired_run
    assert len(set(deadlines)) == 1


def test_main_enforces_aggregate_budget_in_actual_flow_runner(
    evaluation, docker, monkeypatch, capsys
):
    clock = [100.0]
    monkeypatch.setattr(verifier.time, "monotonic", lambda: clock[0])
    waits = []

    def wait(*, timeout):
        waits.append(timeout)
        if len(waits) == 1:
            clock[0] += 6000
            return 0
        clock[0] += timeout
        docker.state["Running"] = True
        raise subprocess.TimeoutExpired("docker run", timeout)

    docker.process.wait.side_effect = wait
    assert verifier.main() == 1
    assert waits == [7200.0, 1200.0]
    payload = json.loads(capsys.readouterr().out)
    assert payload["normalized_score"] == 0
    assert payload["status"] == "computation_budget_exhausted"
    assert docker.launch.call_count == 2


@pytest.mark.parametrize("failed_run", [1, 2])
def test_infrastructure_failure_in_either_run_never_emits_zero(
    evaluation, monkeypatch, capsys, failed_run
):
    runs = 0

    def flow(directory, **kwargs):
        nonlocal runs
        runs += 1
        if runs == failed_run:
            raise RuntimeError("Docker daemon unavailable")
        write_flow_outputs(directory)
        return 0

    monkeypatch.setattr(verifier, "run_docker_flow", flow)
    with pytest.raises(RuntimeError, match="Docker daemon unavailable"):
        verifier.main()
    assert not capsys.readouterr().out.strip()


def test_missing_reference_is_error_and_missing_submission_files_are_zero(evaluation, capsys):
    (evaluation.reference / "reference_metrics.json").unlink()
    with pytest.raises(FileNotFoundError):
        verifier.main()
    assert not capsys.readouterr().out.strip()
    (evaluation.reference / "reference_metrics.json").write_text("{}")
    (evaluation.submission / "config.mk").unlink()
    assert verifier.main() == 1
    assert json.loads(capsys.readouterr().out)["normalized_score"] == 0


def test_public_prompts_agree_on_resource_requirement():
    card = json.loads((Path(task.__file__).parent / "task_card.json").read_text())
    requirement = (
        "The final configuration must complete two full runs from clean working directories on the provided\n"
        "machine, using the original random seed and a different random seed, within two hours combined."
    )
    assert requirement in task.config.task_description
    assert requirement in card["taskPrompt"]
    assert verifier.FLOW_WALL_BUDGET_S == 7200
    assert task.VERIFIER_TIMEOUT_S >= verifier.FLOW_WALL_BUDGET_S + 300
    assert task.config.to_metadata()["evaluation_timeout_s"] == 8100
    assert task.EVALUATION_TIMEOUT_S >= task.VERIFIER_TIMEOUT_S + 300
