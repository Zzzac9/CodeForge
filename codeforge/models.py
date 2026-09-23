"""统一模型适配层。

CodeForge 的生产路径只保留一套 OpenAI-compatible Chat Completions 协议。
不同模型服务通过 base_url / api_key / model 切换；FakeModelClient 仅用于测试。

Prompt Cache 不在本地伪造 KV Cache：
- Harness 尽量保持 Prompt 前缀稳定；
- 兼容后端负责真正的 Prompt / Context Cache；
- CodeForge 解析 usage 中的 cached token 指标，并写入 Trace / Report。
"""

import json
import time
from http.client import RemoteDisconnected
from urllib.parse import urlparse
import urllib.error
import urllib.request

OPENAI_COMPATIBLE_USER_AGENT = "codeforge/0.1"


class FakeModelClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.supports_prompt_cache = False
        self.uses_explicit_prompt_cache = False
        self.prompt_cache_strategy = "off"
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.prompts.append(prompt)
        if not getattr(self, "last_completion_metadata", None):
            self.last_completion_metadata = {}
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        return self.outputs.pop(0)


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _cache_strategy(base_url, configured="auto"):
    """Return how CodeForge should interact with backend prompt caching."""
    mode = str(configured or "auto").strip().lower()
    if mode not in {"auto", "explicit", "automatic", "observe", "off"}:
        raise ValueError(
            "prompt cache mode must be auto, explicit, automatic, observe, or off"
        )
    if mode != "auto":
        return mode

    host = (urlparse(base_url).hostname or "").lower()
    if host == "api.deepseek.com" or host.endswith(".deepseek.com"):
        return "automatic"
    if host == "api.openai.com" or host.endswith(".openai.com"):
        return "automatic"
    return "observe"


def _chat_tools(tools):
    """Convert CodeForge tool specs to standard Chat Completions function tools."""
    rendered = []
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        if isinstance(tool.get("function"), dict):
            rendered.append(tool)
            continue
        name = str(tool.get("name", "")).strip()
        if not name:
            continue
        rendered.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": tool.get(
                        "parameters",
                        {"type": "object", "properties": {}},
                    ),
                },
            }
        )
    return rendered


def _extract_chat_tool_call(message):
    """Convert the first native Chat Completions tool call to runtime tool text."""
    for item in message.get("tool_calls") or []:
        if not isinstance(item, dict) or item.get("type") != "function":
            continue
        function = item.get("function") or {}
        name = str(function.get("name", "")).strip()
        if not name:
            continue
        raw_args = function.get("arguments", "{}")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {}
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            args = {}
        return (
            "<tool>"
            + json.dumps({"name": name, "args": args}, ensure_ascii=False)
            + "</tool>"
        )
    return ""


def _extract_chat_result(data):
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    tool_call = _extract_chat_tool_call(message)
    if tool_call:
        return tool_call

    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                return str(item["text"])
    return ""


def _extract_usage_cache_details(data):
    usage = data.get("usage") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    input_details = usage.get("input_tokens_details") or {}

    cached_tokens = int(
        prompt_details.get("cached_tokens")
        or input_details.get("cached_tokens")
        or usage.get("prompt_cache_hit_tokens")
        or 0
    )
    cache_miss_tokens = usage.get("prompt_cache_miss_tokens")
    cache_fields_reported = (
        "cached_tokens" in prompt_details
        or "cached_tokens" in input_details
        or "prompt_cache_hit_tokens" in usage
        or "prompt_cache_miss_tokens" in usage
    )

    return {
        "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
        "output_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": cached_tokens,
        "cache_miss_tokens": cache_miss_tokens,
        "cache_hit": cached_tokens > 0,
        "cache_usage_reported": cache_fields_reported,
    }


class OpenAICompatibleModelClient:
    """Single production client using OpenAI-compatible Chat Completions."""

    def __init__(
        self,
        model,
        base_url,
        api_key,
        temperature,
        timeout,
        prompt_cache_mode="auto",
    ):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.prompt_cache_strategy = _cache_strategy(
            self.base_url, prompt_cache_mode
        )
        self.supports_prompt_cache = self.prompt_cache_strategy in {
            "automatic",
            "explicit",
        }
        self.uses_explicit_prompt_cache = self.prompt_cache_strategy == "explicit"
        self.last_completion_metadata = {}

    def complete(
        self,
        prompt,
        max_new_tokens,
        prompt_cache_key=None,
        prompt_cache_retention=None,
        tools=None,
    ):
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_new_tokens,
            "stream": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        rendered_tools = _chat_tools(tools)
        if rendered_tools:
            payload["tools"] = rendered_tools
            payload["tool_choice"] = "auto"
        # Generic mode never sends vendor-specific cache fields by default.
        # Explicit mode is available only when the selected endpoint documents
        # prompt_cache_key / prompt_cache_retention compatibility.
        if self.uses_explicit_prompt_cache and prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key
        if self.uses_explicit_prompt_cache and prompt_cache_retention:
            payload["prompt_cache_retention"] = prompt_cache_retention

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": OPENAI_COMPATIBLE_USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        body_text = ""
        attempts = 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout
                ) as response:
                    body_text = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    f"OpenAI-compatible request failed with HTTP {exc.code}: {body}"
                ) from exc
            except (urllib.error.URLError, RemoteDisconnected) as exc:
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "Could not reach the OpenAI-compatible backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "OpenAI-compatible error: backend returned non-JSON content"
            ) from exc

        if data.get("error"):
            raise RuntimeError(f"OpenAI-compatible error: {data['error']}")

        self._record_completion_metadata(
            data,
            prompt_cache_key,
            prompt_cache_retention,
        )
        text = _extract_chat_result(data)
        if text:
            return text
        raise RuntimeError(
            "OpenAI-compatible error: could not extract text or tool call from response"
        )

    def _record_completion_metadata(
        self,
        data,
        prompt_cache_key,
        prompt_cache_retention,
    ):
        usage = _extract_usage_cache_details(data)
        observed_support = bool(usage.get("cache_usage_reported"))
        self.last_completion_metadata = {
            "prompt_cache_supported": self.supports_prompt_cache or observed_support,
            "prompt_cache_strategy": self.prompt_cache_strategy,
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_key_sent": bool(
                self.uses_explicit_prompt_cache and prompt_cache_key
            ),
            "prompt_cache_retention": (
                prompt_cache_retention if self.uses_explicit_prompt_cache else None
            ),
            **usage,
        }
