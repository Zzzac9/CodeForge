"""工作区快照工具。

这个模块负责在 agent 按需读文件之前，先给它一份便宜的"仓库第一印象"。
这份快照刻意保持小而稳定：主要包含 Git 事实和少量白名单项目文档。
"""

import subprocess
import textwrap
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

# 单次工具调用（如读文件）的输出上限，超过则尾部截断。
# 目的是防止大文件撑爆模型上下文窗口。
MAX_TOOL_OUTPUT = 4000
# 历史对话上下文的上限，超过后旧消息会被压缩或丢弃。
MAX_HISTORY = 12000
# 这些文件最可能直接影响 agent 的行动方式。
# 我们不会预加载整个仓库，只会先给模型一小份"导航包"。
DOC_NAMES = ("AGENTS.md", "README.md", "pyproject.toml", "package.json")
# 文件扫描时跳过的目录集合。这些目录要么是 VCS 元数据，要么是
# 构建/缓存产物，扫描它们只会增加噪声而不会带来有用信息。
IGNORED_PATH_NAMES = {".git", ".codeforge", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv"}


def now():
    return datetime.now(timezone.utc).isoformat()


def clip(text, limit=MAX_TOOL_OUTPUT):
    """尾部截断：保留开头，超出部分替换为截断标记。

    适用于多行长文本（如 git status 输出、文件内容）。
    保留头部可以让模型看到上下文开头，判断是否需要进一步读取。
    """
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def middle(text, limit):
    """中间截断：保留头尾，中间用 "..." 替换。

    适用于单行长字符串（如命令输出）。与 clip() 不同，这里先压成一行、
    再保留头尾，因为单行文本的开头和结尾通常都承载关键信息
    （如路径前缀 + 文件名），只保留开头会丢失尾部。
    """
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    left = (limit - 3) // 2
    right = limit - 3 - left
    return text[:left] + "..." + text[-right:]


class WorkspaceContext:
    """仓库工作区的轻量快照，作为 prompt prefix 的数据来源。

    各字段含义：
    - cwd:        当前工作目录（agent 启动时所在路径）
    - repo_root:  Git 仓库根目录
    - branch:     当前分支名（"-" 表示 detached HEAD）
    - default_branch: 远程仓库的默认分支（如 main/master）
    - status:     git status --short 的缓存结果
    - recent_commits: 最近 5 条 git log（--oneline 格式）
    - project_docs: 白名单项目文档的内容快照（{相对路径: 截断内容}）
    """
    def __init__(self, cwd, repo_root, branch, default_branch, status, recent_commits, project_docs):
        self.cwd = cwd
        self.repo_root = repo_root
        self.branch = branch
        self.default_branch = default_branch
        self.status = status
        self.recent_commits = recent_commits
        self.project_docs = project_docs

    @classmethod
    def build(cls, cwd, repo_root_override=None):
        """从当前工作目录收集 Git 和项目文档信息，构造 WorkspaceContext。

        repo_root_override 允许调用方显式指定仓库根目录（如从配置读取），
        避免在非 git 目录或特殊场景下 git rev-parse 失败。
        """
        cwd = Path(cwd).resolve()

        def git(args, fallback=""):
            # 用 timeout=5 防止 git 命令在异常仓库中卡死；
            # check=True 确保非零退出码被捕获（如 detached HEAD 时某些命令会失败）；
            # 任何异常都返回 fallback，保证 build() 不会因单个 git 命令失败而整体崩溃。
            try:
                result = subprocess.run(
                    ["git", *args],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                return result.stdout.strip() or fallback
            except Exception:
                return fallback

        repo_root = (
            Path(repo_root_override).resolve()
            if repo_root_override is not None
            else Path(git(["rev-parse", "--show-toplevel"], str(cwd))).resolve()
        )
        docs = {}
        # 同时扫描 repo_root 和 cwd，这样在子目录启动时也能看到本地文档；
        # 但用相对路径做 key，避免同一份文档被重复收集。
        for base in (repo_root, cwd):
            for name in DOC_NAMES:
                path = base / name
                if not path.exists():
                    continue
                key = str(path.relative_to(repo_root))
                if key in docs:
                    continue
                docs[key] = clip(path.read_text(encoding="utf-8", errors="replace"), 1200)

        return cls(
            cwd=str(cwd),
            repo_root=str(repo_root),
            branch=git(["branch", "--show-current"], "-") or "-",
            default_branch=(
                # git symbolic-ref refs/remotes/origin/HEAD 返回如 "origin/main"。
                # lambda 剥离 "origin/" 前缀得到纯分支名；若不是 origin/ 开头（如
                # 直接返回 "main"），则原样保留。
                # 若命令失败则回退到 "origin/main"。
                lambda branch: branch[len("origin/") :] if branch.startswith("origin/") else branch
            )(git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], "origin/main") or "origin/main"),
            status=clip(git(["status", "--short"], "clean") or "clean", 1500),
            recent_commits=[line for line in git(["log", "--oneline", "-5"]).splitlines() if line],
            project_docs=docs,
        )

    def text(self):
        """将快照渲染为一段固定格式的文本，供拼入 prompt prefix。

        这段文本作为 agent 的"工作区基线快照"被注入到系统提示中，
        让模型在做出行动决策前了解仓库的当前状态（分支、改动、文档等）。
        """
        commits = "\n".join(f"- {line}" for line in self.recent_commits) or "- none"
        docs = "\n".join(f"- {path}\n{snippet}" for path, snippet in self.project_docs.items()) or "- none"
        return textwrap.dedent(
            f"""\
            Workspace:
            - cwd: {self.cwd}
            - repo_root: {self.repo_root}
            - branch: {self.branch}
            - default_branch: {self.default_branch}
            - status:
            {self.status}
            - recent_commits:
            {commits}
            - project_docs:
            {docs}
            """
        ).strip()

    def fingerprint(self):
        """计算快照的 SHA256 指纹，用于缓存失效判断。

        当工作区状态变化时（切换分支、提交、文件改动），指纹会改变，
        调用方据此决定是否需要重新生成 prompt prefix。
        

        使用 sort_keys=True 保证 JSON 序列化稳定，
        确保相同语义内容始终产生相同的哈希值。
        """
        payload = {
            "cwd": self.cwd,
            "repo_root": self.repo_root,
            "branch": self.branch,
            "default_branch": self.default_branch,
            "status": self.status,
            "recent_commits": list(self.recent_commits),
            "project_docs": dict(self.project_docs),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
