"""SystemOneClient against a mocked transport: valid answer, 429, 5xx, timeout, malformed JSON, bad probabilities."""

from __future__ import annotations

import json

import httpx
import pytest

from modules.decision.client import SystemOneClient, parse_server_timing
from modules.decision.protocol import (
    DeciderBadResponse,
    DeciderTimeout,
    DeciderUnavailable,
    confidence_from_probs,
)
from modules.decision.questions import build_questions

QUESTIONS = build_questions(14400, 4.0, 2.0, "1d")
GOOD = {
    "model": "m@1",
    "answers": {
        "action": {"choice": "BUY", "probabilities": {"BUY": 0.71, "SELL": 0.02, "HOLD": 0.27}, "confidence": 0.99},
        "edge": {"noul": 0.52},
        "vol_regime": {
            "score": 2.31,
            "legend": {"0": "very_low", "1": "low", "2": "normal", "3": "high", "4": "extreme"},
            "probabilities": {"very_low": 0.03, "low": 0.12, "normal": 0.42, "high": 0.33, "extreme": 0.10},
            "confidence": 0.5,
        },
    },
    "usage": {"input_tokens": 412, "output_tokens": 0},
}


def client(handler: httpx.MockTransport | object, **kw: object) -> SystemOneClient:
    return SystemOneClient("http://decider:8080", model="m@1", transport=handler, **kw)  # type: ignore[arg-type]


async def test_valid_answer_is_parsed_and_the_confidence_recomputed() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json=GOOD, headers={"Server-Timing": "model;dur=41.2, server;dur=2.8, total;dur=44.0"}
        )

    c = client(httpx.MockTransport(handler), api_key="sekret")
    r = await c.decide('{"v":"chronos.state.v1"}', QUESTIONS)
    assert seen["url"] == "http://decider:8080/v1/systemone" and seen["auth"] == "Bearer sekret"
    body = seen["body"]
    assert isinstance(body, dict) and body["state"] == '{"v":"chronos.state.v1"}' and set(body["questions"]) == {
        "action", "edge", "vol_regime",
    }  # fmt: skip
    assert r.answers.action.confidence == pytest.approx(
        confidence_from_probs(GOOD["answers"]["action"]["probabilities"])
    )
    assert r.answers.action.confidence != 0.99
    assert r.server_timing_ms == {"model": 41.2, "server": 2.8, "total": 44.0} and r.input_tokens == 412
    await c.aclose()


async def test_no_authorization_header_without_a_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json=GOOD)

    await client(httpx.MockTransport(handler)).decide("{}", QUESTIONS)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 401])
async def test_http_errors_are_unavailable(status: int) -> None:
    c = client(httpx.MockTransport(lambda r: httpx.Response(status)))
    with pytest.raises(DeciderUnavailable):
        await c.decide("{}", QUESTIONS)


async def test_timeout_is_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(DeciderTimeout):
        await client(httpx.MockTransport(handler)).decide("{}", QUESTIONS)


async def test_connection_failure_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(DeciderUnavailable):
        await client(httpx.MockTransport(handler)).decide("{}", QUESTIONS)


async def test_malformed_json_is_a_bad_response() -> None:
    c = client(httpx.MockTransport(lambda r: httpx.Response(200, content=b"{not json")))
    with pytest.raises(DeciderBadResponse):
        await c.decide("{}", QUESTIONS)


async def test_probabilities_that_do_not_sum_to_one_are_a_bad_response() -> None:
    bad = json.loads(json.dumps(GOOD))
    bad["answers"]["action"]["probabilities"] = {"BUY": 0.5, "SELL": 0.02, "HOLD": 0.27}
    c = client(httpx.MockTransport(lambda r: httpx.Response(200, json=bad)))
    with pytest.raises(DeciderBadResponse):
        await c.decide("{}", QUESTIONS)


async def test_missing_answer_is_a_bad_response() -> None:
    bad = json.loads(json.dumps(GOOD))
    del bad["answers"]["edge"]
    c = client(httpx.MockTransport(lambda r: httpx.Response(200, json=bad)))
    with pytest.raises(DeciderBadResponse):
        await c.decide("{}", QUESTIONS)


async def test_incomplete_regime_options_are_a_bad_response() -> None:
    bad = json.loads(json.dumps(GOOD))
    bad["answers"]["vol_regime"]["probabilities"] = {"very_low": 0.5, "low": 0.5}
    c = client(httpx.MockTransport(lambda r: httpx.Response(200, json=bad)))
    with pytest.raises(DeciderBadResponse):
        await c.decide("{}", QUESTIONS)


def test_server_timing_parsing_edge_cases() -> None:
    assert parse_server_timing(None) == {} and parse_server_timing("") == {}
    assert parse_server_timing("total;dur=44") == {"total": 44.0}
    assert parse_server_timing("garbage") == {}
