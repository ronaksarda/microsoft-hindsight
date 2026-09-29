"""Async client for the Vectorize Hindsight memory API.

Endpoint paths and payload shapes follow the Hindsight OpenAPI spec (v0.10.1,
``hindsight-docs/static/openapi.json`` in github.com/vectorize-io/hindsight):

* ``PUT  /v1/default/banks/{bank_id}``                 create / update a bank
* ``POST /v1/default/banks/{bank_id}/memories``        retain (``RetainRequest``)
* ``POST /v1/default/banks/{bank_id}/memories/recall`` recall (``RecallRequest``)
* ``DELETE /v1/default/banks/{bank_id}/documents/{id}`` delete one document
* ``GET  /health``                                      liveness of the service

Auth is ``Authorization: Bearer <key>``. Metadata values must be strings.

The local store is the system of record; Hindsight is the semantic memory layer.
This module therefore never touches local persistence. The sync service
(``app.memory_sync``) decides what to retain and queues failures in the outbox.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import settings

logger = logging.getLogger("grantanchor.hindsight")


# --------------------------------------------------------------------------- #
# Schemas (subset of the OpenAPI spec that GrantAnchor uses)
# --------------------------------------------------------------------------- #


class MemoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)
    timestamp: str | None = None
    context: str | None = None
    metadata: dict[str, str] | None = None
    document_id: str | None = None
    tags: list[str] | None = None
    update_mode: Literal["replace", "append"] | None = None


class RetainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    items: list[MemoryItem] = Field(min_length=1)
    async_: bool = Field(default=False, alias="async")


class RetainResponse(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    success: bool
    bank_id: str
    items_count: int
    async_: bool = Field(alias="async")
    operation_id: str | None = None


class RecallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    budget: Literal["low", "mid", "high"] = "mid"
    max_tokens: int = Field(default=4096, gt=0)
    types: list[str] | None = None
    tags: list[str] | None = None
    tags_match: Literal["any", "all", "any_strict", "all_strict", "exact"] = "any"
    query_timestamp: str | None = None


class RecallResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    text: str
    type: str | None = None
    context: str | None = None
    occurred_start: str | None = None
    occurred_end: str | None = None
    mentioned_at: str | None = None
    document_id: str | None = None
    metadata: dict[str, str] | None = None
    tags: list[str] | None = None


class RecallResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    results: list[RecallResult]


class ReflectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    budget: Literal["low", "mid", "high"] = "low"
    max_tokens: int = Field(default=2048, gt=0)
    tags: list[str] | None = None
    tags_match: Literal["any", "all", "any_strict", "all_strict", "exact"] = "any"
    tag_groups: list[dict[str, Any]] | None = None
    response_schema: dict[str, Any] | None = None


class ReflectResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    text: str
    structured_output: dict[str, Any] | None = None
    structured_output_error: str | None = None


class HindsightError(Exception):
    """Raised for any failed Hindsight call. ``retryable`` drives the outbox."""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = True):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class HindsightDisabled(HindsightError):
    def __init__(self) -> None:
        super().__init__("Hindsight is not configured", retryable=False)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class HindsightClient:
    """Thin, schema-validated Hindsight REST client with a per-bank circuit breaker."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        bank_id: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url if base_url is not None else settings.hindsight_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.hindsight_api_key
        self.bank_id = bank_id or settings.hindsight_bank_id
        self.timeout = timeout if timeout is not None else settings.hindsight_timeout_seconds
        self._transport = transport
        self._cooldown_until = 0.0
        self._bank_ready = False

    # -- helpers ----------------------------------------------------------- #

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def circuit_open(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def trip(self, seconds: float) -> None:
        self._cooldown_until = time.monotonic() + seconds

    def reset_circuit(self) -> None:
        self._cooldown_until = 0.0

    def _bank_path(self, suffix: str = "") -> str:
        return f"/v1/default/banks/{self.bank_id}{suffix}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=timeout or self.timeout, transport=self._transport)

    async def _request(
        self, method: str, path: str, json: Any = None, *, trip_on_fail: bool = True, timeout: float | None = None
    ) -> httpx.Response:
        if not self.enabled:
            raise HindsightDisabled()
        if self.circuit_open:
            raise HindsightError("circuit open", retryable=True)
        try:
            async with self._client(timeout) as client:
                resp = await client.request(method, path, json=json, headers=self._headers())
        except httpx.HTTPError as exc:
            if trip_on_fail:
                self.trip(30.0)
            raise HindsightError(f"transport error: {exc.__class__.__name__}: {exc}") from exc
        if resp.status_code >= 400:
            retryable = resp.status_code in (408, 409, 425, 429) or resp.status_code >= 500
            if trip_on_fail and resp.status_code in (401, 402, 403):
                self.trip(300.0)
            elif trip_on_fail and retryable:
                self.trip(30.0)
            raise HindsightError(
                f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
                retryable=retryable,
            )
        return resp

    # -- API --------------------------------------------------------------- #

    async def ensure_bank(self) -> None:
        """Create the bank if needed (PUT is create-or-update, so this is idempotent)."""
        if self._bank_ready:
            return
        await self._request(
            "PUT",
            self._bank_path(),
            json={"name": self.bank_id, "mission": "Grant compliance ledger: spend, rules and milestones."},
        )
        self._bank_ready = True

    async def retain(self, items: list[MemoryItem], *, async_: bool = False) -> RetainResponse:
        body = RetainRequest(items=items, async_=async_).model_dump(by_alias=True, exclude_none=True)
        try:
            resp = await self._request("POST", self._bank_path("/memories"), json=body, trip_on_fail=False)
        except HindsightError as exc:
            if exc.status_code == 404:
                # Bank missing: create it and retry once.
                self._bank_ready = False
                await self.ensure_bank()
                resp = await self._request("POST", self._bank_path("/memories"), json=body)
            else:
                if exc.status_code is None or exc.status_code >= 500:
                    self.trip(30.0)
                raise
        try:
            return RetainResponse.model_validate(resp.json())
        except (ValidationError, ValueError) as exc:
            raise HindsightError(f"unexpected retain response: {exc}", retryable=False) from exc

    async def recall(
        self,
        query: str,
        *,
        tags: list[str] | None = None,
        max_tokens: int | None = None,
        budget: Literal["low", "mid", "high"] | None = None,
    ) -> list[RecallResult]:
        req = RecallRequest(
            query=query,
            tags=tags,
            tags_match="any_strict" if tags else "any",
            max_tokens=max_tokens or settings.hindsight_recall_max_tokens,
            budget=budget or settings.hindsight_recall_budget,
        )
        try:
            resp = await self._request(
                "POST", self._bank_path("/memories/recall"), json=req.model_dump(exclude_none=True), trip_on_fail=False
            )
        except HindsightError as exc:
            if exc.status_code == 404:
                return []  # bank does not exist yet: nothing to recall
            if exc.status_code is None or exc.status_code >= 500:
                self.trip(30.0)
            raise
        try:
            return RecallResponse.model_validate(resp.json()).results
        except (ValidationError, ValueError) as exc:
            raise HindsightError(f"unexpected recall response: {exc}", retryable=False) from exc

    async def reflect(
        self,
        query: str,
        *,
        tags: list[str] | None = None,
        tag_groups: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> ReflectResponse:
        """Ask Hindsight to reason over the bank's memories (``POST .../reflect``). Slower than recall (~10 s)."""
        req = ReflectRequest(query=query, tags=tags, tags_match="any_strict" if tags else "any",
                             tag_groups=tag_groups, response_schema=response_schema)
        resp = await self._request("POST", self._bank_path("/reflect"), json=req.model_dump(exclude_none=True),
                                   trip_on_fail=False, timeout=max(self.timeout, 60.0))
        try:
            return ReflectResponse.model_validate(resp.json())
        except (ValidationError, ValueError) as exc:
            raise HindsightError(f"unexpected reflect response: {exc}", retryable=False) from exc

    async def delete_document(self, document_id: str) -> None:
        try:
            await self._request("DELETE", self._bank_path(f"/documents/{document_id}"), trip_on_fail=False)
        except HindsightError as exc:
            if exc.status_code == 404:
                return  # already gone
            raise

    async def health(self) -> dict[str, Any]:
        """Return service health. Does not require the bank to exist."""
        if not self.enabled:
            return {"status": "disabled"}
        try:
            async with self._client() as client:
                resp = await client.get("/health", headers=self._headers())
            return {"status": "ok" if resp.status_code == 200 else "error", "http_status": resp.status_code}
        except httpx.HTTPError as exc:
            return {"status": "error", "error": exc.__class__.__name__}


def build_memory_item(record: dict[str, Any]) -> MemoryItem:
    """Map a local ledger record to a Hindsight MemoryItem (string metadata only)."""
    meta = {
        "memory_id": str(record.get("id", "")),
        "grant_id": str(record.get("grant_id", "")),
        "spender": str(record.get("spender", "")),
        "category": str(record.get("category", "")),
        "amount": f"{float(record.get('amount', 0.0)):.2f}",
        "vendor": str(record.get("vendor", "")),
        "location": str(record.get("location", "")),
        "source": "grantanchor",
    }
    tags = ["grantanchor", f"grant:{record.get('grant_id', '')}"]
    if record.get("category"):
        tags.append(f"category:{str(record['category']).lower()}")
    return MemoryItem(
        content=str(record.get("content") or f"{meta['spender']} spent {meta['amount']} at {meta['vendor']}"),
        timestamp=record.get("timestamp") or None,
        context=f"grant-ledger:{record.get('grant_id', '')}",
        metadata=meta,
        document_id=str(record.get("id")) if record.get("id") else None,
        tags=tags,
        update_mode="replace",
    )


_default_client: HindsightClient | None = None


def get_client() -> HindsightClient:
    global _default_client
    if _default_client is None:
        _default_client = HindsightClient()
    return _default_client


def set_client(client: HindsightClient | None) -> None:
    """Swap the process-wide client (tests, or after settings change)."""
    global _default_client
    _default_client = client


# --------------------------------------------------------------------------- #
# Backward-compatible module functions (used by earlier callers and tests)
# --------------------------------------------------------------------------- #


async def retain(
    bank_id: str,
    content: str,
    context: str = "",
    timestamp: str = "",
    metadata: dict[str, Any] | None = None,
    grant_id: str = "",
    spender: str = "",
    category: str = "",
    amount: float = 0.0,
    vendor: str = "",
) -> dict[str, Any]:
    """Persist locally and sync to Hindsight (queued in the outbox on failure)."""
    from app import memory_sync

    meta = metadata or {}
    return await memory_sync.record_memory(
        grant_id=grant_id or meta.get("grant_id") or "",
        spender=spender or meta.get("spender", "System"),
        category=category or meta.get("category", "General"),
        amount=amount or float(meta.get("amount", 0.0)),
        vendor=vendor or meta.get("vendor", ""),
        content=content,
        timestamp=timestamp or None,
        context=context,
    )


async def recall(bank_id: str, query: str, top_k: int = 10, grant_id: str = "") -> list[dict[str, Any]]:
    """Recall memories: Hindsight first, local keyword ranking as fallback."""
    from app import memory_sync

    return await memory_sync.recall_memories(query=query, top_k=top_k, grant_id=grant_id)


def clear_bank(bank_id: str) -> None:
    """Reset the circuit breaker (kept for callers that expect this helper)."""
    get_client().reset_circuit()
