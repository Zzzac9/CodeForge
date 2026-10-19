"""命令行入口。

这个模块负责把"用户怎么启动 codeforge"翻译成 runtime 能理解的对象：
解析参数、构建统一模型接口、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。

整体调用链：
  main() -> build_arg_parser() -> build_agent() -> CodeForge.ask()
  其中 build_agent() 内部装配 workspace / model client / session store，
  最终返回一个可运行的 CodeForge 实例。
"""

import argparse
import atexit
import os
import shutil
import sys
import textwrap

from .config import load_project_env, provider_bool, provider_env, provider_float, provider_int
from .models import OpenAICompatibleModelClient
from .runtime import CodeForge, SessionStore
from .sandbox import ShellSandboxConfig
from .workspace import WorkspaceContext, middle

# 默认的敏感环境变量名单。
# 这些变量名会在 trace / report 输出中被自动脱敏（redact），
# 防止 API key、token 等凭据意外泄露到日志或会话文件里。
# 用户可通过 --secret-env-name 追加，或通过 CODEFORGE_SECRET_ENV_NAMES 环境变量扩展。
DEFAULT_SECRET_ENV_NAMES = (
    "CODEFORGE_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "CODEFORGE_OPENAI_API_KEY",
    "CODEFORGE_DEEPSEEK_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)

# 欢迎界面的 ASCII 猫头鹰图案，启动时渲染在终端顶部。
WELCOME_ART = (
    "        /\\___/\\\\",
    "       (  o o  )",
    "       /   ^   \\\\",
    "      /|       |\\\\",
)
WELCOME_NAME = "codeforge"
WELCOME_SUBTITLE = "local coding agent"
WELCOME_STATUS = "calm shell, ready for work"
# /help 命令显示的帮助文本，列出所有可用的交互式命令。
HELP_DETAILS = textwrap.dedent(
    """\
    Commands:
    /help    Show this help message.
    /memory  Show the agent's distilled working memory.
    /session Show the path to the saved session file.
    /reset   Clear the current session history and memory.
    /exit    Exit the agent.
    """
).strip()


class ConsoleProgressRenderer:
    """Render runtime steps and final model content without exposing tool payloads."""

    _CLOSE_FINAL = "</final>"

    def __init__(self, output=None, status_output=None):
        self.output = output or sys.stdout
        self.status_output = status_output or sys.stderr
        self._mode = "idle"
        self._probe = ""
        self._pending = ""
        self.answer_streamed = False

    def _status(self, text):
        print(f"[agent] {text}", file=self.status_output, flush=True)

    def _write_final(self, text):
        if not text:
            return
        self.answer_streamed = True
        self.output.write(text)
        self.output.flush()

    def _feed_final(self, text):
        self._pending += text
        hold = len(self._CLOSE_FINAL)
        if len(self._pending) > hold:
            self._write_final(self._pending[:-hold])
            self._pending = self._pending[-hold:]

    def _feed_delta(self, text):
        if self._mode == "tool":
            return
        if self._mode == "final":
            self._feed_final(text)
            return

        self._probe += text
        stripped = self._probe.lstrip()
        if stripped.startswith("<tool"):
            self._mode = "tool"
            self._probe = ""
            return
        if stripped.startswith("<final>"):
            self._mode = "final"
            content = stripped[len("<final>"):]
            self._probe = ""
            self._feed_final(content)
            return
        if "<tool".startswith(stripped) or "<final>".startswith(stripped):
            return
        self._mode = "final"
        content = self._probe
        self._probe = ""
        self._feed_final(content)

    def _finish_model(self, kind):
        if kind != "final":
            self._mode = "idle"
            self._probe = ""
            self._pending = ""
            return
        if self._mode == "undecided" and self._probe:
            self._mode = "final"
            self._feed_final(self._probe)
            self._probe = ""
        tail = self._pending
        if tail.endswith(self._CLOSE_FINAL):
            tail = tail[:-len(self._CLOSE_FINAL)]
        self._write_final(tail)
        self._pending = ""
        if self.answer_streamed:
            self.output.write("\n")
            self.output.flush()
        self._mode = "idle"

    @staticmethod
    def _tool_description(name, args):
        path = str(args.get("path", "."))
        if name == "read_file":
            return f"读取文件 {path}"
        if name == "list_files":
            return f"列出目录 {path}"
        if name == "search":
            return f"在 {path} 中搜索 {args.get('pattern', '')!r}"
        if name == "write_file":
            return f"写入文件 {path}"
        if name == "patch_file":
            return f"修改文件 {path}"
        if name == "run_shell":
            command = str(args.get("command", ""))
            if len(command) > 100:
                command = command[:97] + "..."
            return f"运行 Shell：{command}"
        if name == "delegate":
            return "执行只读子任务"
        return f"调用工具 {name}"

    def __call__(self, event):
        kind = event.get("event")
        if kind == "model_started":
            self._mode = "undecided"
            self._probe = ""
            self._pending = ""
            self._status(f"正在思考（第 {event.get('attempt', 1)} 轮）…")
        elif kind == "model_delta":
            self._feed_delta(str(event.get("text", "")))
        elif kind == "model_finished":
            self._finish_model(str(event.get("kind", "")))
        elif kind == "model_failed":
            self._finish_model("error")
            self._status(f"模型响应失败：{event.get('error_code', 'model_request_failed')}")
        elif kind == "model_retry_scheduled":
            self._status(
                f"安全重试一次（max_tokens={event.get('next_max_tokens', '?')}）…"
            )
        elif kind == "tool_started":
            description = self._tool_description(
                str(event.get("name", "")), dict(event.get("args", {}) or {})
            )
            self._status(f"步骤 {event.get('step', '?')}：{description}")
        elif kind == "tool_finished":
            self._status(
                f"{event.get('name', 'tool')} {event.get('status', 'completed')} "
                f"({event.get('duration_ms', 0)} ms)"
            )

    def finish(self, answer):
        if not self.answer_streamed:
            print(answer, file=self.output)


def _run_request(agent, prompt):
    renderer = ConsoleProgressRenderer()
    answer = agent.ask(prompt, progress_callback=renderer)
    renderer.finish(answer)


# ---- 统一 OpenAI-compatible Chat Completions 模型配置 ----
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_API_BASE_URL = "https://api.deepseek.com"
DEFAULT_API_TIMEOUT = 300
DEFAULT_MAX_NEW_TOKENS = 4096
DEFAULT_PROMPT_CACHE_MODE = "auto"
DEFAULT_SHELL_BACKEND = "docker"
DEFAULT_DOCKER_IMAGE = "python:3.13-slim"
DEFAULT_SHELL_WORKSPACE_MODE = "read-only"
LEGACY_SECRET_ENV_NAMES_VAR = "MINI_CODING_AGENT_SECRET_ENV_NAMES"
SECRET_ENV_NAMES_VAR = "CODEFORGE_SECRET_ENV_NAMES"


def _effective_model(args):
    explicit_model = getattr(args, "model", None)
    if explicit_model:
        return explicit_model
    return provider_env(
        "CODEFORGE_MODEL",
        ("CODEFORGE_DEEPSEEK_MODEL", "DEEPSEEK_MODEL", "CODEFORGE_OPENAI_MODEL", "OPENAI_MODEL"),
        DEFAULT_MODEL,
    )


def _configured_secret_names(args):
    """汇总所有需要脱敏的环境变量名。

    来源有三：
    - DEFAULT_SECRET_ENV_NAMES：代码内置的默认名单。
    - --secret-env-name CLI 参数：用户显式追加的变量名。
    - CODEFORGE_SECRET_ENV_NAMES 环境变量（兼容旧名 MINI_CODING_AGENT_SECRET_ENV_NAMES）：
      以逗号分隔的变量名列表。

    所有名称统一转为大写，最终去重排序后返回。
    """
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    # 从 CLI 追加
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    # 从环境变量读取（优先新名称，回退到旧名称）
    extra_names = os.environ.get(SECRET_ENV_NAMES_VAR, "")
    if not extra_names.strip():
        extra_names = os.environ.get(LEGACY_SECRET_ENV_NAMES_VAR, "")
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper()
            for item in extra_names.split(",")
            if item.strip()
        )
    return sorted(configured_secret_names)


def _build_model_client(args):
    """Build the single OpenAI-compatible Chat Completions model client."""
    model = _effective_model(args)
    base_url = getattr(args, "base_url", None) or provider_env(
        "CODEFORGE_API_BASE",
        ("CODEFORGE_DEEPSEEK_API_BASE", "DEEPSEEK_API_BASE", "CODEFORGE_OPENAI_API_BASE", "OPENAI_API_BASE"),
        DEFAULT_API_BASE_URL,
    )
    api_key = provider_env(
        "CODEFORGE_API_KEY",
        ("CODEFORGE_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY", "CODEFORGE_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    cache_mode = getattr(args, "prompt_cache_mode", None) or provider_env(
        "CODEFORGE_PROMPT_CACHE_MODE", (), DEFAULT_PROMPT_CACHE_MODE
    )
    return OpenAICompatibleModelClient(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=args.temperature,
        timeout=getattr(args, "api_timeout", DEFAULT_API_TIMEOUT),
        prompt_cache_mode=cache_mode,
    )


def build_welcome(agent, model, host):
    """构建启动欢迎界面。

    根据终端宽度自适应布局，在顶部居中渲染猫头鹰 ASCII art，
    下方以双列形式显示当前 workspace、model、branch、session 等信息。

    布局结构（示意）：
    +====================================+
    |          /\\___/\\                   |
    |         (  o o  )                  |
    |         /   ^   \\                  |
    |        /|       |\\                 |
    |              codeforge                  |
    |        local coding agent          |
    |       calm shell, ready for work   |
    |------------------------------------|
    |                                    |
    | WORKSPACE  /path/to/project        |
    | MODEL     xxx    BRANCH    main    |
    | APPROVAL  ask    SESSION   abc123  |
    |                                    |
    +====================================+
    """
    # 宽度自适应：最小 68 列，最大 84 列，取终端实际列数居中。
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4        # 边框内部可用宽度
    gap = 3                  # 双列之间的间隔
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width

    def row(text):
        """单行文本，左对齐放置在边框内。"""
        body = middle(text, width - 4)
        return f"| {body.ljust(width - 4)} |"

    def divider(char="-"):
        """分隔线，用指定字符填满整行宽度。"""
        return "+" + char * (width - 2) + "+"

    def center(text):
        """文本居中放置在边框内。"""
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    def cell(label, value, size):
        """双列布局中的单个单元格：标签（9字符宽）+ 值，截断到指定宽度。"""
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        """双列布局中的一行：左列 + 间隔 + 右列。"""
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    # 自上而下拼接各段
    line = divider("=")
    rows = [center(text) for text in WELCOME_ART]
    rows.extend(
        [
            center(WELCOME_NAME),
            center(WELCOME_SUBTITLE),
            center(WELCOME_STATUS),
            divider("-"),
            row(""),
            row("WORKSPACE  " + middle(agent.workspace.cwd, inner - 11)),
            pair("MODEL", model, "BRANCH", agent.workspace.branch),
            pair("APPROVAL", agent.approval_policy, "SESSION", agent.session["id"]),
            row(
                "SHELL      "
                + middle(
                    f"{agent.shell_startup_status.get('backend', 'unknown')}:"
                    f"{agent.shell_startup_status.get('state', 'unknown')}",
                    inner - 11,
                )
            ),
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])


def build_agent(args):
    """根据 CLI 参数装配出一个可运行的 CodeForge 实例。

    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把"启动参数"翻译成"agent 运行现场"。

    输入 / 输出：
    - 输入：argparse 解析后的 args
    - 输出：一个新的 CodeForge，或一个从旧 session 恢复出来的 CodeForge

    在 agent 链路里的位置：
    它是整个程序启动链路里最靠近 runtime 的装配点。main() 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 ask()。

    装配顺序：
    1. 构建 WorkspaceContext（采集 cwd、git branch 等工作区快照）
    2. 加载项目级 .env 覆盖（load_project_env）
    3. 整理 secret 环境变量名单
    4. 创建 SessionStore（持久化目录在 .codeforge/sessions）
    5. 根据统一的 OpenAI-compatible 配置构建模型客户端
    6. 若指定了 --resume，从已有 session 恢复；否则新建 CodeForge 实例
    """
    # 这里是 CLI 到 runtime 的装配点：
    # 先采集工作区快照和加载项目级环境，再整理 secret 名单、模型后端和 session。
    workspace = WorkspaceContext.build(args.cwd)
    load_project_env(workspace.repo_root)
    configured_secret_names = _configured_secret_names(args)
    store = SessionStore(workspace.repo_root + "/.codeforge/sessions")
    model = _build_model_client(args)
    shell_config = ShellSandboxConfig(
        backend=args.shell_backend or provider_env("CODEFORGE_SHELL_BACKEND", (), DEFAULT_SHELL_BACKEND),
        image=args.docker_image or provider_env("CODEFORGE_DOCKER_IMAGE", (), DEFAULT_DOCKER_IMAGE),
        workspace_mode=args.shell_workspace_mode or provider_env(
            "CODEFORGE_SHELL_WORKSPACE_MODE", (), DEFAULT_SHELL_WORKSPACE_MODE
        ),
        network="none",
        cpus=args.shell_cpus if args.shell_cpus is not None else provider_float("CODEFORGE_SHELL_CPUS", (), 1.0),
        memory=args.shell_memory or provider_env("CODEFORGE_SHELL_MEMORY", (), "512m"),
        pids_limit=args.shell_pids_limit if args.shell_pids_limit is not None else provider_int("CODEFORGE_SHELL_PIDS_LIMIT", (), 128),
        output_limit=args.shell_output_limit if args.shell_output_limit is not None else provider_int("CODEFORGE_SHELL_OUTPUT_LIMIT", (), 65536),
        startup_timeout=(
            args.docker_startup_timeout
            if args.docker_startup_timeout is not None
            else provider_int("CODEFORGE_DOCKER_STARTUP_TIMEOUT", (), 30)
        ),
        auto_start_docker_desktop=(
            args.docker_auto_start
            if args.docker_auto_start is not None
            else provider_bool("CODEFORGE_DOCKER_AUTO_START", (), True)
        ),
    )
    session_id = args.resume
    if session_id == "latest":
        # "latest" 表示自动选择最近的 session 文件恢复。
        session_id = store.latest()
    if session_id:
        # 恢复已有 session：保留历史对话和 working memory。
        agent = CodeForge.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            secret_env_names=configured_secret_names,
            shell_sandbox_config=shell_config,
            initialize_shell=True,
        )
        atexit.register(agent.close)
        return agent
    # 全新启动：创建空白 session。
    agent = CodeForge(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=configured_secret_names,
        shell_sandbox_config=shell_config,
        initialize_shell=True,
    )
    atexit.register(agent.close)
    return agent


def build_arg_parser():
    """Build CLI arguments for the unified OpenAI-compatible model path."""
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Local coding agent using one OpenAI-compatible Chat Completions model protocol.",
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument("--model", default=None, help="Model name override.")
    parser.add_argument(
        "--base-url",
        default=None,
        help="OpenAI-compatible API base URL, for example https://api.deepseek.com.",
    )
    parser.add_argument(
        "--api-timeout",
        type=int,
        default=DEFAULT_API_TIMEOUT,
        help="Model API request timeout in seconds.",
    )
    parser.add_argument(
        "--prompt-cache-mode",
        choices=("auto", "explicit", "automatic", "observe", "off"),
        default=None,
        help="Prompt cache strategy. auto detects OpenAI/DeepSeek behavior.",
    )
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    parser.add_argument(
        "--approval",
        choices=("ask", "auto", "never"),
        default="ask",
        help="Approval policy for risky tools.",
    )
    parser.add_argument(
        "--shell-backend",
        choices=("docker", "disabled", "unsafe-host"),
        default=None,
        help="Shell backend. unsafe-host is explicit non-sandboxed execution and is never a fallback.",
    )
    parser.add_argument(
        "--docker-image",
        default=None,
        help="Trusted local image used by the Docker shell backend; CodeForge never pulls it implicitly.",
    )
    parser.add_argument(
        "--shell-workspace-mode",
        choices=("read-only", "read-write"),
        default=None,
        help="Docker workspace mount mode.",
    )
    parser.add_argument("--shell-cpus", type=float, default=None, help="Docker CPU limit.")
    parser.add_argument("--shell-memory", default=None, help="Docker memory limit.")
    parser.add_argument("--shell-pids-limit", type=int, default=None, help="Docker process limit.")
    parser.add_argument("--shell-output-limit", type=int, default=None, help="Per-stream shell output byte limit.")
    parser.add_argument("--docker-startup-timeout", type=int, default=None, help="Docker Desktop startup/readiness timeout.")
    parser.add_argument(
        "--docker-auto-start",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="On Windows, start Docker Desktop when installed but not ready.",
    )
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to redact from trace/report.",
    )
    parser.add_argument("--max-steps", type=int, default=6, help="Maximum tool/model iterations per request.")
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=DEFAULT_MAX_NEW_TOKENS,
        help="Maximum model output tokens per step.",
    )
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature.")
    return parser


def main(argv=None):
    """codeforge 的主入口函数。

    启动流程：
    1. 解析命令行参数
    2. 装配 agent（包括 workspace、model client、session 等）
    3. 打印欢迎界面
    4. 如果传入了 prompt，进入 one-shot 模式（执行一次后退出）
    5. 否则进入交互式 REPL 循环，持续读取用户输入直到 /exit 或 Ctrl+C
    """
    args = build_arg_parser().parse_args(argv)
    agent = build_agent(args)

    # 从已构建的 model_client 或 args 中提取 model 和 host 用于欢迎界面显示。
    model = getattr(agent.model_client, "model", getattr(args, "model", DEFAULT_MODEL))
    host = getattr(agent.model_client, "base_url", getattr(args, "base_url", DEFAULT_API_BASE_URL))
    print(build_welcome(agent, model=model, host=host))
    shell_status = agent.shell_startup_status
    if shell_status.get("backend") == "docker" and shell_status.get("state") != "ready":
        detail = shell_status.get("detail") or "Docker shell backend is not ready"
        print(f"Docker shell unavailable (no host fallback): {detail}", file=sys.stderr)

    if args.prompt:
        # one-shot 模式：只跑一次 ask，不进入 REPL 循环。
        # 将位置参数拼接为完整的 prompt 字符串。
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            try:
                _run_request(agent, prompt)
            except RuntimeError as exc:
                print(agent.redact_text(str(exc)), file=sys.stderr)
                return 1
        return 0

    # 交互式 REPL 循环。
    while True:
        # 每次读取一条用户输入，交给同一个 agent，
        # 因此 session history 和 working memory 会跨轮延续。
        try:
            user_input = input("\ncodeforge> ").strip()
        except (EOFError, KeyboardInterrupt):
            # Ctrl+D 或 Ctrl+C 优雅退出。
            print("")
            return 0

        # 空输入直接跳过，重新显示提示符。
        if not user_input:
            continue
        # 内置命令处理：不进入 agent.ask()，直接在当前进程处理。
        if user_input in {"/exit", "/quit"}:
            return 0
        if user_input == "/help":
            print(HELP_DETAILS)
            continue
        if user_input == "/memory":
            # 输出 agent 当前提炼的 working memory。
            print(agent.memory_text())
            continue
        if user_input == "/session":
            # 显示当前 session 文件的磁盘路径。
            print(agent.session_path)
            continue
        if user_input == "/reset":
            # 清空当前 session 的历史和 memory，从空白状态重新开始。
            agent.reset()
            print("session reset")
            continue

        # 非内置命令：交给 agent 的 ask() 处理（模型推理 + 工具调用）。
        print()
        try:
            _run_request(agent, user_input)
        except RuntimeError as exc:
            print(agent.redact_text(str(exc)), file=sys.stderr)
