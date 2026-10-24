"""Extending a label_config to a new product: same label, new hash, never touching the source or the old products."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modules.risk.label_config import extend_label_config, load_label_config
from script.extend_label_config import apply_to_model, main
from tests.label_fixtures import loaded_label


def written(tmp_path: Path, name: str = "label_config.json") -> Path:
    label = loaded_label({"BTC-EUR": 3.0, "ETH-EUR": 3.0, "SOL-EUR": 3.0})
    raw = dict(label.raw)
    raw.update(
        atr_tf=label.cfg.atr_tf, k_tp=label.cfg.k_tp, k_sl=label.cfg.k_sl, entry_mode=label.cfg.entry_mode,
        horizon_min=label.cfg.horizon_min, entry_timeout_min=label.cfg.entry_timeout_min,
        entry_first_bar=label.cfg.entry_first_bar,
        fees_bps={"maker": label.fees.maker_bps, "taker": label.fees.taker_bps, "source": "probe"},
        slip_bps=label.costs.slippage_buffer_bps, stop_extra_slip_bps=label.costs.stop_extra_slip_bps,
        spread_bps=dict(label.spread_bps),
    )  # fmt: skip
    path = tmp_path / name
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_the_extension_changes_only_the_spreads_and_the_hash(tmp_path: Path) -> None:
    src = load_label_config(written(tmp_path))
    raw = extend_label_config(src, {"XRP-EUR": 3.0}, probe={"XRP-EUR": 2.6})
    assert raw["sha256"] != src.sha256 and raw["extended_from_sha256"] == src.sha256
    assert raw["spread_bps"] == {"BTC-EUR": 3.0, "ETH-EUR": 3.0, "SOL-EUR": 3.0, "XRP-EUR": 3.0}
    assert raw["spread_source"]["XRP-EUR"] == {"floor": 3.0, "probe": 2.6} and raw["extended_products"] == ["XRP-EUR"]
    for key in ("atr_tf", "k_tp", "k_sl", "entry_mode", "horizon_min", "entry_first_bar", "fees_bps", "slip_bps",
                "stop_extra_slip_bps", "selection"):  # fmt: skip
        assert raw[key] == src.raw[key]  # barriers, fees, costs and holdout are exactly those of the study
    assert src.spread_bps == {"BTC-EUR": 3.0, "ETH-EUR": 3.0, "SOL-EUR": 3.0}  # the source object is untouched


def test_the_extended_file_loads_and_its_hash_matches_its_content(tmp_path: Path) -> None:
    out = tmp_path / "ext.json"
    assert main(["--src", str(written(tmp_path)), "--add", "XRP-EUR=3.0", "--out", str(out)]) == 0
    ext = load_label_config(out)  # raises if the hash does not match
    assert ext.spread_for("XRP-EUR", None) == 3.0  # type: ignore[arg-type]
    assert ext.cfg == load_label_config(tmp_path / "label_config.json").cfg


def test_a_hand_edit_of_the_new_spread_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "ext.json"
    main(["--src", str(written(tmp_path)), "--add", "XRP-EUR=3.0", "--out", str(out)])
    raw = json.loads(out.read_text(encoding="utf-8"))
    raw["spread_bps"]["XRP-EUR"] = 9.0
    out.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="sha256 does not match"):
        load_label_config(out)


def test_existing_products_cannot_change_and_a_spread_must_be_positive(tmp_path: Path) -> None:
    src = load_label_config(written(tmp_path))
    with pytest.raises(ValueError, match="already in the label_config"):
        extend_label_config(src, {"SOL-EUR": 5.0})
    with pytest.raises(ValueError, match="positive"):
        extend_label_config(src, {"XRP-EUR": 0.0})
    with pytest.raises(ValueError, match="positive"):
        extend_label_config(src, {})


def test_out_cannot_overwrite_the_source(tmp_path: Path) -> None:
    src = written(tmp_path)
    with pytest.raises(SystemExit):
        main(["--src", str(src), "--add", "XRP-EUR=3.0", "--out", str(src)])


def test_a_model_copy_is_re_pointed_with_its_provenance(tmp_path: Path) -> None:
    src = written(tmp_path)
    old = load_label_config(src)
    model = tmp_path / "model"
    (model / "fold_1").mkdir(parents=True)
    (model / "final").mkdir()
    (model / "model_card.json").write_text(json.dumps({"label_config": {"sha256": old.sha256}}), encoding="utf-8")
    (model / "fold_1" / "policy.json").write_text(json.dumps({"label_config_sha256": old.sha256, "tau_buy": 0.6}))
    (model / "final" / "policy.json").write_text(json.dumps({"untunable": True, "reason": "untunable_ev"}))
    out = tmp_path / "ext.json"
    assert main(["--src", str(src), "--add", "XRP-EUR=3.0", "--out", str(out), "--model-dir", str(model)]) == 0
    new = load_label_config(out)
    card = json.loads((model / "model_card.json").read_text(encoding="utf-8"))
    assert card["label_config"] == {"sha256": new.sha256, "extended_from_sha256": old.sha256}
    assert json.loads((model / "fold_1" / "policy.json").read_text())["label_config_sha256"] == new.sha256
    assert json.loads((model / "final" / "policy.json").read_text()) == {"untunable": True, "reason": "untunable_ev"}
    assert load_label_config(model / "label_config.json").sha256 == new.sha256


def test_a_model_trained_for_another_label_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "ext.json"
    main(["--src", str(written(tmp_path)), "--add", "XRP-EUR=3.0", "--out", str(out)])
    model = tmp_path / "model"
    model.mkdir()
    (model / "model_card.json").write_text(json.dumps({"label_config": {"sha256": "f" * 64}}), encoding="utf-8")
    with pytest.raises(SystemExit):
        apply_to_model(model, out, "a" * 64, "b" * 64)
