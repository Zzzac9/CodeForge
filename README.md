# CodeForge

CodeForge 是一个面向本地代码仓库的轻量级 Coding Agent Harness。

它直接运行在终端中，先感知当前 Git 工作区，再通过受约束的工具读取文件、修改代码、执行命令，并将会话、Checkpoint、Trace 和运行报告持久化到本地 `.codeforge/` 目录。

项目重点不是堆更多“万能能力”，而是把 **模型接入、工具调用、上下文治理、工作记忆、安全边界、恢复机制与评测审计** 做成一条可控、可恢复、可复盘的执行链路。

## 核心特性

- **多模型后端**：支持 Ollama、OpenAI-compatible、Anthropic-compatible 与 DeepSeek。
- **受约束工具系统**：7 个显式注册工具，带参数校验、风险分级和审批策略。
- **Context Engineering**：按 section 管理 Prompt 预算，超限时分层压缩，当前请求始终保留。
- **结构化记忆**：工作记忆、文件摘要、episodic notes 与持久记忆分层管理。
- **Freshness 校验**：文件摘要使用 SHA-256 检查新鲜度，外部修改后自动失效。
- **Checkpoint / Resume**：工具执行后自动记录恢复点，并检测文件或工作区漂移。
- **安全边界**：路径沙箱、环境变量隔离、Secret 脱敏、审批机制、重复调用拦截。
- **完整审计**：每次运行生成 task state、trace 与 report，便于调试和回放。
- **本地优先**：状态全部保存在仓库本地，不依赖数据库或额外服务。
- **轻依赖**：核心运行时仅使用 Python 标准库。

## 运行界面

CLI 帮助：

![CodeForge CLI help](assets/screenshots/codeforge-help.png)

启动界面：

![CodeForge start](assets/screenshots/codeforge-start.png)

交互模式：

![CodeForge REPL](assets/screenshots/codeforge-repl.png)

## 架构概览

```text
CLI / REPL
   │
   ▼
CodeForge Runtime
   ├── Model Client        模型后端适配
   ├── Workspace Context   Git 与项目事实快照
   ├── Context Manager     Prompt 组装与预算压缩
   ├── Layered Memory      工作记忆与持久记忆
   ├── Tool Registry       工具注册、校验与执行
   ├── Checkpoint          任务恢复状态
   └── Run Store           trace / task_state / report
```

一次请求的主循环可以概括为：

```text
用户请求
  │
  ▼
构建工作区与上下文
  │
  ▼
调用模型
  │
  ▼
解析 <tool> / <final>
  │
  ├── tool → 校验 → 审批 → 执行 → 更新记忆 → Checkpoint
  │                              │
  │                              └─────────────┐
  │                                            ▼
  └────────────────────────────────────── 再次调用模型
                                               │
                                               ▼
                                            final
                                               │
                                               ▼
                                      持久记忆 + 审计报告
```

## 快速开始

### 1. 环境要求

- Python 3.10+
- Git（建议）
- 若使用 Ollama，需要本地 Ollama 服务

### 2. 安装

使用 `uv`：

```bash
uv sync
```

或者安装为 editable package：

```bash
pip install -e .
```

安装后可以通过以下两种方式启动：

```bash
codeforge
python -m codeforge
```

### 3. 配置模型

复制 `.env.example` 为 `.env`，只填写你实际使用的 provider。

当前默认 provider 为 **DeepSeek**：

```env
CODEFORGE_DEEPSEEK_API_BASE=https://api.deepseek.com/anthropic
CODEFORGE_DEEPSEEK_API_KEY=your-api-key
CODEFORGE_DEEPSEEK_MODEL=deepseek-v4-pro
```

OpenAI-compatible：

```env
CODEFORGE_OPENAI_API_BASE=https://your-api.example/v1
CODEFORGE_OPENAI_API_KEY=your-api-key
CODEFORGE_OPENAI_MODEL=gpt-5.4
```

Anthropic-compatible：

```env
CODEFORGE_ANTHROPIC_API_BASE=https://www.right.codes/claude/v1
CODEFORGE_ANTHROPIC_API_KEY=your-api-key
CODEFORGE_ANTHROPIC_MODEL=claude-sonnet-4-6
```

配置优先级：

```text
显式 CLI 参数 > .env 中的 CODEFORGE_* 变量 > 兼容环境变量 > 代码默认值
```

## 常用启动方式

交互模式：

```bash
codeforge
```

指定模型后端：

```bash
codeforge --provider openai
codeforge --provider anthropic
codeforge --provider deepseek
```

使用本地 Ollama：

```bash
ollama serve
ollama pull qwen3.5:4b
codeforge --provider ollama --model qwen3.5:4b
```

指定工作目录：

```bash
codeforge --cwd /path/to/repo
```

执行一次性任务：

```bash
codeforge "inspect the test failures and propose a fix"
```

恢复最近一次会话：

```bash
codeforge --resume latest
```

## REPL 内置命令

| 命令 | 作用 |
|---|---|
| `/help` | 查看帮助 |
| `/memory` | 查看提炼后的工作记忆 |
| `/session` | 查看当前 session 文件路径 |
| `/reset` | 清空当前会话状态 |
| `/exit` / `/quit` | 退出 |

## 工具系统

模型不能任意访问系统能力，只能调用显式注册的工具：

| 工具 | 风险 | 作用 |
|---|---|---|
| `list_files` | safe | 列出工作区文件 |
| `read_file` | safe | 按行读取 UTF-8 文件 |
| `search` | safe | 搜索仓库内容，优先使用 `rg` |
| `run_shell` | risky | 在仓库根目录执行命令 |
| `write_file` | risky | 创建或覆盖文本文件 |
| `patch_file` | risky | 对唯一命中的文本块做精确替换 |
| `delegate` | safe | 启动受限、只读的子 Agent 调查任务 |

高风险工具受 `--approval ask|auto|never` 控制。

## 上下文治理

CodeForge 将每轮 Prompt 拆成多个 section，并为不同 section 设置独立预算：

| Section | 默认预算 | 最低预算 |
|---|---:|---:|
| Prefix | 3600 | 1200 |
| Memory | 1600 | 400 |
| Relevant Memory | 1200 | 300 |
| History | 5200 | 1500 |

总预算默认约为 **12,000 字符**。当 Prompt 超限时按以下顺序压缩：

```text
relevant_memory → history → memory → prefix
```

当前用户请求不会被裁剪。

历史压缩会优先保留最近 6 轮；较旧的重复 `read_file` 会折叠，并尽量复用已经生成的文件摘要。

稳定的 Prefix 由规则、工具签名和工作区事实组成。工作区 fingerprint 未变化时，Prefix 可以继续复用同一缓存标识。

## 记忆系统

CodeForge 使用分层、可解释的文件记忆，而不是依赖向量数据库。

运行时工作记忆主要包含：

- 当前任务摘要
- 最近访问文件
- 文件短摘要
- episodic notes

文件摘要保存对应内容的 SHA-256 freshness。文件发生变化后，旧摘要会自动失效，避免恢复或后续推理继续依赖过期内容。

持久记忆保存在：

```text
.codeforge/memory/
├── MEMORY.md
└── topics/
    ├── project-conventions.md
    ├── key-decisions.md
    ├── dependency-facts.md
    └── user-preferences.md
```

相关记忆召回采用可解释的 **tag 命中 + 关键词重叠 + 时间排序**，不需要 embedding 服务。

## Checkpoint 与恢复

每次工具执行后，CodeForge 都会更新 Checkpoint，记录当前目标、阻塞点、下一步、关键文件 freshness 与运行时身份。

恢复时会判断：

| 状态 | 含义 |
|---|---|
| `full-valid` | Checkpoint 与当前环境一致 |
| `partial-stale` | 关键文件已经发生变化 |
| `workspace-mismatch` | 工作区或运行时配置发生变化 |
| `schema-mismatch` | Checkpoint schema 不兼容 |
| `no-checkpoint` | 没有可恢复状态 |

因此 `--resume latest` 并不是简单把旧对话重新塞回模型，而是先检查旧状态在当前仓库中是否仍然可信。

## 安全设计

CodeForge 的安全边界主要包括：

- **路径沙箱**：工具路径必须留在 workspace root 内。
- **环境变量隔离**：Shell 只继承允许的环境变量。
- **Secret 脱敏**：Trace 和 Report 写盘前会替换已配置的敏感值。
- **高风险审批**：写文件、Patch、Shell 命令可以要求人工确认。
- **重复调用拦截**：避免相同工具调用陷入死循环。
- **只读委派**：子 Agent 默认不能修改文件或执行高风险动作。
- **深度限制**：避免无限递归委派。

## 会话、运行工件与审计

所有状态默认保存在 `.codeforge/`，该目录已加入 `.gitignore`。

每次 `ask()` 都会产生一组运行工件：

```text
.codeforge/
├── sessions/
│   └── <session_id>.json
├── memory/
│   └── ...
└── runs/
    └── <run_id>/
        ├── task_state.json
        ├── trace.jsonl
        └── report.json
```

- `task_state.json`：当前任务状态、停止原因、工具步数等。
- `trace.jsonl`：模型调用、工具调用、Checkpoint 等事件时间线。
- `report.json`：最终运行摘要、Prompt 元数据、安全事件与持久记忆结果。

这使一次 Agent 运行既能继续，也能在结束后被复盘。

## 模型后端

| Provider | 接口 | Prompt Cache |
|---|---|---|
| Ollama | Ollama native | 否 |
| OpenAI-compatible | Responses API | 支持的后端可用 |
| Anthropic-compatible | Messages API | 当前未接入 |
| DeepSeek | Anthropic-compatible | 当前未接入 |
| FakeModelClient | 内存脚本 | 测试 / Benchmark 使用 |

所有真实模型后端都被适配为统一的：

```python
complete(prompt, max_new_tokens, ...) -> str
```

Runtime 不需要关心底层 HTTP、SSE 或不同 provider 的响应格式。

## 项目结构

```text
codeforge/
├── cli.py               # CLI / REPL 入口
├── config.py            # .env 与配置优先级
├── models.py            # 模型后端适配
├── runtime.py           # Agent 主控制循环
├── tools.py             # 工具注册、校验与执行
├── workspace.py         # Git 工作区快照
├── context_manager.py   # Prompt 预算与历史压缩
├── memory.py            # 工作记忆与持久记忆
├── task_state.py        # 单次任务状态机
├── run_store.py         # Trace / Report / State 落盘
├── evaluator.py         # 固定 Benchmark
└── metrics.py           # 指标与实验框架
```

其他主要目录：

```text
benchmarks/              # 固定回归任务
scripts/                 # 评测与实验脚本
tests/                   # 自动化测试
assets/screenshots/      # README 截图
```

## 测试与 Benchmark

运行自动化测试：

```bash
python -m pytest -q
```

项目内置固定 Benchmark 与指标框架，覆盖工具恢复、路径边界、重复读取、上下文压缩、记忆依赖、Checkpoint/Resume、安全场景以及多 provider 对照实验。

Benchmark 使用隔离的 fixture workspace 与 verifier 来判断最终产物，而不是只看模型有没有返回“完成”。

## 设计文档

更完整的实现说明、数据流和模块细节见：

[codeforge-实现逻辑与设计文档.md](codeforge-实现逻辑与设计文档.md)

## 设计原则

CodeForge 更关注一个 Coding Agent 在真实仓库里能否 **受约束地执行、连续地工作、在状态变化后正确恢复，并留下可解释的运行证据**。

> 受约束的能力，比不可审计的“万能能力”更可靠。
