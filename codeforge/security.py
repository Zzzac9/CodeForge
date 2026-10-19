"""Security decisions shared by the runtime and file tools.

This module deliberately separates three different questions:

* the registry's static ``risky`` bit (capability-level risk),
* an operation-specific risk assessment (arguments and targets), and
* mandatory safety boundaries (which approval can never override).

The path helpers reject symlinks/reparse points in tool paths.  That is more
restrictive than resolving symlinks that happen to remain inside the workspace,
but it avoids a large class of link-swap and junction escape problems.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


@dataclass(frozen=True)
class RiskAssessment:
    level: str
    summary: str
    target_paths: tuple[str, ...] = ()
    possible_impacts: tuple[str, ...] = ()
    unknown_impact: bool = False
    prohibited: bool = False
    denial_reason: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class SafetyDecision:
    passed: bool
    reason: str = ""


# These patterns cover operations for which a shell string is not an acceptable
# interface, even inside the normal Docker boundary.  This is defense in depth;
# it is not presented as the sandbox itself.
PROHIBITED_SHELL_PATTERNS = (
    (re.compile(r"(?is)(?:^|[;&|]\s*)\s*rm\s+(?:-[^\s]*r[^\s]*f|-[^\s]*f[^\s]*r)\s+(?:/|/\*|/workspace(?:/\*)?|\.|\.\/\*|\$\{?HOME\}?|~)(?:\s|$)"), "recursive deletion of a filesystem, home, or workspace root"),
    (re.compile(r"(?is)\b(?:mkfs(?:\.[a-z0-9]+)?|fdisk|parted)\b"), "disk/filesystem modification"),
    (re.compile(r"(?is)\b(?:shutdown|reboot|halt|poweroff)\b"), "host or system shutdown"),
    (re.compile(r"(?is)\b(?:diskpart|format(?:\.com)?)\b"), "Windows disk/filesystem modification"),
    (re.compile(r"(?is)\bRemove-Item\b[^\r\n]*\b-Recurse\b[^\r\n]*(?:[A-Za-z]:\\(?:\s|$)|\\\\|\$env:(?:USERPROFILE|SystemDrive))"), "recursive deletion of a Windows root"),
    (re.compile(r"(?is)(?:/var/run/docker\.sock|docker_engine|\\\\\.\\pipe\\docker_engine)"), "Docker daemon/socket access"),
    (re.compile(r"(?is)(?:--privileged|--pid\s*=\s*host|--network\s*=\s*host)"), "privileged or host-namespace execution"),
    (re.compile(r"(?is):\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "process fork bomb"),
    (re.compile(r"(?is)>\s*/dev/(?:sd[a-z]|nvme\d+n\d+|vd[a-z])"), "raw block-device overwrite"),
    (re.compile(r"(?is)\bgit\s+(?:clean\s+[^\r\n]*(?:-[^\s]*f|--force)|reset\s+--hard)\b"), "destructive whole-worktree Git operation"),
)


SHELL_MUTATION_PATTERN = re.compile(
    r"(?is)(?:^|[;&|]\s*)(?:rm|mv|cp|mkdir|rmdir|touch|sed\s+-i|git\s+(?:clean|reset|checkout)|"
    r"del|erase|move|copy|ren|rename|Remove-Item|Move-Item|Copy-Item|Set-Content|Add-Content)\b"
)


def _higher_risk(left: str, right: str) -> str:
    return left if RISK_ORDER[left] >= RISK_ORDER[right] else right


def assess_tool_risk(name, args, tool_spec, root, *, shell_backend="docker") -> RiskAssessment:
    """Return an argument-aware assessment without granting permission."""
    args = args or {}
    static_level = "high" if tool_spec.get("risky") else "low"
    targets = []
    impacts = []
    unknown = False
    prohibited = False
    denial_reason = ""
    summary = tool_spec.get("description", name)
    level = static_level

    if name in {"list_files", "read_file", "search", "write_file", "patch_file"}:
        raw_path = str(args.get("path", "."))
        try:
            target = secure_workspace_path(root, raw_path, allow_missing=name == "write_file")
            targets.append(display_workspace_path(root, target))
        except Exception:
            # Validation reports the precise error.  The risk record still makes
            # it explicit that the requested target could not be trusted.
            targets.append(raw_path)
            unknown = True

    if name == "write_file":
        impacts.append("creates or atomically replaces one workspace file")
    elif name == "patch_file":
        impacts.append("atomically replaces one uniquely matched text block")
    elif name == "run_shell":
        command = str(args.get("command", ""))
        targets.append(display_workspace_path(root, Path(root)))
        if shell_backend == "docker":
            impacts.append("executes a command in a restricted temporary Docker container")
        elif shell_backend == "unsafe-host":
            impacts.append("executes directly on the host without sandbox isolation")
        else:
            impacts.append("requests shell execution while the shell backend is disabled")
        unknown = True
        if SHELL_MUTATION_PATTERN.search(command):
            impacts.append("may modify files when the workspace mount is writable")
        for pattern, reason in PROHIBITED_SHELL_PATTERNS:
            if pattern.search(command):
                level = "critical"
                prohibited = True
                denial_reason = reason
                impacts.append(reason)
                break

    return RiskAssessment(
        level=_higher_risk(static_level, level),
        summary=summary,
        target_paths=tuple(dict.fromkeys(targets)),
        possible_impacts=tuple(dict.fromkeys(impacts)),
        unknown_impact=unknown,
        prohibited=prohibited,
        denial_reason=denial_reason,
    )


def mandatory_safety_check(name, assessment, *, shell_backend, read_only=False):
    """Enforce boundaries that approval (including ``auto``) cannot bypass."""
    if read_only and name in {"run_shell", "write_file", "patch_file"}:
        return SafetyDecision(False, "runtime is read-only")
    if assessment.prohibited:
        return SafetyDecision(False, assessment.denial_reason or "operation is prohibited")
    if name == "run_shell" and shell_backend == "disabled":
        return SafetyDecision(False, "shell execution is disabled")
    if name == "run_shell" and shell_backend not in {"docker", "unsafe-host"}:
        return SafetyDecision(False, f"unsupported shell backend: {shell_backend}")
    return SafetyDecision(True)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def secure_workspace_path(root, raw_path, *, allow_missing=False):
    """Resolve a workspace path and reject escapes and link/reparse traversal.

    Existing path components are checked both before and after resolution.  The
    caller should invoke this immediately before I/O; write helpers below repeat
    the check before the final atomic replace.
    """
    root = Path(root).resolve()
    raw = os.fspath(raw_path)
    if not raw or "\x00" in raw:
        raise ValueError("path must not be empty or contain NUL")
    supplied = Path(raw)
    lexical = supplied if supplied.is_absolute() else root / supplied
    lexical = Path(os.path.abspath(os.fspath(lexical)))
    try:
        if os.path.commonpath([os.fspath(root), os.fspath(lexical)]) != os.fspath(root):
            raise ValueError(f"path escapes workspace: {raw}")
    except ValueError:
        raise ValueError(f"path escapes workspace: {raw}") from None

    relative = lexical.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if _is_link_or_reparse(current):
                raise ValueError(f"symlink/reparse paths are not allowed: {raw}")
        else:
            break

    resolved = lexical.resolve(strict=False)
    try:
        if os.path.commonpath([os.fspath(root), os.fspath(resolved)]) != os.fspath(root):
            raise ValueError(f"path escapes workspace: {raw}")
    except ValueError:
        raise ValueError(f"path escapes workspace: {raw}") from None
    if not allow_missing and not resolved.exists():
        # Existing callers often provide their own file/dir error text, so only
        # missing paths are left for them to classify.
        return resolved
    return resolved


def display_workspace_path(root, path):
    root = Path(root).resolve()
    path = Path(path).resolve(strict=False)
    try:
        value = path.relative_to(root).as_posix()
        return value or "."
    except ValueError:
        return os.fspath(path)


def read_text_secure(root, raw_path, *, errors="strict"):
    path = secure_workspace_path(root, raw_path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8", errors=errors) as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def atomic_write_text(root, raw_path, content):
    """Write UTF-8 in the target directory, fsync, then atomically replace."""
    root = Path(root).resolve()
    path = secure_workspace_path(root, raw_path, allow_missing=True)
    parent_raw = os.path.relpath(path.parent, root)
    parent = secure_workspace_path(root, parent_raw, allow_missing=True)
    parent.mkdir(parents=True, exist_ok=True)
    parent = secure_workspace_path(root, parent_raw)
    if not parent.is_dir():
        raise ValueError("parent path is not a directory")

    previous_mode = None
    if path.exists():
        path = secure_workspace_path(root, raw_path)
        if path.is_dir():
            raise ValueError("path is a directory")
        previous_mode = stat.S_IMODE(path.stat().st_mode)

    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            delete=False,
            dir=os.fspath(parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temp_name = handle.name
            handle.write(str(content))
            handle.flush()
            os.fsync(handle.fileno())
        if previous_mode is not None:
            os.chmod(temp_name, previous_mode)
        # Repeat boundary/link checks immediately before the commit point.
        path = secure_workspace_path(root, raw_path, allow_missing=True)
        secure_workspace_path(root, os.path.relpath(path.parent, root))
        if path.exists() and _is_link_or_reparse(path):
            raise ValueError(f"symlink/reparse paths are not allowed: {raw_path}")
        os.replace(temp_name, path)
        temp_name = None
        return path
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def render_approval_request(name, args, assessment, redact):
    safe_args = redact(args)
    targets = ", ".join(assessment.target_paths) or "(none identified)"
    impacts = "; ".join(assessment.possible_impacts) or "unknown"
    unknown = "yes" if assessment.unknown_impact else "no"
    return (
        "\nTool approval required\n"
        f"- tool: {name}\n"
        f"- full arguments: {json.dumps(safe_args, ensure_ascii=False, sort_keys=True)}\n"
        f"- target paths: {targets}\n"
        f"- risk level: {assessment.level}\n"
        f"- possible impact: {impacts}\n"
        f"- unknown impact: {unknown}\n"
        "Approve this operation? [y/N] "
    )
