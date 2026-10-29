"""The audits of L0 measure with the label_config of the MODEL THAT RUNS, not with the one of the research pool the
refit trains on (found on the field, dry run of 2026-10-24: ``no label spread for XRP-EUR``)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from modules.dryrun.learner import AuditLabels
from modules.dryrun.registry import artifacts_from_dir
from modules.dryrun.revision_impact import RegistryDeciderLoader
from modules.features.serializer import serialize_state
from modules.features.state_builder import PositionInput, StateBuilder
from modules.market.indicators import IndicatorSnapshot
from modules.risk.label_config import LoadedLabelConfig, extend_label_config, load_label_config
from tests.indicator_fixtures import synthetic_bars
from tests.label_fixtures import loaded_label
from tests.test_learner import (
    LABEL,
    FlatAnswers,
    Rig,
    SteepVwap,
    impact_rig,
    l0_impact,
    make_version,
)
from tests.test_revision_impact import LABEL as IMPACT_LABEL
from tests.test_revision_impact import RISK, THR, world
from training.backtest import build_market

__all__ = ["world"]

TRAINING = LABEL  # the research pool: BTC-EUR and SOL-EUR
DAY = date(2024, 1, 20)


def write_label(path: Path, label: LoadedLabelConfig) -> LoadedLabelConfig:
    """A label_config.json as ``feasibility`` writes it, for ``label`` (the hash is the label's own)."""
    raw = dict(label.raw)
    raw.update(
        atr_tf=label.cfg.atr_tf, k_tp=label.cfg.k_tp, k_sl=label.cfg.k_sl, entry_mode=label.cfg.entry_mode,
        horizon_min=label.cfg.horizon_min, entry_timeout_min=label.cfg.entry_timeout_min,
        entry_first_bar=label.cfg.entry_first_bar,
        fees_bps={"maker": label.fees.maker_bps, "taker": label.fees.taker_bps, "source": "probe"},
        slip_bps=label.costs.slippage_buffer_bps, stop_extra_slip_bps=label.costs.stop_extra_slip_bps,
        spread_bps=dict(label.spread_bps),
    )  # fmt: skip
    path.write_text(json.dumps(raw), encoding="utf-8")
    return load_label_config(path)


def extended(tmp_path: Path) -> LoadedLabelConfig:
    """The training label of the research pool plus XRP-EUR: what ``script/extend_label_config.py`` makes."""
    base = write_label(tmp_path / "pool.json", TRAINING)
    out = tmp_path / "extended.json"
    out.write_text(json.dumps(extend_label_config(base, {"XRP-EUR": 3.0})), encoding="utf-8")
    return load_label_config(out)


def register(rig: Rig, name: str, label: LoadedLabelConfig, alias: str | None) -> None:
    """A registered version whose ``label_config.json`` is ``label`` (written BEFORE registering: immutable after)."""
    model = make_version(rig.tmp / "extra", name)
    write_label(model / "label_config.json", label)
    rig.registry.register(artifacts_from_dir(model), {"version": name})
    if alias is not None:
        rig.registry.promote(name, alias)


def states(
    label: LoadedLabelConfig, product: str, seed: int, rows: range, version: str
) -> tuple[list[dict[str, Any]], Any]:
    """Live ``DecisionRecord`` s of ``product`` built with ``label`` (the label of the version that decides), and the
    market the batch path rebuilds them from."""
    bars = synthetic_bars(days=25, seed=seed, zero_volume_day=None, constant_days=None)
    closes = bars["close"]
    btc = synthetic_bars(days=25, seed=42, zero_volume_day=None, constant_days=None)
    m_btc = build_market("BTC-EUR", btc, label)
    market = m_btc if product == "BTC-EUR" else build_market(product, bars, label, m_btc.ind)
    builder = StateBuilder(label)
    ind = market.ind
    out = []
    for i in rows:

        def snap(p: str, frame: pd.DataFrame, i: int = i) -> IndicatorSnapshot:
            row = dict(zip(frame.columns, frame.to_numpy(dtype=np.float64)[i], strict=True))
            return IndicatorSnapshot.from_row(p, frame.index[i].to_pydatetime(), row)

        state = builder.build(
            snapshot=snap(product, ind), btc=None if product == "BTC-EUR" else snap("BTC-EUR", m_btc.ind),
            sentiment=None, position=PositionInput.flat(), close_t=float(closes.iloc[i]),
        )  # fmt: skip
        t = (ind.index[i] + pd.Timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({
            "decision_id": f"{version}-{product}-{i}", "ts_bar_close": t, "book": "champion", "product": product,
            "state": serialize_state(state), "policy": {"policy_version": version, "final": "HOLD"},
        })  # fmt: skip
    return out, market


def first_row(product_seed: int = 41) -> int:
    bars = synthetic_bars(days=25, seed=product_seed, zero_volume_day=None, constant_days=None)
    return (datetime(2024, 1, 20, tzinfo=UTC) - bars.index[0].to_pydatetime()).days * 1440 + 30


def write_decisions(rig: Rig, records: list[dict[str, Any]]) -> None:
    rig.paths.decisions_dir.mkdir(parents=True, exist_ok=True)
    path = rig.paths.decisions_dir / f"{DAY.isoformat()}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


# ==================================================================================== the field regression


def test_the_research_pool_label_has_no_spread_for_the_product_the_champion_was_extended_to(tmp_path: Path) -> None:
    """The failure of the dry run: the pool label (3 products) cannot build the state of the 4th."""
    ext = extended(tmp_path)
    assert set(ext.spread_bps) == {"BTC-EUR", "SOL-EUR", "XRP-EUR"} and ext.sha256 != TRAINING.sha256
    with pytest.raises(ValueError, match="no label spread for XRP-EUR"):
        TRAINING.spread_for("XRP-EUR", datetime(2024, 1, 20, tzinfo=UTC))


def test_l0_audits_a_champion_extended_to_one_more_product_with_its_own_label(tmp_path: Path) -> None:
    ext = extended(tmp_path)
    rig = Rig(tmp_path)  # its training label (``label=``) is the pool's: BTC-EUR and SOL-EUR
    register(rig, "champ-ext", ext, "champion")
    rows = range(first_row() + 0, first_row() + 1400, 5)
    sol, m_sol = states(ext, "SOL-EUR", 41, rows, "champ-ext")
    btc, m_btc = states(ext, "BTC-EUR", 42, rows, "champ-ext")
    xrp, m_xrp = states(ext, "XRP-EUR", 43, rows, "champ-ext")
    write_decisions(rig, sol + btc + xrp)
    seen: list[str] = []

    def markets(label: LoadedLabelConfig) -> dict[str, Any]:
        seen.append(label.sha256)  # the markets are built for THE LABEL being audited
        return {"SOL-EUR": m_sol, "BTC-EUR": m_btc, "XRP-EUR": m_xrp}

    doc = rig.learner(markets=markets, products=["SOL-EUR", "BTC-EUR", "XRP-EUR"]).run(date(2024, 1, 21))
    l0 = doc["steps"]["L0"]
    assert l0["outcome"] == "ok", l0["message"]
    parity = l0["details"]["state_parity"]
    assert parity["state_parity"]["rows"] == 3 * len(rows) and parity["state_parity"]["match"] == 1.0
    assert parity["unmatched_rows"] == 0
    audit = l0["details"]["audit_label"]
    assert audit["source"] == "champion" and audit["version"] == "champ-ext"
    assert audit["label_config_sha256"] == ext.sha256 and audit["products"] == ["BTC-EUR", "SOL-EUR", "XRP-EUR"]
    assert set(seen) == {ext.sha256}  # never the pool's


def test_without_a_champion_the_audits_fall_back_to_the_training_label_and_say_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rig = Rig(tmp_path)
    (tmp_path / "registry" / "aliases.json").unlink()  # an empty registry: no champion
    rows = range(first_row(), first_row() + 1400, 5)
    sol, m_sol = states(TRAINING, "SOL-EUR", 41, rows, "m@1")
    btc, m_btc = states(TRAINING, "BTC-EUR", 42, rows, "m@1")
    write_decisions(rig, sol + btc)
    seen: list[str] = []

    def markets(label: LoadedLabelConfig) -> dict[str, Any]:
        seen.append(label.sha256)
        return {"SOL-EUR": m_sol, "BTC-EUR": m_btc}

    doc = rig.learner(markets=markets, products=["SOL-EUR", "BTC-EUR"]).run(date(2024, 1, 21))
    l0 = doc["steps"]["L0"]
    assert l0["outcome"] == "ok" and l0["details"]["state_parity"]["state_parity"]["match"] == 1.0
    audit = l0["details"]["audit_label"]
    assert audit["source"] == "training_fallback" and "UnknownAlias" in audit["reason"]
    assert audit["label_config_sha256"] == TRAINING.sha256 and set(seen) == {TRAINING.sha256}
    assert "learner_audit_label_fallback" in capsys.readouterr().out  # the explicit event


def test_an_unreadable_champion_label_is_a_fallback_with_its_reason_not_a_silent_one(tmp_path: Path) -> None:
    rig = Rig(tmp_path)  # the base champion of the rig has a label_config.json of "{}"
    label, info = AuditLabels(rig.learner()._registry, TRAINING).champion()  # noqa: SLF001
    assert label is TRAINING and info["source"] == "training_fallback" and "malformed label_config" in info["reason"]
    none_label, none_info = AuditLabels(rig.learner()._registry, None).champion()  # noqa: SLF001
    assert none_label is None and none_info["source"] == "none"


def test_two_versions_with_different_label_configs_are_each_audited_with_their_own(tmp_path: Path) -> None:
    champ = extended(tmp_path)
    other = loaded_label({"BTC-EUR": 3.0, "SOL-EUR": 4.5}, horizon_min=2 * 1440, k_tp=2.0, k_sl=1.0)  # another barrier
    assert other.sha256 not in (champ.sha256, TRAINING.sha256)
    rig = Rig(tmp_path)
    register(rig, "champ-ext", champ, "champion")
    register(rig, "chall-short", other, "challenger")
    rows = range(first_row(), first_row() + 1400, 7)
    sol_c, m_sol_c = states(champ, "SOL-EUR", 41, rows, "champ-ext")
    btc_c, m_btc_c = states(champ, "BTC-EUR", 42, rows, "champ-ext")
    sol_o, m_sol_o = states(other, "SOL-EUR", 41, rows, "chall-short")
    btc_o, m_btc_o = states(other, "BTC-EUR", 42, rows, "chall-short")
    for r in sol_o + btc_o:
        r["book"] = "challenger"
    write_decisions(rig, sol_c + btc_c + sol_o + btc_o)
    by_sha = {
        champ.sha256: {"SOL-EUR": m_sol_c, "BTC-EUR": m_btc_c},
        other.sha256: {"SOL-EUR": m_sol_o, "BTC-EUR": m_btc_o},
    }  # fmt: skip
    asked: list[str] = []

    def markets(label: LoadedLabelConfig) -> dict[str, Any]:
        asked.append(label.sha256)
        return by_sha[label.sha256]

    doc = rig.learner(markets=markets, products=["SOL-EUR", "BTC-EUR"]).run(date(2024, 1, 21))
    l0 = doc["steps"]["L0"]
    assert l0["outcome"] == "ok", l0["message"]
    parity = l0["details"]["state_parity"]["state_parity"]
    assert parity["rows"] == 4 * len(rows) and parity["match"] == 1.0  # a single label would break one version's tokens
    assert sorted(asked) == sorted({champ.sha256, other.sha256}) and len(asked) == 2  # loaded once per label_config


def test_a_decision_of_a_version_the_registry_does_not_know_is_audited_with_the_champion_label(tmp_path: Path) -> None:
    champ = extended(tmp_path)
    rig = Rig(tmp_path)
    register(rig, "champ-ext", champ, "champion")
    rows = range(first_row(), first_row() + 1400, 10)
    sol, m_sol = states(champ, "SOL-EUR", 41, rows, "stub-not-registered")
    btc, m_btc = states(champ, "BTC-EUR", 42, rows, "stub-not-registered")
    xrp, m_xrp = states(champ, "XRP-EUR", 43, rows, "stub-not-registered")  # a product only the champion knows
    write_decisions(rig, sol + btc + xrp)
    markets = {"SOL-EUR": m_sol, "BTC-EUR": m_btc, "XRP-EUR": m_xrp}
    doc = rig.learner(markets=lambda label: markets, products=["SOL-EUR", "BTC-EUR", "XRP-EUR"]).run(date(2024, 1, 21))
    assert doc["steps"]["L0"]["outcome"] == "ok", doc["steps"]["L0"]["message"]
    assert doc["steps"]["L0"]["details"]["state_parity"]["state_parity"]["match"] == 1.0


# ===================================================================================== revision impact


class RegistryStub:
    def __init__(self, root: Path) -> None:
        self.root = root

    def get_version(self, version: str) -> Any:
        class Found:
            path = self.root / version

        return Found()


def test_the_decider_loader_gives_each_version_the_label_stored_in_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import modules.decision.baseline as baseline_module

    ext = extended(tmp_path)
    (tmp_path / "v-ext").mkdir()
    write_label(tmp_path / "v-ext" / "label_config.json", ext)
    (tmp_path / "v-old").mkdir()  # a version directory without a label_config.json
    loaded: list[tuple[str, str]] = []

    class FakeDecider:
        @classmethod
        def load(cls, path: Path, *, label: LoadedLabelConfig, questions: Any, subset: str, **kw: Any) -> object:
            loaded.append((path.name, label.sha256))
            return object()

    monkeypatch.setattr(baseline_module, "BaselineDecider", FakeDecider)
    loader = RegistryDeciderLoader(RegistryStub(tmp_path), TRAINING)
    _decider, _policy, label = loader("v-ext")
    assert label.sha256 == ext.sha256 and loaded[-1] == ("v-ext", ext.sha256)  # its own, not the fallback
    _decider, _policy, label_old = loader("v-old")
    assert label_old is TRAINING  # only a directory with no label_config.json takes the fallback
    loader("v-ext")
    assert len(loaded) == 2  # cached by version
    from modules.dryrun.revision_impact import ConfigurationError

    with pytest.raises(ConfigurationError, match="no label_config.json"):
        RegistryDeciderLoader(RegistryStub(tmp_path))("v-old")


def test_a_product_the_label_of_a_version_does_not_cover_is_not_measured_for_it(world: dict[str, Any]) -> None:
    from modules.dryrun.revision_impact import revision_impact

    only_btc = loaded_label({"BTC-EUR": 3.0})
    out = revision_impact(
        world["S"], world["F"], [date(2024, 1, 20)], FlatAnswers(), None, only_btc, [], RISK, "v1", thresholds=THR
    )
    assert {d.product for d in out} <= {"BTC-EUR"}
    both = revision_impact(
        world["S"], world["F"], [date(2024, 1, 20)], FlatAnswers(), None, IMPACT_LABEL, [], RISK, "v1", thresholds=THR
    )
    assert {d.product for d in both} == {"BTC-EUR", "ETH-EUR"}


def test_l0_impact_measures_a_version_with_the_label_its_loader_gives(tmp_path: Path) -> None:
    books = {"champion": ["m@1"], "challenger": ["m@2"]}
    rig, loader = impact_rig(tmp_path, {"m@1": FlatAnswers(), "m@2": SteepVwap()}, books)
    without_sol = loaded_label({"BTC-EUR": 3.0})  # the challenger's label does not know the product being audited
    plain = loader.__call__

    def with_labels(version: str) -> tuple[Any, ...]:
        decider, policy = plain(version)
        return (decider, policy, without_sol) if version == "m@2" else (decider, policy)

    rig.kw["deciders"] = with_labels
    d = l0_impact(rig)
    books = d["products"]["SOL-EUR"]["books"]
    assert list(books) == ["champion"]  # m@2 never decided on SOL-EUR under its own label: nothing to measure
    assert d["products"]["SOL-EUR"]["verdict"]["champion"]["R1"] == "pass"


def test_l0_impact_uses_the_champion_label_and_not_the_training_one(tmp_path: Path) -> None:
    training_without_sol = loaded_label({"BTC-EUR": 3.0})
    books = {"champion": ["m@1"]}
    rig, _loader = impact_rig(tmp_path, {"m@1": FlatAnswers()}, books, label=training_without_sol)
    register(rig, "champ-sol", LABEL, "champion")  # the champion that runs covers SOL-EUR, the training label does not
    d = l0_impact(rig)
    assert list(d["products"]["SOL-EUR"]["books"]) == ["champion"]
    assert d["products"]["SOL-EUR"]["verdict"]["champion"]["R1"] == "pass"
