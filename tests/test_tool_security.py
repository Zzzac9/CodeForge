import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from codeforge import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from codeforge import cli as codeforge_cli
from codeforge.sandbox import DockerSandbox, SandboxStatus, ShellSandboxConfig


def build_agent(tmp_path, *, approval="auto", backend="unsafe-host", outputs=()):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path, repo_root_override=tmp_path)
    return MiniAgent(
        model_client=FakeModelClient(list(outputs)),
        workspace=workspace,
        session_store=SessionStore(tmp_path / ".codeforge" / "sessions"),
        approval_policy=approval,
        shell_sandbox_config=ShellSandboxConfig(
            backend=backend,
            auto_start_docker_desktop=False,
            output_limit=4096,
        ),
    )


def shell_command(argv):
    if os.name == "nt":
        return subprocess.list2cmdline([os.fspath(item) for item in argv])
    return shlex.join([os.fspath(item) for item in argv])


def test_ask_approval_displays_arguments_target_risk_and_unknown_impact(tmp_path):
    agent = build_agent(tmp_path, approval="ask")
    with patch("builtins.input", return_value="n") as prompt:
        result = agent.run_tool("run_shell", {"command": "echo hello", "timeout": 3})

    message = prompt.call_args.args[0]
    assert "tool: run_shell" in message
    assert '"command": "echo hello"' in message
    assert "target paths:" in message
    assert "risk level: high" in message
    assert "possible impact:" in message
    assert "unknown impact: yes" in message
    assert result == "error: approval denied for run_shell"
    assert agent._last_tool_result_metadata["approval_result"] == "denied"
    assert agent._last_tool_result_metadata["safety_check_passed"] is True


def test_auto_approval_cannot_bypass_prohibited_shell_boundary(tmp_path):
    agent = build_agent(tmp_path, approval="auto")
    result = agent.run_tool("run_shell", {"command": "rm -rf /workspace", "timeout": 3})

    assert "mandatory safety check denied" in result
    metadata = agent._last_tool_result_metadata
    assert metadata["risk_level"] == "critical"
    assert metadata["approval_result"] == "not_requested"
    assert metadata["safety_check_passed"] is False
    assert metadata["security_event_type"] == "prohibited_operation"


def test_auto_approval_and_safety_check_are_recorded_separately(tmp_path):
    agent = build_agent(tmp_path, approval="auto")
    result = agent.run_tool("write_file", {"path": "safe.txt", "content": "ok\n"})

    assert result.startswith("wrote safe.txt")
    assert (tmp_path / "safe.txt").read_text(encoding="utf-8") == "ok\n"
    metadata = agent._last_tool_result_metadata
    assert metadata["approval_result"] == "approved"
    assert metadata["safety_check_passed"] is True
    assert metadata["risk_level"] == "high"
    assert metadata["affected_paths"] == ["safe.txt"]


def test_never_rejects_high_risk_before_execution(tmp_path):
    agent = build_agent(tmp_path, approval="never")
    result = agent.run_tool("write_file", {"path": "denied.txt", "content": "no"})

    assert result == "error: approval denied for write_file"
    assert not (tmp_path / "denied.txt").exists()


def test_absolute_and_parent_workspace_escapes_are_rejected(tmp_path):
    outside = tmp_path.parent / "outside-security-test.txt"
    outside.write_text("outside", encoding="utf-8")
    agent = build_agent(tmp_path)

    assert "path escapes workspace" in agent.run_tool("read_file", {"path": "../outside-security-test.txt"})
    assert "path escapes workspace" in agent.run_tool("read_file", {"path": os.fspath(outside.resolve())})


def test_symlink_is_rejected_without_reading_target(tmp_path):
    outside = tmp_path.parent / "outside-link-target.txt"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is not permitted on this Windows host: {exc}")
    agent = build_agent(tmp_path)

    result = agent.run_tool("read_file", {"path": "linked.txt"})
    assert "symlink/reparse paths are not allowed" in result
    assert "outside" not in result


def test_atomic_write_preserves_original_when_replace_fails(tmp_path):
    target = tmp_path / "atomic.txt"
    target.write_text("original", encoding="utf-8")
    agent = build_agent(tmp_path)

    with patch("codeforge.security.os.replace", side_effect=OSError("simulated replace failure")):
        result = agent.run_tool("write_file", {"path": "atomic.txt", "content": "replacement"})

    assert "simulated replace failure" in result
    assert target.read_text(encoding="utf-8") == "original"
    assert agent._last_tool_result_metadata["workspace_changed"] is False
    assert not list(tmp_path.glob(".atomic.txt.*.tmp"))


def test_patch_rechecks_unique_match_at_execution(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("one old", encoding="utf-8")
    agent = build_agent(tmp_path)
    original_approve = agent.approve

    def mutate_then_approve(name, args, assessment=None):
        target.write_text("old old", encoding="utf-8")
        return original_approve(name, args, assessment)

    with patch.object(agent, "approve", side_effect=mutate_then_approve):
        result = agent.run_tool("patch_file", {"path": "sample.txt", "old_text": "old", "new_text": "new"})

    assert "old_text must occur exactly once, found 2" in result
    assert target.read_text(encoding="utf-8") == "old old"
    assert agent._last_tool_result_metadata["tool_status"] == "error"


def test_docker_unavailable_fails_closed_without_host_fallback(tmp_path):
    agent = build_agent(tmp_path, backend="docker")
    with patch("codeforge.sandbox.shutil.which", return_value=None):
        result = agent.run_tool("run_shell", {"command": "echo must-not-run", "timeout": 3})

    assert "Docker CLI was not found" in result
    metadata = agent._last_tool_result_metadata
    assert metadata["tool_error_code"] == "sandbox_unavailable"
    assert metadata["sandbox"]["backend"] == "docker"
    assert metadata["sandbox"]["state"] == "unavailable"


def test_docker_invocation_has_mandatory_isolation_and_cleanup(tmp_path):
    config = ShellSandboxConfig(backend="docker", workspace_mode="read-write", image="trusted:test")
    sandbox = DockerSandbox(tmp_path, config, owner_id="codeforge-test")
    sandbox.status = SandboxStatus("docker", "ready", image_available=True)
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return 0, "ok\n", "", False, False

    with patch("codeforge.sandbox.shutil.which", return_value="docker"), patch(
        "codeforge.sandbox._run_bounded", side_effect=fake_run
    ), patch.object(sandbox, "_remove_container", return_value=True) as remove:
        result = sandbox.run("python -V", 5, {"PATH": "safe"})

    argv = captured["argv"]
    assert "--rm" in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert not argv[argv.index("--user") + 1].startswith("0:")
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges:true"
    assert "--pids-limit" in argv and "--cpus" in argv and "--memory" in argv
    mount = argv[argv.index("--volume") + 1]
    assert mount.endswith(":/workspace:rw")
    assert "docker.sock" not in " ".join(argv)
    assert result.backend == "docker"
    remove.assert_called_once()


def test_host_shell_timeout_sets_auditable_status_and_bounds_output(tmp_path):
    config = ShellSandboxConfig(backend="unsafe-host", output_limit=256)
    agent = build_agent(tmp_path, backend="unsafe-host")
    agent.shell_sandbox.config = config
    command = shell_command([sys.executable, "-c", "import time; print('x'*10000); time.sleep(5)"])

    result = agent.run_tool("run_shell", {"command": command, "timeout": 1})

    metadata = agent._last_tool_result_metadata
    assert "timed_out: true" in result
    assert metadata["timed_out"] is True
    assert metadata["tool_error_code"] == "tool_timeout"
    assert metadata["sandbox"]["output_truncated"] is True
    assert len(result) < 1200


def test_host_timeout_terminates_child_process_tree(tmp_path):
    marker = tmp_path / "orphan-marker.txt"
    script = tmp_path / "spawn_child.py"
    child = f"import time, pathlib; time.sleep(2); pathlib.Path({os.fspath(marker)!r}).write_text('orphan')"
    script.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    agent = build_agent(tmp_path, backend="unsafe-host")

    result = agent.run_tool("run_shell", {"command": shell_command([sys.executable, script]), "timeout": 1})
    time.sleep(2.5)

    assert "timed_out: true" in result
    assert not marker.exists()


def test_trace_records_approval_risk_denial_and_affected_files(tmp_path):
    outputs = (
        '<tool>{"name":"write_file","args":{"path":"trace.txt","content":"ok"}}</tool>',
        "<final>done</final>",
    )
    agent = build_agent(tmp_path, approval="auto", outputs=outputs)
    assert agent.ask("write it") == "done"

    events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    event = [item for item in events if item["event"] == "tool_executed"][-1]
    assert event["approval_result"] == "approved"
    assert event["safety_check_passed"] is True
    assert event["risk_level"] == "high"
    assert event["affected_paths"] == ["trace.txt"]
    assert event["denial_reason"] == ""


def test_project_env_configures_shell_after_workspace_env_is_loaded(tmp_path):
    workspace = WorkspaceContext.build(tmp_path, repo_root_override=tmp_path)
    (tmp_path / ".env").write_text(
        "CODEFORGE_SHELL_BACKEND=disabled\n"
        "CODEFORGE_DOCKER_IMAGE=trusted:local\n"
        "CODEFORGE_SHELL_WORKSPACE_MODE=read-write\n"
        "CODEFORGE_SHELL_CPUS=0.5\n",
        encoding="utf-8",
    )
    args = codeforge_cli.build_arg_parser().parse_args(["--cwd", os.fspath(tmp_path)])
    with patch.dict(os.environ, {}, clear=True), patch(
        "codeforge.cli.WorkspaceContext.build", return_value=workspace
    ), patch("codeforge.cli.OpenAICompatibleModelClient"):
        agent = codeforge_cli.build_agent(args)

    config = agent.shell_sandbox.config
    assert config.backend == "disabled"
    assert config.image == "trusted:local"
    assert config.workspace_mode == "read-write"
    assert config.cpus == 0.5


def test_windows_docker_desktop_start_is_bounded_and_then_checks_local_image(tmp_path):
    sandbox = DockerSandbox(
        tmp_path,
        ShellSandboxConfig(backend="docker", startup_timeout=2, auto_start_docker_desktop=True),
    )
    desktop = tmp_path / "Docker Desktop.exe"
    desktop.write_text("placeholder", encoding="utf-8")
    image_result = type("Result", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
    with patch("codeforge.sandbox.platform.system", return_value="Windows"), patch(
        "codeforge.sandbox.shutil.which", return_value="docker"
    ), patch.object(sandbox, "_probe_engine", side_effect=[(False, "not ready"), (True, "ready")]), patch.object(
        sandbox, "_find_docker_desktop", return_value=desktop
    ), patch("codeforge.sandbox.subprocess.Popen") as started, patch(
        "codeforge.sandbox.subprocess.run", return_value=image_result
    ):
        status = sandbox.initialize()

    assert status.state == "ready"
    assert status.docker_desktop_started is True
    assert status.image_available is True
    started.assert_called_once()


def test_close_only_removes_containers_tracked_by_this_instance(tmp_path):
    sandbox = DockerSandbox(tmp_path, ShellSandboxConfig(backend="docker"))
    sandbox._containers.update({"codeforge-owned-a", "codeforge-owned-b"})
    with patch.object(sandbox, "_remove_container", return_value=True) as remove:
        assert sandbox.close() is True

    assert {call.args[0] for call in remove.call_args_list} == {"codeforge-owned-a", "codeforge-owned-b"}
    assert sandbox._containers == set()


def test_approval_denial_is_written_to_trace(tmp_path):
    agent = build_agent(
        tmp_path,
        approval="never",
        outputs=(
            '<tool>{"name":"write_file","args":{"path":"denied.txt","content":"no"}}</tool>',
            "<final>denied</final>",
        ),
    )
    assert agent.ask("try a denied write") == "denied"
    events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    event = [item for item in events if item["event"] == "tool_executed"][-1]
    assert event["approval_result"] == "denied"
    assert event["safety_check_passed"] is True
    assert event["tool_error_code"] == "approval_denied"
    assert event["denial_reason"]


def test_real_docker_read_only_shell_when_engine_and_image_are_available(tmp_path):
    image = os.environ.get("CODEFORGE_TEST_DOCKER_IMAGE", "python:3.13-slim")
    sandbox = DockerSandbox(
        tmp_path,
        ShellSandboxConfig(
            backend="docker",
            image=image,
            workspace_mode="read-only",
            auto_start_docker_desktop=False,
        ),
    )
    status = sandbox.initialize()
    if status.state != "ready":
        pytest.skip(f"real Docker integration unavailable: {status.state}: {status.detail}")

    result = sandbox.run("printf codeforge", 10, {})
    assert result.exit_code == 0
    assert result.stdout == "codeforge"
    assert result.cleanup_succeeded is True
