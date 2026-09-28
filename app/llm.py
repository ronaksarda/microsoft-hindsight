"""Groq chat-completions client (OpenAI-compatible) used for advisory analysis only.

Behaviour verified against console.groq.com/docs:

* ``POST {base}/chat/completions`` with ``response_format={"type": "json_object"}``
  (JSON mode). The prompt must itself ask for JSON.
* JSON mode guarantees syntactically valid JSON, not a schema. Callers validate.
* A generation that fails JSON validation returns HTTP 400 with
  ``error.code == "json_validate_failed"``. It is intermittent, so we retry it once.
* 429 (rate limited), 498 (flex capacity) and 5xx are retried with exponential
  backoff and full jitter. ``retry-after`` (seconds) is honoured when present.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from app.config import settings

logger = logging.getLogger("grantanchor.llm")

RETRYABLE_STATUS = {429, 498, 500, 502, 503, 504}


class LLMError(Exception):
    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind  # disabled | timeout | rate_limited | http | malformed | transport


@dataclass
class LLMResult:
    data: dict[str, Any]
    model: str
    attempts: int


Sleeper = Callable[[float], Awaitable[None]]


def backoff_delay(attempt: int, *, base: float, cap: float, rng: random.Random | None = None) -> float:
    """Full-jitter exponential backoff: uniform(0, min(cap, base * 2**attempt))."""
    r = rng or random
    return r.uniform(0.0, min(cap, base * (2**attempt)))


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


class GroqClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleeper = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.groq_api_key
        self.model = model or settings.groq_model
        self.base_url = (base_url or settings.groq_base_url).rstrip("/")
        self.timeout = timeout if timeout is not None else settings.groq_timeout_seconds
        self.max_retries = max_retries if max_retries is not None else settings.groq_max_retries
        self._transport = transport
        self._sleep = sleep
        self._rng = rng

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self.timeout, connect=min(5.0, self.timeout))

    async def _wait(self, attempt: int, resp: httpx.Response | None) -> None:
        delay = backoff_delay(
            attempt, base=settings.groq_backoff_base_seconds, cap=settings.groq_backoff_max_seconds, rng=self._rng
        )
        if resp is not None:
            hinted = _retry_after_seconds(resp)
            if hinted is not None:
                delay = min(max(delay, hinted), settings.groq_backoff_max_seconds)
        await self._sleep(delay)

    async def complete_json(self, system_prompt: str, user_prompt: str, *, max_tokens: int = 1024) -> LLMResult:
        """Return the parsed JSON object from the model, or raise ``LLMError``."""
        if not self.enabled:
            raise LLMError("GROQ_API_KEY not configured", kind="disabled")

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        json_retry_used = False
        last_error: LLMError | None = None

        async with httpx.AsyncClient(timeout=self._timeout(), transport=self._transport) as client:
            for attempt in range(self.max_retries + 1):
                resp: httpx.Response | None = None
                try:
                    resp = await client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
                except httpx.TimeoutException as exc:
                    last_error = LLMError(f"timeout: {exc.__class__.__name__}", kind="timeout")
                except httpx.HTTPError as exc:
                    last_error = LLMError(f"transport: {exc.__class__.__name__}", kind="transport")
                else:
                    if resp.status_code == 200:
                        return LLMResult(data=self._parse(resp), model=self.model, attempts=attempt + 1)
                    if resp.status_code == 400 and self._is_json_validate_failed(resp) and not json_retry_used:
                        json_retry_used = True
                        last_error = LLMError("json_validate_failed", kind="malformed")
                        continue  # immediate single retry, no backoff
                    if resp.status_code not in RETRYABLE_STATUS:
                        raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}", kind="http")
                    kind = "rate_limited" if resp.status_code == 429 else "http"
                    last_error = LLMError(f"HTTP {resp.status_code}", kind=kind)

                if attempt < self.max_retries:
                    logger.info("groq retry", extra={"attempt": attempt + 1, "reason": str(last_error)})
                    await self._wait(attempt, resp)

        assert last_error is not None
        raise last_error

    @staticmethod
    def _is_json_validate_failed(resp: httpx.Response) -> bool:
        try:
            return resp.json().get("error", {}).get("code") == "json_validate_failed"
        except ValueError:
            return False

    @staticmethod
    def _parse(resp: httpx.Response) -> dict[str, Any]:
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected response envelope: {exc}", kind="malformed") from exc
        try:
            data = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise LLMError("model content is not valid JSON", kind="malformed") from exc
        if not isinstance(data, dict):
            raise LLMError("model JSON is not an object", kind="malformed")
        return data

    async def ping(self) -> dict[str, Any]:
        """Cheap readiness probe: GET /models (no tokens spent)."""
        if not self.enabled:
            return {"status": "disabled"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0), transport=self._transport) as client:
                resp = await client.get(f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"})
            return {"status": "ok" if resp.status_code == 200 else "error", "http_status": resp.status_code}
        except httpx.HTTPError as exc:
            return {"status": "error", "error": exc.__class__.__name__}


_default: GroqClient | None = None


def get_client() -> GroqClient:
    global _default
    if _default is None:
        _default = GroqClient()
    return _default


def set_client(client: GroqClient | None) -> None:
    global _default
    _default = client
