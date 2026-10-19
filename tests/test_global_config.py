import os
from unittest.mock import patch

from codeforge import cli
from codeforge.config import load_project_env
from codeforge.workspace import WorkspaceContext


def test_installation_env_is_loaded_before_target_project_env(tmp_path):
    install_root = tmp_path / "install"
    target = tmp_path / "other-project"
    install_root.mkdir()
    target.mkdir()
    install_env = install_root / ".env"
    install_env.write_text(
        "CODEFORGE_MODEL=shared-model\n"
        "CODEFORGE_API_BASE=https://shared.example\n"
        "CODEFORGE_API_KEY=shared-secret\n",
        encoding="utf-8",
    )
    (target / ".env").write_text(
        "CODEFORGE_MODEL=project-model\n"
        "OPENAI_API_KEY=project-secret\n",
        encoding="utf-8",
    )

    with patch.dict(
        os.environ,
        {
            "CODEFORGE_MODEL": "system-model",
            "CODEFORGE_API_BASE": "https://system.example",
            "SYSTEM_ONLY": "from-system",
        },
        clear=True,
    ), patch("codeforge.config.find_codeforge_env", return_value=install_env):
        load_project_env(target)
        assert os.environ["CODEFORGE_MODEL"] == "project-model"
        assert os.environ["CODEFORGE_API_BASE"] == "https://shared.example"
        assert os.environ["CODEFORGE_API_KEY"] == "project-secret"
        assert os.environ["SYSTEM_ONLY"] == "from-system"


def test_cross_project_build_uses_installation_model_config_and_local_state(tmp_path):
    install_root = tmp_path / "codeforge-install"
    target = tmp_path / "unrelated-workspace"
    install_root.mkdir()
    target.mkdir()
    (target / "README.md").write_text("target\n", encoding="utf-8")
    install_env = install_root / ".env"
    install_env.write_text(
        "CODEFORGE_MODEL=shared-model\n"
        "CODEFORGE_API_BASE=https://shared.example/v1\n"
        "CODEFORGE_API_KEY=shared-secret\n",
        encoding="utf-8",
    )
    args = cli.build_arg_parser().parse_args(
        ["--cwd", os.fspath(target), "--shell-backend", "disabled"]
    )

    target_workspace = WorkspaceContext.build(target, repo_root_override=target)
    with patch.dict(os.environ, {}, clear=True), patch(
        "codeforge.config.find_codeforge_env", return_value=install_env
    ), patch(
        "codeforge.config.find_project_env", return_value=None
    ), patch(
        "codeforge.cli.WorkspaceContext.build", return_value=target_workspace
    ), patch("codeforge.cli.OpenAICompatibleModelClient") as mock_client:
        agent = cli.build_agent(args)

    kwargs = mock_client.call_args.kwargs
    assert kwargs["model"] == "shared-model"
    assert kwargs["base_url"] == "https://shared.example/v1"
    assert kwargs["api_key"] == "shared-secret"
    assert agent.session_path.is_relative_to(target / ".codeforge")
    assert agent.run_store.root.is_relative_to(target / ".codeforge")
    assert not (install_root / ".codeforge").exists()


def test_cli_model_argument_overrides_project_installation_and_system(tmp_path):
    install_env = tmp_path / "install.env"
    install_env.write_text("CODEFORGE_MODEL=shared-model\n", encoding="utf-8")
    target = tmp_path / "project"
    target.mkdir()
    (target / ".env").write_text("CODEFORGE_MODEL=project-model\n", encoding="utf-8")
    args = cli.build_arg_parser().parse_args(
        [
            "--cwd",
            os.fspath(target),
            "--model",
            "cli-model",
            "--shell-backend",
            "disabled",
        ]
    )

    with patch.dict(os.environ, {"CODEFORGE_MODEL": "system-model"}, clear=True), patch(
        "codeforge.config.find_codeforge_env", return_value=install_env
    ), patch("codeforge.cli.OpenAICompatibleModelClient") as mock_client:
        cli.build_agent(args)

    assert mock_client.call_args.kwargs["model"] == "cli-model"
