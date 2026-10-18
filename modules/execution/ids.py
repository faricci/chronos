"""Deterministic order ids (spec sez. 4.1.5): a network retry or a replay can never create a second order."""

from __future__ import annotations

import uuid
from typing import Final

NAMESPACE_CHRONOS: Final = uuid.UUID("6f1c2a8e-5b1e-5c9a-9d1f-1f0b7c3a2e11")


def client_order_id(decision_id: str, leg: str) -> str:
    """``uuid5(NAMESPACE_CHRONOS, f"{decision_id}:{leg}")`` as a string."""
    return str(uuid.uuid5(NAMESPACE_CHRONOS, f"{decision_id}:{leg}"))
