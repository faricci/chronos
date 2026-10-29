"""HTTP client for a System One backend (OpenJev or ``chronos-decider``), spec sez. 2.3 C.2.

One long-lived ``httpx.AsyncClient``, one POST per decision, no internal retry: a failure becomes a typed exception and
the engine decides what to do within the bar budget (HOLD or baseline fallback). The confidence in the answers is always
recomputed locally (``protocol``), never read from the backend.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import httpx
from pydantic import ValidationError

from modules.decision.protocol import (
    DeciderBadResponse,
    DeciderTimeout,
    DeciderUnavailable,
    DecisionAnswers,
    SystemOneRequest,
    SystemOneResponse,
)
from modules.decision.questions import QuestionSet

__all__ = ["DecideResult", "SystemOneClient", "parse_server_timing"]

_TIMING = re.compile(r"(?P<name>[A-Za-z_][\w-]*)\s*;\s*dur\s*=\s*(?P<ms>[0-9]+(?:\.[0-9]+)?)")


def parse_server_timing(header: str | None) -> dict[str, float]:
    """``model;dur=41.2, server;dur=2.8, total;dur=44.0`` -> ``{"model": 41.2, ...}`` (milliseconds)."""
    if not header:
        return {}
    return {m["name"]: float(m["ms"]) for m in _TIMING.finditer(header)}


@dataclass(frozen=True)
class DecideResult:
    answers: DecisionAnswers
    model: str
    server_timing_ms: dict[str, float] = field(default_factory=dict)
    http_total_ms: float = 0.0
    input_tokens: int = 0


class SystemOneClient:
    def __init__(
        self,
        base_url: str,
        *,
        model: str,
        timeout_ms: float = 250,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_ms / 1000),
            limits=httpx.Limits(max_keepalive_connections=8),
            http2=False,
            headers=headers,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def decide(self, state: str, questions: QuestionSet) -> DecideResult:
        body = SystemOneRequest(model=self._model, state=state, questions=questions.questions)
        started = time.perf_counter()
        try:
            resp = await self._client.post("/v1/systemone", content=body.model_dump_json())
        except httpx.TimeoutException as e:
            raise DeciderTimeout(f"decider timed out: {type(e).__name__}") from e
        except httpx.HTTPError as e:
            raise DeciderUnavailable(f"decider unreachable: {type(e).__name__}: {e}") from e
        if resp.status_code != 200:
            raise DeciderUnavailable(f"decider answered HTTP {resp.status_code}")
        try:
            parsed = SystemOneResponse.model_validate_json(resp.content)
        except ValidationError as e:
            raise DeciderBadResponse(
                f"invalid decider response: {e.error_count()} error(s): {e.errors()[0]['msg']}"
            ) from e
        return DecideResult(
            answers=parsed.answers.with_recomputed_confidence(),
            model=parsed.model,
            server_timing_ms=parse_server_timing(resp.headers.get("Server-Timing")),
            http_total_ms=(time.perf_counter() - started) * 1000,
            input_tokens=parsed.usage.input_tokens,
        )
