# CodeForge review pack

## Project pitch

CodeForge is a local coding-agent harness built around constrained tools, resumable state, and inspectable evidence rather than unrestricted host access.

## Architecture map

Review the runtime as a chain of independently enforced controls:

1. Tool Registry and parameter schemas.
2. Operation-specific risk analysis and mandatory safety decisions.
3. Approval outcome, which cannot override safety boundaries.
4. File snapshots, `partial_success`, Checkpoint, Trace, and Report evidence.
5. Docker lifecycle and fail-closed behavior, with real integration results distinguished from mocked lifecycle tests.

The implementation and residual-risk discussion live in the project README and architecture/design documentation. Tests under `tests/test_tool_security.py` use disposable workspaces and include optional real-Docker coverage.

## Benchmark evidence

`tests/test_evaluator.py`, `tests/test_metrics.py`, and `tests/test_tool_security.py` cover deterministic task completion, recovery contracts, safety decisions, process cleanup, and the optional real-Docker boundary.

## Sample run artifact list

- `task_state.json`: resumable task state and stop reason.
- `trace.jsonl`: ordered model, approval, safety, tool, timeout, and checkpoint events.
- `report.json`: final outcome plus prompt, memory, sandbox, and security metadata.
