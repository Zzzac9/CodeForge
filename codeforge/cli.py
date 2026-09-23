"""命令行入口。

这个模块负责把"用户怎么启动 codeforge"翻译成 runtime 能理解的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。

整体调用链：
  main() -> build_arg_parser() -> build_agent() -> CodeForge.ask()
  其中 build_agent() 内部装配 workspace / model client / session store，
  最终返回一个可运行的 CodeForge 实例。
"""

import argparse
import os
import shutil
import sys
import textwrap

from .config import load_project_env, provider_env
from .models import AnthropicCompatibleModelClient, OllamaModelClient, OpenAICompatibleModelClient
from .runtime import CodeForge, SessionStore
from .workspace import WorkspaceContext, middle

# 默认的敏感环境变量名单。
# 这些变量名会在 trace / report 输出中被自动脱敏（redact），
# 防止 API key、token 等凭据意外泄露到日志或会话文件里。
# 用户可通过 --secret-env-name 追加，或通过 CODEFORGE_SECRET_ENV_NAMES 环境变量扩展。
DEFAULT_SECRET_ENV_NAMES = (
    "CODEFORGE_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "CODEFORGE_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CODEFORGE_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "CODEFORGE_RIGHT_CODES_API_KEY",
    "RIGHT_CODES_API_KEY",
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


# ---- 各 provider 的默认模型与 API 地址 ----
# 这些常量仅在用户未通过 --model / --base-url / 环境变量显式指定时生效。
DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OPENAI_MODEL = "gpt-5.4"
DEFAULT_OPENAI_BASE_URL = "https://www.right.codes/codex/v1"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_ANTHROPIC_BASE_URL = "https://www.right.codes/claude/v1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"
# 旧版项目名称为 mini-coding-agent，保留此环境变量名以兼容旧配置。
LEGACY_SECRET_ENV_NAMES_VAR = "MINI_CODING_AGENT_SECRET_ENV_NAMES"
SECRET_ENV_NAMES_VAR = "CODEFORGE_SECRET_ENV_NAMES"


def _effective_model(args, provider):
    """根据优先级解析最终使用的模型名称。

    模型选择优先级（由高到低）：
    1. 用户显式传入 --model 参数
    2. provider 对应的环境变量（如 CODEFORGE_OPENAI_MODEL）
    3. 代码里的默认值（如 DEFAULT_OPENAI_MODEL）

    Args:
        args: argparse 解析后的命名空间。
        provider: 当前 provider 标识，如 "openai"、"anthropic"。

    Returns:
        str: 最终确定的模型名称。
    """
    explicit_model = getattr(args, "model", None)
    if explicit_model:
        return explicit_model
    if provider == "openai":
        model = provider_env("CODEFORGE_OPENAI_MODEL", ("OPENAI_MODEL",))
        if model:
            return model
        return DEFAULT_OPENAI_MODEL
    if provider == "anthropic":
        model = provider_env("CODEFORGE_ANTHROPIC_MODEL", ("ANTHROPIC_MODEL",))
        if model:
            return model
        return DEFAULT_ANTHROPIC_MODEL
    if provider == "deepseek":
        model = provider_env("CODEFORGE_DEEPSEEK_MODEL", ("DEEPSEEK_MODEL",))
        if model:
            return model
        return DEFAULT_DEEPSEEK_MODEL
    # 走到这里说明 provider 未被明确匹配（通常是 ollama），
    # 直接回退到 Ollama 默认模型。
    return DEFAULT_OLLAMA_MODEL


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
    """根据 CLI 参数创建对应的模型客户端实例。

    CLI 只负责把 provider 选择翻译成具体 client。
    真正的提示词格式、缓存支持、HTTP 协议差异，都封装在 models.py 里。

    每个 provider 分支依次解析 model / base_url / api_key，然后构造对应 client：
    - openai -> OpenAICompatibleModelClient（OpenAI 兼容协议）
    - anthropic -> AnthropicCompatibleModelClient（Anthropic Messages API 兼容协议）
    - deepseek -> AnthropicCompatibleModelClient（DeepSeek 也走 Anthropic 兼容协议）
    - 其他 -> OllamaModelClient（本地 Ollama 服务）
    """
    provider = getattr(args, "provider", "openai")

    if provider == "openai":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("CODEFORGE_OPENAI_API_BASE", ("OPENAI_API_BASE",), DEFAULT_OPENAI_BASE_URL)
        api_key = provider_env("CODEFORGE_OPENAI_API_KEY", ("OPENAI_API_KEY",))
        return OpenAICompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )
    if provider == "anthropic":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("CODEFORGE_ANTHROPIC_API_BASE", ("ANTHROPIC_API_BASE",), DEFAULT_ANTHROPIC_BASE_URL)
        # Anthropic provider 的 API key 有多个回退路径：
        # 优先 CODEFORGE_ANTHROPIC_API_KEY，其次 ANTHROPIC_API_KEY，
        # 再回退到 RIGHT_CODES / OPENAI 的 key（兼容共用网关场景）。
        api_key = provider_env(
            "CODEFORGE_ANTHROPIC_API_KEY",
            ("ANTHROPIC_API_KEY", "CODEFORGE_RIGHT_CODES_API_KEY", "RIGHT_CODES_API_KEY", "CODEFORGE_OPENAI_API_KEY", "OPENAI_API_KEY"),
        )
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )
    if provider == "deepseek":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("CODEFORGE_DEEPSEEK_API_BASE", ("DEEPSEEK_API_BASE",), DEFAULT_DEEPSEEK_BASE_URL)
        api_key = provider_env("CODEFORGE_DEEPSEEK_API_KEY", ("DEEPSEEK_API_KEY",))
        # DeepSeek 使用 Anthropic 兼容的 Messages API 协议。
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )

    # 默认分支：ollama 或未识别的 provider，走本地 Ollama 服务。
    model = _effective_model(args, provider)
    host = getattr(args, "host", DEFAULT_OLLAMA_HOST)
    return OllamaModelClient(
        model=model,
        host=host,
        temperature=args.temperature,
        top_p=args.top_p,
        timeout=args.ollama_timeout,
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
    5. 根据 provider 构建对应的模型客户端
    6. 若指定了 --resume，从已有 session 恢复；否则新建 CodeForge 实例
    """
    # 这里是 CLI 到 runtime 的装配点：
    # 先采集工作区快照和加载项目级环境，再整理 secret 名单、模型后端和 session。
    workspace = WorkspaceContext.build(args.cwd)
    load_project_env(workspace.repo_root)
    configured_secret_names = _configured_secret_names(args)
    store = SessionStore(workspace.repo_root + "/.codeforge/sessions")
    model = _build_model_client(args)
    session_id = args.resume
    if session_id == "latest":
        # "latest" 表示自动选择最近的 session 文件恢复。
        session_id = store.latest()
    if session_id:
        # 恢复已有 session：保留历史对话和 working memory。
        return CodeForge.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            secret_env_names=configured_secret_names,
        )
    # 全新启动：创建空白 session。
    return CodeForge(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=configured_secret_names,
    )


def build_arg_parser():
    """构建命令行参数解析器。

    所有 CLI 参数在此集中定义，包括位置参数 prompt 以及
    provider、model、session、approval 等可选参数。
    """
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Minimal coding agent for Ollama, OpenAI-compatible, Anthropic-compatible, or DeepSeek models.",
    )
    # 位置参数：如果不传则进入交互 REPL 模式。
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    # --provider 决定走哪个模型后端，默认 openai。
    parser.add_argument("--provider", choices=("ollama", "openai", "anthropic", "deepseek"), default="deepseek", help="Model backend to use.")
    parser.add_argument(
        "--model",
        default=None,
        help="Model name override. Defaults to qwen3.5:4b for Ollama, CODEFORGE_OPENAI_MODEL for openai, CODEFORGE_ANTHROPIC_MODEL for anthropic, and CODEFORGE_DEEPSEEK_MODEL for deepseek when set.",
    )
    parser.add_argument("--host", default=DEFAULT_OLLAMA_HOST, help="Ollama server URL.")
    # --base-url 覆盖默认的 API 网关地址，适用于 openai / anthropic / deepseek。
    parser.add_argument("--base-url", default=None, help="Provider API base URL for openai, anthropic, or deepseek.")
    parser.add_argument("--ollama-timeout", type=int, default=300, help="Ollama request timeout in seconds.")
    parser.add_argument("--openai-timeout", type=int, default=300, help="OpenAI-compatible request timeout in seconds.")
    # --resume 允许恢复之前的会话；传 "latest" 则自动选择最近的 session。
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    # --approval 控制危险工具的执行策略：ask=每次询问, auto=自动批准, never=永不执行。
    parser.add_argument("--approval", choices=("ask", "auto", "never"), default="ask", help="Approval policy for risky tools.")
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to treat as secrets for trace/report redaction.",
    )
    # --max-steps 限制单次请求中模型与工具之间的最大交互轮数。
    parser.add_argument("--max-steps", type=int, default=6, help="Maximum tool/model iterations per request.")
    # --max-new-tokens 限制模型每次响应的最大输出 token 数。
    parser.add_argument("--max-new-tokens", type=int, default=512, help="Maximum model output tokens per step.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature sent to Ollama.")
    parser.add_argument("--top-p", type=float, default=0.9, help="Top-p sampling value sent to Ollama.")
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
    model = getattr(agent.model_client, "model", getattr(args, "model", DEFAULT_OLLAMA_MODEL))
    host = getattr(agent.model_client, "host", getattr(agent.model_client, "base_url", getattr(args, "host", DEFAULT_OLLAMA_HOST)))
    print(build_welcome(agent, model=model, host=host))

    if args.prompt:
        # one-shot 模式：只跑一次 ask，不进入 REPL 循环。
        # 将位置参数拼接为完整的 prompt 字符串。
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            try:
                print(agent.ask(prompt))
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
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
            print(agent.ask(user_input))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
