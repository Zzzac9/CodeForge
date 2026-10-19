"""Environment-file configuration helpers.

The CodeForge installation ``.env`` supplies portable defaults.  A target
project may override those defaults with its own ``.env`` without changing
where that project's runtime state is stored.
"""

import os
import re
from pathlib import Path


ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NORMALIZED_ENV_ALIASES = {
    "CODEFORGE_API_BASE": (
        "CODEFORGE_DEEPSEEK_API_BASE",
        "DEEPSEEK_API_BASE",
        "CODEFORGE_OPENAI_API_BASE",
        "OPENAI_API_BASE",
    ),
    "CODEFORGE_API_KEY": (
        "CODEFORGE_DEEPSEEK_API_KEY",
        "DEEPSEEK_API_KEY",
        "CODEFORGE_OPENAI_API_KEY",
        "OPENAI_API_KEY",
    ),
    "CODEFORGE_MODEL": (
        "CODEFORGE_DEEPSEEK_MODEL",
        "DEEPSEEK_MODEL",
        "CODEFORGE_OPENAI_MODEL",
        "OPENAI_MODEL",
    ),
}


def _strip_quotes(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_env_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].strip()
    if "=" not in line:
        raise ValueError(f"invalid .env line: {line}")
    name, value = line.split("=", 1)
    name = name.strip()
    if not ENV_KEY_PATTERN.match(name):
        raise ValueError(f"invalid .env variable name: {name}")
    return name, _strip_quotes(value)


def find_project_env(start):
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for path in (current, *current.parents):
        env_path = path / ".env"
        if env_path.exists():
            return env_path
    return None


def codeforge_root():
    """Return the installation root without relying on a machine-specific path."""
    return Path(__file__).resolve().parent.parent


def find_codeforge_env():
    """Return the installation-level default env file when it exists."""
    env_path = codeforge_root() / ".env"
    return env_path if env_path.is_file() else None


def _load_env_file(env_path, override):
    loaded = {}
    if env_path is None:
        return loaded
    for line in Path(env_path).read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        name, value = parsed
        loaded[name] = value
    # Normalize model aliases inside each layer before applying it.  This is
    # important for source priority: a target project's OPENAI_API_KEY must
    # override an installation-level CODEFORGE_API_KEY, not lose merely because
    # provider_env checks the canonical name first.
    for canonical, aliases in NORMALIZED_ENV_ALIASES.items():
        if canonical in loaded:
            continue
        for alias in aliases:
            if loaded.get(alias):
                loaded[canonical] = loaded[alias]
                break
    for name, value in loaded.items():
        if override or name not in os.environ:
            os.environ[name] = value
    return loaded


def load_project_env(start, override=True):
    """Load system < installation .env < target project .env.

    ``override=True`` is the CLI path and establishes the documented layer
    order.  The project and installation file may be the same file when
    CodeForge is run against its own source tree; it is loaded only once.
    """
    installation_env = find_codeforge_env()
    project_env = find_project_env(start)
    loaded = _load_env_file(installation_env, override=override)
    if project_env is not None and (
        installation_env is None or project_env.resolve() != installation_env.resolve()
    ):
        loaded.update(_load_env_file(project_env, override=override))
    return loaded


def provider_env(name, legacy_names=(), default=""):
    for env_name in (name, *legacy_names):
        value = os.environ.get(env_name)
        if value:
            return value
    return default


def provider_bool(name, legacy_names=(), default=False):
    value = provider_env(name, legacy_names, "")
    if value == "":
        return bool(default)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def provider_int(name, legacy_names=(), default=0):
    value = provider_env(name, legacy_names, "")
    if value == "":
        return int(default)
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None


def provider_float(name, legacy_names=(), default=0.0):
    value = provider_env(name, legacy_names, "")
    if value == "":
        return float(default)
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
