# CodeForge

CodeForge 是一个面向本地代码仓库的轻量级 Coding Agent Harness。

它直接运行在终端中，先感知当前 Git 工作区，再通过受约束的工具读取文件、修改代码、执行命令，并将会话、Checkpoint、Trace 和运行报告持久化到本地 `.codeforge/` 目录。

项目重点不是堆更多“万能能力”，而是把 **模型接入、工具调用、上下文治理、工作记忆、安全边界、恢复机制与评测审计** 做成一条可控、可恢复、可复盘的执行链路。

## 核心特性

- **统一模型接口**：只保留一套 OpenAI-compatible Chat Completions 协议，通过 `base_url / api_key / model` 切换兼容模型服务。
- **受约束工具系统**：7 个显式注册工具，带参数校验、风险分级和审批策略。
- **Context Engineering**：按 section 管理 Prompt 预算，超限时分层压缩，当前请求始终保留。
- **结构化记忆**：工作记忆、文件摘要、episodic notes 与持久记忆分层管理。
- **Freshness 校验**：文件摘要使用 SHA-256 检查新鲜度，外部修改后自动失效。
- **Checkpoint / Resume**：工具执行后自动记录恢复点，并检测文件或工作区漂移。
- **安全边界**：路径沙箱、环境变量隔离、Secret 脱敏、审批机制、重复调用拦截。
- **完整审计**：每次运行生成 task state、trace 与 report，便于调试和回放。
- **本地优先**：状态全部保存在仓库本地，不依赖数据库或额外服务。
- **轻依赖**：核心运行时仅使用 Python 标准库。

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

CodeForge 不再按 provider 维护多套协议，只使用一套 **OpenAI-compatible Chat Completions** 接口。切换模型服务只需要改三个值：

```env
CODEFORGE_API_BASE=https://api.deepseek.com
CODEFORGE_API_KEY=your-api-key
CODEFORGE_MODEL=deepseek-v4-pro
CODEFORGE_PROMPT_CACHE_MODE=auto
```

例如切换到其他支持 Chat Completions API 的 OpenAI-compatible endpoint，只需要替换 `API_BASE`、`API_KEY` 和 `MODEL`。Runtime、Tool Calling、Memory、Checkpoint 与 Trace 都不需要变化。

配置优先级：

```text
显式 CLI 参数 > .env 中的 CODEFORGE_* 变量 > 兼容旧环境变量 > 代码默认值
```

## 常用启动方式

交互模式：

```bash
codeforge
```

临时覆盖模型或 API 地址：

```bash
codeforge --model deepseek-v4-pro
codeforge --base-url https://api.deepseek.com
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

CodeForge 的安全链按顺序执行：参数校验 → 操作级风险分析 → 强制安全边界 → 审批 → 执行。审批通过不等于安全检查通过；`auto` 只自动批准，不能越过禁止策略、只读模式或 Shell 后端边界。

- **路径边界**：文件工具只接受 workspace root 内路径，并拒绝路径链中的符号链接和 Windows reparse point；这不是进程隔离意义上的“沙箱”。
- **原子文件写入**：`write_file` / `patch_file` 使用同目录临时文件、`fsync` 和 `os.replace`，`patch_file` 在执行点再次检查 `old_text` 唯一匹配。
- **Docker Shell 沙箱**：默认每次 `run_shell` 创建一个临时非 root 容器，只挂载工作区，禁网、只读根文件系统、丢弃 capabilities，并限制 CPU、内存、进程数、时间和输出。
- **安全失败**：Docker 不可用、镜像缺失、启动失败或异常退出时不会回退到宿主机 Shell。
- **显式宿主机模式**：`--shell-backend unsafe-host` 是独立的非安全兼容选项，`auto` 不会隐式启用它。
- **环境变量隔离**：容器只收到 Shell 白名单中的非敏感环境变量；Docker CLI 连接变量不传入容器。
- **Secret 脱敏**：Trace 和 Report 写盘前会替换已配置的敏感值。
- **双层决策**：保留 Registry 的静态 `risky` 标记，同时记录具体操作的风险等级、目标、可能影响、未知影响、审批结果和安全检查结果。
- **重复调用拦截**：避免相同工具调用陷入死循环。
- **只读委派**：子 Agent 默认不能修改文件或执行高风险动作。
- **深度限制**：避免无限递归委派。

Shell 默认配置：

```bash
# 先由操作者显式准备受信任镜像；运行时不会自动 pull
docker pull python:3.13-slim

codeforge --shell-backend docker \
  --docker-image python:3.13-slim \
  --shell-workspace-mode read-only

# 仅在明确接受宿主机执行风险时使用；不会由 auto 或故障回退触发
codeforge --shell-backend unsafe-host
```

Windows 上启动时会探测 Docker Engine。Docker Desktop 已安装但未运行时，默认尝试启动并在 `--docker-startup-timeout` 内等待；可用 `--no-docker-auto-start` 禁止。退出时只清理本 CodeForge 实例创建且仍被跟踪的容器，不关闭 Docker Desktop，也不碰其他项目容器。

Docker 显著缩小了进程可见的宿主资源，但不是绝对安全边界：仍需信任 Docker Engine、容器镜像、宿主内核与 Docker Desktop 的挂载实现。工作区设为 `read-write` 时，容器内命令仍可破坏已挂载文件；因此默认是 `read-only`，并额外禁止明显的整盘、根目录、工作区递归删除和 Docker Socket/特权逃逸操作。

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

## 模型接口与 Prompt Cache

生产路径只保留一个 `OpenAICompatibleModelClient`，使用 OpenAI-compatible Chat Completions 请求格式。`FakeModelClient` 仅用于测试和 Benchmark。

```python
complete(prompt, max_new_tokens, ...) -> str
```

切换 DeepSeek、OpenAI 或其他兼容 endpoint 时，只替换 `base_url / api_key / model`，Runtime 不再维护 provider 分支。Chat Completions API 的原生 `function_call` 会被统一转换成 CodeForge 内部的工具调用格式，因此工具审批、参数校验、Trace 和 Memory 链路保持不变。

Prompt Cache 采用两层策略：

- **稳定前缀**：规则、工具签名与工作区事实尽量保持在 Prompt 前部，Prefix Hash 作为缓存身份记录到 Trace。
- **服务端缓存**：默认不发送厂商专用缓存字段，依赖兼容服务自身的 Prompt / Context Cache；DeepSeek 会按重复前缀自动缓存。对明确支持 `prompt_cache_key` 的 endpoint，可手动切到 `explicit` 模式。
- **真实命中统计**：统一解析 `prompt_tokens_details.cached_tokens` / `input_tokens_details.cached_tokens`，并兼容 `prompt_cache_hit_tokens / prompt_cache_miss_tokens`，把 `cached_tokens`、`cache_hit` 和缓存策略写入运行元数据。

因此这里的 Prompt Cache 不是 CodeForge 在本地伪造 KV Cache，而是 **Harness 对服务端 Prompt/Context Cache 的稳定前缀优化、能力适配与命中观测**。

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

项目内置固定 Benchmark 与指标框架，覆盖工具恢复、路径边界、重复读取、上下文压缩、记忆依赖、Checkpoint/Resume、安全场景以及不同模型配置的回归对比。

Benchmark 使用隔离的 fixture workspace 与 verifier 来判断最终产物，而不是只看模型有没有返回“完成”。

## 设计文档

更完整的实现说明、数据流和模块细节见：

[codeforge-实现逻辑与设计文档.md](codeforge-实现逻辑与设计文档.md)

## 设计原则

CodeForge 更关注一个 Coding Agent 在真实仓库里能否 **受约束地执行、连续地工作、在状态变化后正确恢复，并留下可解释的运行证据**。

> 受约束的能力，比不可审计的“万能能力”更可靠。
