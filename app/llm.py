import json
import time

import httpx

from . import config


class LLMError(Exception):
    pass


RETRYABLE = {429, 500, 502, 503, 504}


def complete_json(system, payload, max_tokens=8000):
    if not config.LLM_API_KEY:
        raise LLMError("LLM_API_KEY is not set on the server")

    body = {
        "model": config.LLM_MODEL,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }
    if "gpt-oss" in config.LLM_MODEL:
        body["reasoning_effort"] = "low"
    headers = {"Authorization": f"Bearer {config.LLM_API_KEY}"}
    last = LLMError("no attempt was made")

    wait = 0.0
    for i in range(5):
        if i:
            time.sleep(wait or 2.0 * i)
        wait = 0.0
        try:
            res = httpx.post(
                f"{config.LLM_BASE_URL}/chat/completions",
                json=body,
                headers=headers,
                timeout=config.LLM_TIMEOUT,
            )
        except httpx.HTTPError as err:
            last = LLMError(f"network error talking to the model: {err}")
            continue

        if res.status_code in RETRYABLE:
            last = LLMError(f"model provider returned {res.status_code}")
            if res.status_code == 429:
                try:
                    wait = min(float(res.headers.get("retry-after", 0)), 30.0) + 1.0
                except (TypeError, ValueError):
                    wait = 15.0
            continue
        if res.status_code == 400 and "json_validate_failed" in res.text:
            last = LLMError("model produced invalid JSON")
            continue
        if res.status_code >= 400:
            raise LLMError(f"model provider returned {res.status_code}: {res.text[:300]}")

        data = res.json()
        try:
            text = data["choices"][0]["message"]["content"]
            parsed = json.loads(text)
        except (KeyError, IndexError, json.JSONDecodeError):
            last = LLMError("model did not return valid JSON")
            continue

        usage = data.get("usage") or {}
        return parsed, {
            "in": usage.get("prompt_tokens", 0),
            "out": usage.get("completion_tokens", 0),
        }

    raise last