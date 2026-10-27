"""A ready-made probe environment on the fake venue: ledger, balance book, guard and the REAL live gateway.

The gateway is built exactly as the runner builds it (same constructor, same checks), over ``Venue`` instead of the
exchange. No network, no key. Used by the stage tests; the runner tests build the whole runtime instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from modules.config import ExecProbeConfig, ExecutionConfig, FallbackFees
from modules.execution.balance import AccountSnapshot, BalanceBook
from modules.execution.balance_guard import BalanceGuard
from modules.execution.gateway import LiveExecutionGateway
from modules.execution.spot import SpotRegistry
from modules.ledger.positions import PositionLedger
from modules.probe import limits
from tests.coverage_fixtures import KillSwitch
from tests.gateway_fixtures import FeeSourceStub
from tests.probe_fakes import Clock, P, Venue

LIVE_ENV = {"CHRONOS_LIVE_CONFIRM": "I_ACCEPT_REAL_ORDERS"}
INC = {"EUR": Decimal("0.01"), "BTC": Decimal("0.00000001")}


@dataclass
class Rig:
    clock: Clock
    venue: Venue
    ledger: PositionLedger
    balances: BalanceBook
    guard: BalanceGuard
    gateway: LiveExecutionGateway
    registry: SpotRegistry
    kill: KillSwitch
    cfg: ExecProbeConfig

    async def close(self) -> None:
        await self.ledger.close()

    def refresh_snapshot(self) -> None:
        """Apply a fresh balance snapshot of the venue (valid when no reservation is open)."""
        b = self.balances
        eur_hold, btc_hold = self.venue.holds()
        snap = AccountSnapshot(
            balances={
                "EUR": (self.venue.eur_total - eur_hold, eur_hold),
                "BTC": (self.venue.btc_total - btc_hold, btc_hold),
            },
            taken_at=self.clock(),
        )
        b.apply_snapshot(snap, b.epoch, INC)


async def make_rig(
    *, eur: str = "5.00", btc: str = "0", cfg: ExecProbeConfig | None = None, **gateway_kwargs: object
) -> Rig:
    clock = Clock()
    venue = Venue(clock, eur=eur, btc=btc)
    registry = await SpotRegistry.load(venue, [P])  # type: ignore[arg-type]
    ledger = await PositionLedger.open(":memory:", "live")
    balances = BalanceBook(limits.BOOK, "exchange", clock)
    kill = KillSwitch()
    guard = BalanceGuard(balances, ledger.book_view(limits.BOOK), registry, FeeSourceStub(clock), FallbackFees(), kill)
    try:
        gateway = LiveExecutionGateway(
            guard, venue, ledger, registry, clock, book=limits.BOOK, balances=balances,  # type: ignore[arg-type]
            execution=ExecutionConfig(mode="live"), env=LIVE_ENV, sleep=clock.sleep, **gateway_kwargs,  # type: ignore[arg-type]
        )  # fmt: skip
    except BaseException:
        await ledger.close()
        raise
    rig = Rig(clock, venue, ledger, balances, guard, gateway, registry, kill, cfg or ExecProbeConfig(enabled=True))
    rig.refresh_snapshot()
    return rig
