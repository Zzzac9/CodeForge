# Agent Harness v1 overview

CodeForge keeps the model behind a deterministic runtime boundary: the Tool Registry declares capabilities, the Runtime Loop validates and executes them, Memory preserves reusable facts, Checkpoint records resumable task state, and Trace/Report retain an auditable history.

Tool execution follows `validation → operation risk analysis → mandatory safety checks → approval → execution`. Approval never replaces a safety check. File tools stay inside the fixed workspace boundary and use atomic replacement for writes. Shell tools use a per-call restricted Docker container by default and fail closed when Docker or the configured local image is unavailable; `unsafe-host` is an explicit non-sandboxed compatibility mode.

The detailed security boundary, lifecycle, residual risks, and rollout phases are documented in [the implementation design](../../codeforge-%E5%AE%9E%E7%8E%B0%E9%80%BB%E8%BE%91%E4%B8%8E%E8%AE%BE%E8%AE%A1%E6%96%87%E6%A1%A3.md#12-%E5%AE%89%E5%85%A8%E8%BE%B9%E7%95%8C).
