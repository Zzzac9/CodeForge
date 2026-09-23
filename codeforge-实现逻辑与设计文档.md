# codeforge 完整实现逻辑与设计文档

> codeforge 是一个面向代码仓库的轻量本地 coding agent。它直接跑在终端里，先看当前工作区，再用一组受约束的工具去读文件、改文件、跑命令，并把会话状态保存在本地 `.codeforge/` 目录里。

---

## 目录

- [1. 项目总览](#1-项目总览)
- [2. 启动链路](#2-启动链路)
- [3. 配置层](#3-配置层)
- [4. 模型后端适配](#4-模型后端适配)
- [5. 核心运行时 (CodeForge)](#5-核心运行时-codeforge)
- [6. 工具系统](#6-工具系统)
- [7. 工作区快照](#7-工作区快照)
- [8. 上下文管理器](#8-上下文管理器)
- [9. 工作记忆系统](#9-工作记忆系统)
- [10. Checkpoint 与 Resume 机制](#10-checkpoint-与-resume-机制)
- [11. 持久化与审计](#11-持久化与审计)
- [12. 安全边界](#12-安全边界)
- [13. 测试与 Benchmark](#13-测试与-benchmark)
- [14. 完整数据流](#14-完整数据流)
- [15. 设计理念总结](#15-设计理念总结)

---

## 1. 项目总览

### 1.1 技术栈

- **语言**：Python 3.10+
- **依赖**：零外部依赖（仅标准库 `urllib`、`subprocess`、`json`、`argparse` 等）
- **安装**：`pip install -e .` 或 `uv sync`
- **CLI 命令**：`codeforge` 或 `python -m codeforge`

### 1.2 源文件结构（13 个模块）

```
codeforge/
├── __init__.py          # 公开 API 导出
├── __main__.py          # python -m codeforge 入口 → 调 cli.main()
├── cli.py               # 命令行解析 + REPL/one-shot 主循环
├── config.py            # .env 文件加载 + 环境变量优先级链
├── models.py            # 4 种模型后端的 HTTP 适配层
├── runtime.py           # 核心 agent 控制循环 (CodeForge 类, 1349 行)
├── tools.py             # 7 个工具的定义、校验、执行
├── workspace.py         # Git 工作区事实快照
├── context_manager.py   # Prompt 组装 + 预算收缩
├── memory.py            # 工作记忆 + 文件持久记忆
├── task_state.py        # 单次运行状态机 (TaskState)
├── run_store.py         # 运行工件落盘 (task_state / trace / report)
├── evaluator.py         # 固定基准回归测试框架
└── metrics.py           # 指标采集与实验框架
```

### 1.3 对象依赖图

```
CodeForge (runtime.py)
├── model_client      # models.py — Ollama / OpenAI / Anthropic / Fake
├── workspace         # workspace.py — Git 事实 + 项目文档
├── session_store     # runtime.py:SessionStore — 会话持久化
├── run_store         # run_store.py — 运行工件落盘
├── memory            # memory.py — 工作记忆 (LayeredMemory)
├── context_manager   # context_manager.py — prompt 组装
├── tools             # tools.py — 工具注册表 (7-8 个工具)
├── prefix_state      # 稳定前缀（"工作手册"）—— workspace + tool_specs
└── session           # 当前会话 (history + memory + checkpoints)
```

---

## 2. 启动链路

```
python -m codeforge  /  codeforge CLI
        │
        ▼
__main__.py → cli.main()
        │
        ├── 1. 解析参数 (build_arg_parser)
        │     支持的参数：
        │     ├── --provider (ollama/openai/anthropic/deepseek, 默认 deepseek)
        │     ├── --model (覆盖默认模型)
        │     ├── --approval (ask/auto/never, 默认 ask)
        │     ├── --max-steps (工具调用上限, 默认 6)
        │     ├── --max-new-tokens (输出 token 上限, 默认 512)
        │     ├── --temperature (采样温度, 默认 0.2)
        │     ├── --top-p (top-p 采样, 默认 0.9)
        │     ├── --resume (恢复 session, 支持 "latest")
        │     ├── --cwd (工作目录, 默认 .)
        │     └── --secret-env-name (额外 secret 变量名)
        │
        ├── 2. 装配 agent (build_agent)
        │     ├── WorkspaceContext.build(args.cwd)
        │     │     └── 运行 git 命令采集：repo_root, branch, status, recent commits
        │     ├── load_project_env(workspace.repo_root)
        │     │     └── 从 repo root 向上找 .env, 注入 os.environ
        │     ├── 整理 secret 环境变量白名单
        │     ├── 创建 SessionStore (.codeforge/sessions/)
        │     ├── _build_model_client(args)
        │     │     └── 根据 provider 创建对应的 HTTP client
        │     └── 如果 --resume: CodeForge.from_session()
        │         否则: 新建 CodeForge()
        │
        ├── 3. 打印欢迎界面 (build_welcome)
        │     ASCII 猫头 + 工作区/模型/审批/会话信息
        │
        └── 4. 进入运行模式
              ├── One-shot: 有命令行 prompt → agent.ask(prompt) → 打印 → 退出
              └── REPL: while True: input("codeforge> ") → agent.ask(input)
                    内置命令:
                    ├── /help    → 查看帮助
                    ├── /memory  → 查看工作记忆
                    ├── /session → 查看会话文件路径
                    ├── /reset   → 清空会话
                    └── /exit    → 退出
```

---

## 3. 配置层

`config.py` 负责环境变量加载与优先级。

### 3.1 优先级链

```
显式 CLI 参数 > .env 里的 CODEFORGE_* 变量 > 旧环境变量名 (兼容) > 代码默认值
```

### 3.2 关键函数

| 函数 | 作用 |
|---|---|
| `find_project_env(start)` | 从当前目录向上遍历找 `.env` 文件 |
| `load_project_env(start)` | 解析 `.env` 中的 `KEY=VALUE` 行并注入 `os.environ`，支持 `export` 前缀和引号值 |
| `provider_env(name, legacy_names, default)` | 按优先级链查找：`CODEFORGE_XXX` → 旧名列表 → default |

---

## 4. 模型后端适配

`models.py` 把 4 种不同 provider 的 HTTP 接口差异抹平成统一的 `complete(prompt, max_new_tokens) → str` 接口。

### 4.1 四种客户端

| 类 | 协议 | HTTP 端点 | Prompt Cache | 用途 |
|---|---|---|---|---|
| `OllamaModelClient` | Ollama native | `POST /api/generate` | ❌ | 本地模型 |
| `OpenAICompatibleModelClient` | OpenAI Responses API | `POST /v1/responses` | ✅ (仅 openai.com / right.codes) | GPT 系 |
| `AnthropicCompatibleModelClient` | Anthropic Messages API | `POST /v1/messages` | ❌ | Claude / DeepSeek |
| `FakeModelClient` | 内存脚本 | - | ❌ | 测试/benchmark |

### 4.2 统一接口

```python
def complete(self, prompt, max_new_tokens,
             prompt_cache_key=None, prompt_cache_retention=None) -> str
```

- runtime 不需要知道底层是 HTTP 还是内存、SSE 还是 JSON
- prompt cache 参数在 `supports_prompt_cache=True` 时才实际发送
- `last_completion_metadata` 记录 usage / cached_tokens 供上报

### 4.3 Ollama 客户端

- 发送 `model` + `prompt` + `options`（num_predict, temperature, top_p）
- 返回 `response` 字段
- 错误处理：HTTP 错误 + Ollama 业务错误

### 4.4 OpenAI 兼容客户端

- 发送 `input` 数组格式的 message，支持 `prompt_cache_key` / `prompt_cache_retention`
- **双格式响应解析**：
  - **SSE** (`text/event-stream`)：逐行解析 `data:` 事件，支持 `response.output_text.delta`（增量）、`response.output_text.done`（完成）、`response.completed`（完整响应）
  - **JSON**：直接解析 `output_text` / `output[].content[].text` / `choices[].message.content`
- 3 次重试 + 指数退避（仅 5xx 错误）
- 从 usage 中提取 `cached_tokens` 做缓存命中统计

### 4.5 Anthropic 兼容客户端

- 发送 `messages` 数组格式，带 `x-api-key` 和 `anthropic-version: 2023-06-01` 头
- 从 `content` 数组中提取 `text` block
- **特殊处理 DeepSeek**：DeepSeek 的 Anthropic 兼容接口会自动把 `<tool>` XML 转为 `tool_use` content block，客户端将其还原为 `<tool>` XML 格式
- 3 次重试 + 指数退避

### 4.6 Base URL 规范化

`_normalize_versioned_base_url()` 确保所有兼容接口的 URL 都以 `/v1` 结尾。

---

## 5. 核心运行时 (CodeForge)

`runtime.py`（1349 行）是整个项目的心脏。`CodeForge` 类持有 agent 的全部状态。

### 5.1 对象图

```
CodeForge
├── model_client        # 模型后端 (Ollama/OpenAI/Anthropic/Fake)
├── workspace           # WorkspaceContext — Git 事实 + 项目文档快照
├── session_store       # SessionStore — .codeforge/sessions/ 的 CRUD
├── run_store           # RunStore — .codeforge/runs/<run_id>/ 工件管理
├── memory              # LayeredMemory — 工作记忆
├── context_manager     # ContextManager — prompt 组装与预算控制
├── tools               # dict: tool_name → callable
├── prefix              # PromptPrefix — 稳定前缀（哈希 + workspace 指纹 + tool 签名）
├── approval_policy     # "ask" | "auto" | "never"
├── max_steps           # 工具调用上限 (默认 6)
├── max_new_tokens      # 每步输出 token 上限 (默认 512)
├── secret_env_names    # 需脱敏的环境变量名列表
├── session             # {"id", "history", "memory", "checkpoint"}
└── checkpoint_mgr      # 每步后自动 checkpoint
```

### 5.2 `ask()` — 总调度器

这是 **感知 → 决策 → 行动 → 记录** 的完整控制循环：

```
ask(user_message)
  │
  ├── 0. 初始化
  │     ├── TaskState.create()           # 创建任务状态机
  │     ├── run_store.start_run()        # 创建 .codeforge/runs/<run_id>/
  │     ├── record({"role":"user", ...}) # 记入 history
  │     └── tool_steps = 0, attempts = 0
  │
  └── while tool_steps < max_steps AND attempts < (max_steps * 3):
       │
       ├── 1. 感知: _build_prompt_and_metadata()
       │     ├── refresh_prefix()
       │     │     └── 检查 workspace fingerprint 是否变化
       │     │         → 变了: 重新构建前缀、更新 prompt cache key
       │     │         → 没变: 复用旧前缀
       │     ├── evaluate_resume_state()
       │     │     └── 检查最近的 checkpoint
       │     │         返回: full-valid | partial-stale | workspace-mismatch |
       │     │               schema-mismatch | no-checkpoint
       │     └── context_manager.build(user_message)
       │           └── 组装完整 prompt (prefix + memory + relevant_memory
       │                                         + history + current_request)
       │
       ├── 2. 决策: model_client.complete(prompt)
       │     ├── 传入 prompt_cache_key (稳定前缀的哈希)
       │     ├── 传入 prompt_cache_retention
       │     └── 返回模型原始输出文本
       │
       ├── 3. 解析: parse(raw)
       │     ├── 先尝试 JSON 格式: <tool>{"name":"...","args":{...}}</tool>
       │     ├── 再尝试 XML 格式:  <tool name="..." path="..."><content>...</content></tool>
       │     ├── 识别 <final>answer</final> → kind="final"
       │     ├── 裸文本（无标签）→ 自动当作 final
       │     └── 无法解析 → kind="retry"
       │
       ├── 4. [if tool] 行动: run_tool(name, args)
       │     ├── ✅ 工具是否存在？
       │     ├── ✅ 参数是否合法？(validate_tool)
       │     ├── ✅ 是否重复调用？(最近 2 次完全相同 → 拒绝)
       │     ├── ✅ 是否需要审批？(risky 工具 + approval_policy 决定)
       │     ├── 📸 执行前拍工作区快照
       │     ├── ⚙️ 执行工具
       │     ├── 📸 执行后拍快照 → diff 对比
       │     └── 🧠 更新 working memory
       │
       ├── 5. [if final] 完成
       │     ├── promote_durable_memory() → 提取持久记忆
       │     ├── write_report() → 写审计报告
       │     └── return final_answer
       │
       └── 6. 记录: 每步之后
             ├── record({"role":"tool", ...})  → 写入 history
             ├── emit_trace()                  → 写 trace.jsonl
             ├── write_task_state()            → 更新 task_state.json
             └── create_checkpoint()           → 打 checkpoint
```

### 5.3 `parse()` — 模型输出解析

支持两种格式：

**JSON 格式**：
```xml
<tool>{"name":"read_file","args":{"path":"src/main.py"}}</tool>
```

**XML 格式**：
```xml
<tool name="write_file" path="src/main.py">
<content>
print("hello")
</content>
</tool>
```

解析逻辑：
1. 先尝试 JSON 解析 `<tool>...</tool>` 内容
2. JSON 失败则尝试 XML 解析（从属性 + 子元素提取 name 和 args）
3. 寻找 `<final>...</final>` 或裸文本作为最终答案
4. 都不匹配返回 `("retry", error_message)`

### 5.4 `run_tool()` — 工具执行护栏

完整的防线链：

```
run_tool(name, args)
  │
  ├── 1. 工具名校验
  │     └── name not in self.tools → error: "unknown tool"
  │
  ├── 2. 参数校验 (validate_tool)
  │     └── 按 tool_spec 检查必填字段、类型
  │
  ├── 3. 重复调用检测
  │     └── 与最近 2 次调用的 (name, args) 完全一致 → 拒绝
  │
  ├── 4. 审批检查
  │     ├── safe 工具 (list_files, read_file, search) → 直接放行
  │     └── risky 工具 (write_file, patch_file, run_shell):
  │           ├── approval_policy="auto"   → 自动批准
  │           ├── approval_policy="never"  → 直接拒绝
  │           └── approval_policy="ask"    → 交互式 y/N 确认
  │
  ├── 5. 工作区快照
  │     ├── 执行前: snapshot_before
  │     ├── 执行中: tool_result = tool(args)
  │     └── 执行后: snapshot_after → diff
  │
  └── 6. 更新 working memory
        └── 根据工具类型更新 recent_files / file_summaries
```

### 5.5 `approve()` — 交互审批

```
显示: "Allow write_file src/main.py? [y/N]"
等待用户输入 → y/yes → 批准; 其他 → 拒绝
```

### 5.6 `path()` — 路径沙箱

```python
def path(self, raw_path):
    resolved = os.path.normpath(os.path.join(self.workspace.cwd, raw_path))
    # 必须在 workspace root 之下
    if os.path.commonpath([resolved, self.workspace.repo_root]) != self.workspace.repo_root:
        raise ValueError("path escapes workspace")
    return resolved
```

### 5.7 `reset()` — 清空会话

清空 history、memory、checkpoint、run_store，保留 session ID 和 model client。

---

## 6. 工具系统

`tools.py` 定义了 agent 可用的全部工具。

### 6.1 工具注册表

| 工具 | 风险等级 | 能力 | 关键参数 |
|---|---|---|---|
| `list_files` | safe | 列出目录下的文件和子目录 | `path` (相对路径) |
| `read_file` | safe | 按行号范围读取文件 | `path`, `start_line`, `end_line` |
| `search` | safe | 搜索文件内容（优先 rg，fallback Python） | `pattern`, `path` |
| `run_shell` | **risky** | 在 repo root 执行命令 | `command` |
| `write_file` | **risky** | 创建/覆盖文件 | `path`, `content` |
| `patch_file` | **risky** | 精确字符串替换 | `path`, `old_text`, `new_text` |
| `delegate` | safe | 创建只读子 agent | `task` (任务描述) |

### 6.2 各工具实现细节

**`list_files`**：
- 跳过 IGNORED_PATH_NAMES：`.git`、`.codeforge`、`__pycache__`、`.venv`、`node_modules` 等
- 最多返回 200 条
- 标记 `[D]` 目录 / `[F]` 文件

**`read_file`**：
- UTF-8 编码读取
- 行号从 1 开始
- 行范围 1-200 行

**`search`**：
- 优先调用 `rg`（更快的原生搜索）
- 没有 `rg` 则 fallback 到纯 Python 的逐文件遍历
- 最多 200 条匹配

**`run_shell`**：
- `subprocess.run(shell=True)` 在 repo_root 执行
- 超时 1-120 秒
- 环境变量隔离：只传 `HOME`, `PATH`, `TERM`, `USER`, `SHELL`, `LANG`, `VIRTUAL_ENV`, `CONDA_PREFIX` 等白名单
- 返回 `exit_code` + `stdout` + `stderr`

**`write_file`**：
- 自动创建父目录
- UTF-8 写入

**`patch_file`**：
- **精确替换**：`old_text` 必须在文件中恰好命中 1 次
- 命中 0 次 → error: "not found"
- 命中 N 次 → error: "ambiguous, matched N times"

**`delegate`**：
- 创建子 `CodeForge` 实例：
  - `read_only=True`
  - `approval_policy="never"`
  - `depth = parent_depth + 1`
  - `max_steps = 3`
  - 初始 memory 包含父 agent 的历史摘要
- 调用子 agent 的 `ask(task)`，返回结果

### 6.3 工具校验

`validate_tool(agent, name, args)` 对每个工具做细粒度校验：

- `list_files`：path 必须存在且是目录
- `read_file`：path 必须存在且是文件，行号范围合法
- `search`：pattern 非空
- `run_shell`：command 非空
- `write_file`：path 不能是已存在的目录
- `patch_file`：path 必须存在且是文件

---

## 7. 工作区快照

`workspace.py` 的 `WorkspaceContext` 在 agent 启动时采集仓库的"事实快照"。

### 7.1 `WorkspaceContext.build(cwd)`

```
1. git rev-parse --show-toplevel  → repo_root
2. git rev-parse --abbrev-ref HEAD → branch
3. git remote show origin 2>/dev/null | grep "HEAD branch" → default_branch
4. git status --short              → 工作区状态
5. git log --oneline -5            → 最近 5 条提交
6. 扫描白名单项目文档:
   ├── repo_root 下的 AGENTS.md, README.md, pyproject.toml, package.json
   └── cwd 下的 上述文件（项目级 + 子目录级）
   每个文件截断到 1200 字符
```

### 7.2 `text()` — 渲染为 prompt 文本

```
REPOSITORY ROOT: /path/to/repo
CURRENT BRANCH: main
DEFAULT BRANCH: main
GIT STATUS:
 M src/main.py
?? new.txt
RECENT COMMITS:
abc1234 Fix login bug
def5678 Add tests
PROJECT FILES:
--- AGENTS.md ---
(content truncated to 1200 chars)
```

### 7.3 `fingerprint()` — 缓存失效标识

对整个工作区文本做 SHA-256 哈希，用于判断稳定前缀是否需要重建，以及 prompt cache 是否仍然有效。

---

## 8. 上下文管理器

`context_manager.py` 的 `ContextManager` 负责将 prompt 控制在 12,000 字符预算内。

### 8.1 Prompt 布局

```
┌──────────────────────────────────────────────┐
│ PREFIX (预算 3600 → 地板 1200)               │
│   稳定规则 + 工作区事实 + checkpoint 状态     │
├──────────────────────────────────────────────┤
│ MEMORY (预算 1600 → 地板 400)                │
│   working memory 仪表盘                      │
│   (task_summary + recent_files + file_summaries)│
├──────────────────────────────────────────────┤
│ RELEVANT MEMORY (预算 1200 → 地板 300)       │
│   与当前请求相关的持久记忆笔记               │
├──────────────────────────────────────────────┤
│ HISTORY (预算 5200 → 地板 1500)              │
│   压缩后的会话历史                           │
│   - 最近 6 轮完整保留                        │
│   - 旧轮次去重 + 摘要                        │
├──────────────────────────────────────────────┤
│ CURRENT REQUEST                             │
│   永远不裁剪                                 │
└──────────────────────────────────────────────┘
```

### 8.2 预算收缩策略

当总 prompt 超过 12,000 字符时，按以下顺序依次压缩：

```
relevant_memory → history → memory → prefix
```

每段都有"地板"（预算的 1/4），低于地板就不再压缩。Current request 永远不压缩。

### 8.3 历史压缩规则

- **最近 6 轮**（`recent_window`）完整保留，单条最多 900 字符
- **旧轮次的 `read_file`**：去重，用文件摘要替代正文
- **旧工具调用**：压缩为单行摘要（`command → exit_code | stdout | stderr`）
- 从 budget 大往小填：先尝试放全部条目，超预算就从最近的条目开始裁

---

## 9. 工作记忆系统

`memory.py` 实现两层记忆。

### 9.1 LayeredMemory（运行时工作记忆）

```python
{
    "working": {
        "task_summary": "当前任务的一句话摘要",
        "recent_files": ["file1.py", "file2.py", ...]  # ≤ 8 个
    },
    "episodic_notes": [
        {"tag": "bug", "content": "login 函数的空指针..."}  # ≤ 12 条
    ],
    "file_summaries": {
        "file1.py": {
            "summary": "前 3 行非空内容...",
            "freshness": "sha256_hash"
        }  # ≤ 500 字符, ≤ 6 个
    }
}
```

### 9.2 记忆更新时机

| 事件 | 操作 |
|---|---|
| `read_file` | `remember_file()` + `set_file_summary()` |
| `write_file` / `patch_file` | `remember_file()` + `invalidate_file_summary()` |
| 工具执行后 | 提取关键信息到 episodic_notes |
| 每轮对话开始 | `invalidate_stale_file_summaries()` 检查 freshness |

### 9.3 文件摘要新鲜度

- 每次 `set_file_summary()` 计算文件内容的 SHA-256 hash
- 后续操作前用当前文件 hash 与记录对比
- 不匹配 → 摘要过期，自动失效

### 9.4 DurableMemoryStore（持久记忆）

存盘到 `.codeforge/memory/`：
```
.codeforge/memory/
├── MEMORY.md           # 索引文件
└── topics/
    ├── project-conventions.md
    ├── key-decisions.md
    ├── dependency-facts.md
    └── user-preferences.md
```

- **Subject Key 去重**：同类事实的新记录自动替代旧记录
- **`promote()`**：从模型最终答案中提取标记行（`Project convention:` / `Decision:` / `Dependency fact:` / `User preference:`）并持久化
- **自动记录**：`promote_auto_records()` 让模型从历史中自动发现值得持久化的知识

### 9.5 记忆检索

`retrieval_candidates(query)` 做**纯关键词匹配**（不用 embedding）：

1. **Tag 精确匹配**：query 中的 tag 与笔记 tag 完全一致 → 高分
2. **关键词重叠**：query 词与笔记内容的重叠度 → 中分
3. **排序**：按 (tag 匹配, 关键词重叠, 时间倒序) 综合排序
4. 取 top N（由 `MAX_RELEVANT_NOTES` 控制）

---

## 10. Checkpoint 与 Resume 机制

### 10.1 Checkpoint 创建

每次 `ask()` 的工具执行后都会 `create_checkpoint()`，记录：

```
{
    "goal": "当前任务目标",
    "blocker": "当前卡点",
    "next_step": "下一步计划",
    "key_files": [
        {"path": "src/main.py", "freshness": "sha256_hash"}
    ],
    "runtime_identity": {
        "cwd": "...",
        "model": "...",
        "approval_policy": "...",
        "feature_flags": {...},
        "workspace_fingerprint": "sha256",
        "tool_signature": ["list_files", "read_file", ...]
    },
    "schema_version": 2,
    "created_at": "ISO timestamp"
}
```

### 10.2 Resume 状态评估

`evaluate_resume_state()` 比较当前运行时与 checkpoint 的状态：

| 状态 | 含义 | 行为 |
|---|---|---|
| `full-valid` | everything matches | 正常继续 |
| `partial-stale` | 关键文件的 freshness 变了 | 文件被外部修改过，提醒模型 |
| `workspace-mismatch` | workspace fingerprint 或 runtime 配置变了 | 不同的工作环境，谨慎恢复 |
| `schema-mismatch` | checkpoint schema 版本不兼容 | 无法恢复 |
| `no-checkpoint` | 没有 checkpoint | 全新开始 |

### 10.3 Session 持久化

`SessionStore` 管理 `.codeforge/sessions/{id}.json`，包含完整的 history、memory、checkpoint。

恢复链路：
```
--resume latest
    → SessionStore.latest() 找到最新的 session
    → SessionStore.load(session_id) 加载 JSON
    → CodeForge.from_session(...) 重建 CodeForge 实例
    → evaluate_resume_state() 评估 checkpoint 新鲜度
    → 在 prompt 中注入 checkpoint context
```

---

## 11. 持久化与审计

`run_store.py` + `task_state.py` 负责运行期间的数据持久化。

### 11.1 Run 工件目录

每次 `ask()` 产生一个 `run_<timestamp>` 目录：

```
.codeforge/runs/run_20260101_120000/
├── task_state.json   # 任务状态机快照（每步更新，原子写入）
├── trace.jsonl       # 逐事件时间线（追加写入）
└── report.json       # 最终审计报告（原子写入）
```

### 11.2 TaskState 状态机

```
running → completed  (final_answer_returned)
running → stopped    (step_limit_reached | retry_limit_reached | user_interrupted)
running → failed     (exception)
```

记录字段：`run_id`, `task_id`, `user_request`, `status`, `tool_steps[]`, `attempts`, `last_tool`, `stop_reason`, `final_answer`, `checkpoint_id`, `resume_status`

### 11.3 Trace 事件

每步 action 都以 JSONL 格式追加写入 trace：
```json
{"event": "tool_call", "name": "read_file", "args": {...}, "timestamp": "..."}
{"event": "tool_result", "output": "...", "duration_ms": 123}
{"event": "model_call", "prompt_len": 8500, "metadata": {...}}
{"event": "checkpoint", "key_files": [...]}
```

### 11.4 Report 审计报告

最终生成的 `report.json` 包含：
- `task_state` 快照
- `prompt_metadata`（模型、参数、缓存命中率）
- `durable_promotions`（本次运行提炼的持久记忆）
- `security_events`（审批记录、路径逃逸拦截等）

---

## 12. 安全边界

### 12.1 路径沙箱

```python
CodeForge.path(raw_path)
    → os.path.normpath(os.path.join(workspace.cwd, raw_path))
    → 检查 commonpath 必须在 workspace.repo_root 之下
    → 防止 ../ 和符号链接逃逸
```

### 12.2 环境变量隔离

`run_shell` 只继承白名单环境变量：
- `HOME`, `PATH`, `TERM`, `USER`, `SHELL`, `LANG`
- `VIRTUAL_ENV`, `CONDA_PREFIX`
- 不泄露父 shell 的完整环境

### 12.3 Secret 脱敏

```python
CodeForge.redact_text(text)
    → 遍历 secret_env_names
    → 用 os.environ[name] 的值替换为 "<redacted>"
```

在 trace 和 report 写入前自动脱敏。

### 12.4 子 Agent 只读委托

`tool_delegate` 创建子 `CodeForge` 时：
- `read_only=True` → 不能执行 write_file / patch_file / run_shell
- `approval_policy="never"` → 不弹交互确认
- `depth + 1`，有最大深度限制

### 12.5 审批防线

| 工具类型 | safe (list_files, read_file, search, delegate) | risky (write_file, patch_file, run_shell) |
|---|---|---|
| `--approval ask` | 直接执行 | **交互确认** |
| `--approval auto` | 直接执行 | 自动批准 |
| `--approval never` | 直接执行 | **拒绝** |

### 12.6 重复调用检测

最近 2 次调用的 `(name, args)` 完全一致 → 自动拒绝，防止死循环。

---

## 13. 测试与 Benchmark

### 13.1 evaluator.py — 基准回归测试

- 使用 `FakeModelClient` 注入预定义的模型输出序列
- 覆盖 20+ 种场景：正常 patch、无效 patch 恢复、路径逃逸恢复、重复读取恢复等
- 从 `benchmarks/coding_tasks.json` 加载任务定义
- 复制 fixture 仓库到临时目录作为隔离工作区
- 用 shell verifier 验证最终产物
- 产出 benchmark artifact JSON

### 13.2 metrics.py — 指标与实验框架

| 实验 | 说明 |
|---|---|
| `run_memory_dependency_experiment()` | 12 tasks × 3 variants × 5 reps：测试记忆对减少重复读取的效果 |
| `run_context_stress_matrix()` | 上下文压力矩阵测试 |
| `run_security_experiment_suite()` | 10 个安全场景：路径逃逸、符号链接、审批拒绝等 |
| `run_recovery_ablation_v2()` | 恢复能力消融实验 |
| `run_provider_experiments()` | 真实模型多 provider 对比（GPT/Claude/DeepSeek） |

---

## 14. 完整数据流

```
用户输入 "帮我修复 test_login.py 的断言错误"
  │
  ▼
cli.main()
  └── while True: input("codeforge> ") → agent.ask(user_input)
  │
  ▼
CodeForge.ask("帮我修复 test_login.py 的断言错误")
  │
  ├── record({"role":"user", "content":"帮我修复 test_login.py 的断言错误"})
  ├── TaskState.create()                    # 创建任务状态: status=running
  ├── run_store.start_run()                 # 创建 .codeforge/runs/run_20260101_120000/
  │
  └── 主控制循环 ─────────────────────────────────────────────
       │
       ├── 第 1 轮 ──────────────────────────────────────────
       │   ├── refresh_prefix()             # workspace fingerprint 不变 → 复用
       │   ├── evaluate_resume_state()      # 无 checkpoint → "no-checkpoint"
       │   ├── context_manager.build(message)
       │   │     组装 prompt (prefix + memory + history + current_request)
       │   │     预算: 总共 ~8,500 字符, 未触发压缩
       │   │
       │   ├── model_client.complete(prompt, max_new_tokens=512)
       │   │     → 模型输出:
       │   │       <tool>{"name":"read_file","args":{"path":"test_login.py"}}</tool>
       │   │
       │   ├── parse(raw) → ("tool", {"name":"read_file", "args":{"path":"test_login.py"}})
       │   │
       │   ├── run_tool("read_file", {"path":"test_login.py"})
       │   │     ├── safe 工具 → 无需审批
       │   │     ├── 路径沙箱检查 ✓
       │   │     ├── 执行: 读取文件内容
       │   │     ├── remember_file("test_login.py")
       │   │     └── set_file_summary("test_login.py", "def test_login(): ...")
       │   │
       │   ├── record({"role":"tool", ...})  # 记录工具结果
       │   ├── emit_trace()                  # 写 trace.jsonl
       │   ├── write_task_state()            # 更新 task_state
       │   └── create_checkpoint()           # checkpoint: {"goal":"修复断言","key_files":["test_login.py"]}
       │
       ├── 第 2 轮 ──────────────────────────────────────────
       │   ├── context_manager.build(message)
       │   │     prompt 现在包含 read_file 的结果
       │   │
       │   ├── model_client.complete(prompt)
       │   │     → 模型输出:
       │   │       <tool>{"name":"patch_file","args":{"path":"test_login.py",
       │   │         "old_text":"assert result == 1","new_text":"assert result == 2"}}</tool>
       │   │
       │   ├── parse(raw) → ("tool", {"name":"patch_file", ...})
       │   │
       │   ├── run_tool("patch_file", {"path":"test_login.py", ...})
       │   │     ├── risky 工具 → approval_policy="ask" → 交互确认
       │   │     │   打印: "Allow patch_file test_login.py? [y/N]"
       │   │     │   用户输入: y
       │   │     ├── 路径沙箱检查 ✓
       │   │     ├── 精确替换: old_text 命中 1 次 → 成功
       │   │     ├── remember_file("test_login.py")
       │   │     └── invalidate_file_summary("test_login.py")
       │   │
       │   └── create_checkpoint() ...        # 更新 checkpoint
       │
       ├── 第 3 轮 ──────────────────────────────────────────
       │   ├── context_manager.build(message)
       │   │
       │   ├── model_client.complete(prompt)
       │   │     → 模型输出:
       │   │       <final>已将 test_login.py 中的断言从 assert result == 1
       │   │       修改为 assert result == 2。可以运行 pytest 验证。</final>
       │   │
       │   ├── parse(raw) → ("final", "已将 test_login.py 中的断言...")
       │   │
       │   └── 完成！
       │
       └── 最终处理 ─────────────────────────────────────────
             ├── promote_durable_memory()
             │     └── 从 final answer 提取标记行 → 写入 .codeforge/memory/topics/
             ├── write_report()                # 写 report.json
             ├── write_task_state()            # status = completed
             └── return "已将 test_login.py 中的断言..."
  │
  ▼
cli.main() 打印结果
```

---

## 15. 设计理念总结

### 15.1 白名单工具

模型**只能用** 7 个显式注册的工具，参数严格校验。没有"万能函数调用"——每个工具都有明确的 schema、风险等级、输入约束。

### 15.2 多层安全护栏

```
路径沙箱 (防逃逸)
  +
环境变量隔离 (防泄露)
  +
Secret 脱敏 (trace/report 自动打码)
  +
审批机制 (risky 工具必须确认)
  +
重复调用检测 (防死循环)
  +
只读子 agent (delegate 不能修改文件)
  +
深度限制 (delegate 不能无限嵌套)
```

### 15.3 完整审计追踪

每次 `ask()` 都产生 3 个文件：
- **trace.jsonl**：逐事件时间线（可回放）
- **task_state.json**：状态机快照（可恢复）
- **report.json**：最终摘要（可审计）

### 15.4 上下文预算控制

- 总 prompt 硬上限 12,000 字符
- 每段有预算 + 地板
- 压缩顺序明确：相关记忆 → 历史 → 工作记忆 → 前缀
- 当前请求永远不裁剪
- **稳定前缀缓存**：前缀变化时才重建，其余时间复用同一 cache key

### 15.5 Checkpoint 恢复

- 每次工具执行后自动打 checkpoint
- 支持跨会话恢复（`--resume latest`）
- 自动检测过期（文件被外部修改、workspace 配置变化）
- 在 prompt 中注入恢复状态，让模型感知上下文

### 15.6 工作记忆而非向量数据库

- 不做 embedding，不做语义搜索
- 纯关键词匹配 + tag + 时间排序
- 文件摘要带新鲜度校验（SHA-256）
- 持久记忆用 Subject Key 去重，简单的文件系统存储

### 15.7 模型无关

通过 `models.py` 适配层抹平差异：
- Ollama（本地）
- OpenAI 兼容（GPT）
- Anthropic 兼容（Claude / DeepSeek）
- Fake（测试）

所有 provider 暴露统一的 `complete(prompt) → str` 接口。

### 15.8 零外部依赖

只使用 Python 标准库：`urllib`、`subprocess`、`json`、`argparse`、`pathlib`、`hashlib`、`textwrap` 等。安装即用，无需 `pip install` 额外包。

### 15.9 原子写入

所有文件写入（task_state.json、report.json）都通过 `tempfile + os.replace` 实现原子操作，防止写入中断导致文件损坏。

### 15.10 本地优先

- 所有状态保存在 `.codeforge/` 目录（sessions/ + runs/ + memory/）
- 不需要外部服务
- 不需要数据库
- 不需要网络（除模型 API 调用外）
- 不建议提交 `.codeforge/` 到 git（已在 `.gitignore`）

---

> **一句话总结**：codeforge 是一个用 Python 标准库构建的、面向本地仓库的轻量 coding agent。它通过白名单工具 + 多层护栏保证安全，通过上下文预算控制保证效率，通过 checkpoint/resume 保证连续性，通过 trace/report 保证可审计。它的核心哲学是"受约束的能力比开放式的能力更可靠"。
