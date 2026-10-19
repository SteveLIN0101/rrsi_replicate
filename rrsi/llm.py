# Copyright 2026 The rrsi Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""OpenAI-compatible Chat Completions for search roles and the workspace judge."""

from __future__ import annotations

import os
import re
import threading
import time

import httpx
from openai import APIError, APIStatusError, BadRequestError, OpenAI

from .env import model_name, require_openai_env

MAX_TOKENS = 20_000

_client: OpenAI | None = None
_client_lock = threading.Lock()
_JSON_SUFFIX = ("\n\nOutput ONLY a single valid JSON object. No prose before or "
                "after, no markdown fences.")


def _get_client() -> OpenAI:
    global _client
    require_openai_env()
    with _client_lock:
        if _client is None:
            # This module owns retries so SDK retries cannot multiply attempts.
            _client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"],
                             api_key=os.environ["OPENAI_API_KEY"],
                             timeout=600, max_retries=0)
        return _client


def extract_json(text: str) -> str:
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    if not t.startswith("{") and not t.startswith("["):
        start = min([i for i in (t.find("{"), t.find("[")) if i != -1], default=-1)
        if start != -1:
            t = t[start:]
    if t and t[0] == "{" and not t.endswith("}"):
        end = t.rfind("}")
        if end != -1:
            t = t[:end + 1]
    if t and t[0] == "[" and not t.endswith("]"):
        end = t.rfind("]")
        if end != -1:
            t = t[:end + 1]
    return t


def generate(prompt: str, system: str | None = None, max_retries: int = 6,
             json_only: bool = False, model: str | None = None,
             max_tokens: int = MAX_TOKENS, cache_prefix: str | None = None,
             temperature: float | None = None) -> str:
    if max_retries < 1:
        raise ValueError("max_retries must be at least 1")
    client = _get_client()
    mdl = model or model_name()
    sys_prompt = (system or "") + (_JSON_SUFFIX if json_only else "")
    content = f"{cache_prefix}\n\n{prompt}" if cache_prefix else prompt
    messages = []
    if sys_prompt:
        messages.append({"role": "system", "content": sys_prompt})
    messages.append({"role": "user", "content": content})
    kwargs = {"model": mdl, "max_tokens": max_tokens, "messages": messages, "stream": True}
    if temperature is not None:
        kwargs["temperature"] = temperature
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            # Some compatible endpoints accept only the newer token-limit name.
            try:
                resp = client.chat.completions.create(**kwargs)
            except BadRequestError as e:
                detail = str(e).lower()
                if "max_tokens" in kwargs and "max_tokens" in detail and "max_completion_tokens" in detail:
                    kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                    resp = client.chat.completions.create(**kwargs)
                else:
                    raise
            parts, finish_reason = [], None
            with resp:
                for chunk in resp:
                    if chunk.choices:
                        choice = chunk.choices[0]
                        parts.append(choice.delta.content or "")
                        if choice.finish_reason:
                            finish_reason = choice.finish_reason
            if finish_reason == "length":
                raise ValueError("Model response exceeded the output token limit")
            text = "".join(parts)
            if finish_reason and text.strip():
                return extract_json(text) if json_only else text
            last_err = RuntimeError("empty or incomplete response stream")
        except APIStatusError as e:
            if e.status_code not in {408, 409, 429} and e.status_code < 500:
                raise
            last_err = e
        except (APIError, httpx.TransportError) as e:
            last_err = e
        if attempt + 1 < max_retries:
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"generate failed after {max_retries} tries: {last_err}")


if __name__ == "__main__":
    print(generate("Reply with exactly: OK", max_tokens=256))
