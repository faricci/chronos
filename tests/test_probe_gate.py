"""The enabling of the real run (plan sez. 3.5, acts 1-4) and the stage chain (sez. 4). No network, no real key."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from modules.config import ExecProbeConfig
from modules.execution.orders import ProductMeta
from modules.probe.gate import (
    PROBE_CONFIRM_ENV,
    GateRefused,
    ask_confirmation,
    check_gate,
    confirm_phrase,
    key_fingerprint,
    read_credentials,
    require_terminal,
    secrets_of,
)
from modules.probe.stages import (
    StageBlocked,
    StageRecord,
    check_chain,
    config_hash,
    crossing_buy_intent,
    far_buy_intent,
    read_stage,
    skipped_record,
    write_stage,
)

D = Decimal
GOOD_ENV = {"CHRONOS_LIVE_CONFIRM": "I_ACCEPT_REAL_ORDERS", PROBE_CONFIRM_ENV: "BTC-EUR:3.00"}
ENABLED = ExecProbeConfig(enabled=True)
NOW = datetime(2026, 10, 28, 8, 0, tzinfo=UTC)
PEM = (
    "-----BEGIN EC PRIVATE KEY-----\n"
    "MHcCAQEEIFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEoAoGCCqGSM49\n"
    "-----END EC PRIVATE KEY-----\n"
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "outside" / "probe.key"
    path.parent.mkdir()
    path.write_text(
        f'COINBASE_API_KEY=organizations/o/apiKeys/k1\nCOINBASE_API_SECRET="{PEM.replace(chr(10), chr(92) + "n")}"\n'
    )
    return path


# ------------------------------------------------------------------------------------------- the acts


def test_all_acts_present_returns_the_resolved_key_file(repo: Path, key_file: Path) -> None:
    assert check_gate(ENABLED, GOOD_ENV, key_file, repo, can_send_orders=True) == key_file.resolve()


def test_the_flag_in_the_config_is_act_one(repo: Path, key_file: Path) -> None:
    with pytest.raises(GateRefused, match="enabled is false"):
        check_gate(ExecProbeConfig(), GOOD_ENV, key_file, repo, can_send_orders=True)


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"CHRONOS_LIVE_CONFIRM": "I_ACCEPT_REAL_ORDERS"},  # the probe's own confirmation is missing
        {PROBE_CONFIRM_ENV: "BTC-EUR:3.00"},  # the live confirmation is missing
        {"CHRONOS_LIVE_CONFIRM": "yes", PROBE_CONFIRM_ENV: "BTC-EUR:3.00"},
        {"CHRONOS_LIVE_CONFIRM": "I_ACCEPT_REAL_ORDERS", PROBE_CONFIRM_ENV: "BTC-EUR:20.00"},  # not the config's L_tot
        {"CHRONOS_LIVE_CONFIRM": "I_ACCEPT_REAL_ORDERS", PROBE_CONFIRM_ENV: "BTC-EUR:3"},
        {"CHRONOS_LIVE_CONFIRM": "I_ACCEPT_REAL_ORDERS", PROBE_CONFIRM_ENV: "BTC-EUR:3.00 "},
        {"CHRONOS_LIVE_CONFIRM": "I_ACCEPT_REAL_ORDERS", PROBE_CONFIRM_ENV: "ETH-EUR:3.00"},
    ],
)
def test_the_two_confirmations_must_be_exact_and_equal_to_the_config(
    repo: Path, key_file: Path, env: dict[str, str]
) -> None:
    with pytest.raises(GateRefused, match="CHRONOS"):
        check_gate(ENABLED, env, key_file, repo, can_send_orders=True)


def test_a_read_only_stage_needs_the_flag_and_the_key_file_only(repo: Path, key_file: Path) -> None:
    assert check_gate(ENABLED, {}, key_file, repo, can_send_orders=False) == key_file.resolve()


def test_the_confirmation_follows_the_config_total(repo: Path, key_file: Path) -> None:
    cfg = ExecProbeConfig(enabled=True, max_total_loss_quote=D("2.00"))
    with pytest.raises(GateRefused):
        check_gate(cfg, GOOD_ENV, key_file, repo, can_send_orders=True)
    check_gate(cfg, {**GOOD_ENV, PROBE_CONFIRM_ENV: "BTC-EUR:2.00"}, key_file, repo, can_send_orders=True)


def test_no_key_file_is_refused(repo: Path) -> None:
    with pytest.raises(GateRefused, match="--key-file is required"):
        check_gate(ENABLED, GOOD_ENV, None, repo, can_send_orders=True)


def test_a_missing_key_file_is_refused(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(GateRefused, match="does not exist"):
        check_gate(ENABLED, GOOD_ENV, tmp_path / "nope.key", repo, can_send_orders=True)


def test_a_key_file_inside_the_repo_is_refused_even_through_dotdot(repo: Path, tmp_path: Path) -> None:
    inside = repo / "config" / "probe.key"
    inside.parent.mkdir()
    inside.write_text("x")
    with pytest.raises(GateRefused, match="inside the repository"):
        check_gate(ENABLED, GOOD_ENV, inside, repo, can_send_orders=True)
    sneaky = tmp_path / "outside2" / ".." / "repo" / "config" / "probe.key"
    (tmp_path / "outside2").mkdir()
    with pytest.raises(GateRefused, match="inside the repository"):
        check_gate(ENABLED, GOOD_ENV, sneaky, repo, can_send_orders=True)


def test_a_key_in_the_environment_is_refused(repo: Path, key_file: Path) -> None:
    for name in ("COINBASE_API_KEY", "COINBASE_API_SECRET"):
        with pytest.raises(GateRefused, match=name):
            check_gate(ENABLED, {**GOOD_ENV, name: "x"}, key_file, repo, can_send_orders=True)
        with pytest.raises(GateRefused, match=name):
            check_gate(ENABLED, {name: ""}, key_file, repo, can_send_orders=False)  # present, even if empty


def test_the_refusal_message_never_holds_a_secret(repo: Path, key_file: Path) -> None:
    with pytest.raises(GateRefused) as exc:
        check_gate(
            ENABLED, {**GOOD_ENV, "COINBASE_API_KEY": "SUPER-SECRET-VALUE-123"}, key_file, repo, can_send_orders=True
        )
    assert "SUPER-SECRET" not in str(exc.value)


# ------------------------------------------------------------------------------------------ act four


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_stdin_must_be_a_terminal() -> None:
    with pytest.raises(GateRefused, match="not a terminal"):
        require_terminal(io.StringIO("CONFERMO\n"))
    require_terminal(Tty("x"))


def test_the_phrase_is_exact_and_nothing_else_confirms() -> None:
    assert confirm_phrase(ENABLED) == "CONFERMO BTC-EUR 3.00 EUR"
    out = io.StringIO()
    assert ask_confirmation(ENABLED, ["line a", "line b"], Tty("CONFERMO BTC-EUR 3.00 EUR\n"), out)
    assert "CONFERMO BTC-EUR 3.00 EUR" in out.getvalue() and "line a" in out.getvalue()
    for wrong in (
        "",
        "si",
        "confermo btc-eur 3.00 eur",
        "CONFERMO BTC-EUR 3 EUR",
        "CONFERMO BTC-EUR 20.00 EUR",
        "y",
        "--yes",
    ):
        assert not ask_confirmation(ENABLED, [], Tty(wrong + "\n"), io.StringIO())
    assert not ask_confirmation(ENABLED, [], Tty(""), io.StringIO())  # EOF


# ------------------------------------------------------------------------------------------ credentials


def test_credentials_from_an_env_style_file(key_file: Path) -> None:
    cfg = read_credentials(key_file)
    assert cfg.api_key is not None and cfg.api_key.get_secret_value() == "organizations/o/apiKeys/k1"
    assert cfg.api_secret is not None and cfg.api_secret.get_secret_value() == PEM  # "\n" unescaped by CoinbaseConfig
    assert len(key_fingerprint(cfg)) == 8
    assert "k1" not in repr(cfg) and "FAKEFAKE" not in repr(cfg)  # SecretStr


def test_credentials_from_the_json_the_console_downloads(tmp_path: Path) -> None:
    path = tmp_path / "cdp.json"
    path.write_text(json.dumps({"name": "organizations/o/apiKeys/k2", "privateKey": PEM}))
    cfg = read_credentials(path)
    assert cfg.api_key is not None and cfg.api_key.get_secret_value().endswith("k2")
    assert cfg.api_secret is not None and cfg.api_secret.get_secret_value() == PEM


def test_the_fingerprint_is_stable_and_not_the_key(key_file: Path) -> None:
    cfg = read_credentials(key_file)
    assert key_fingerprint(cfg) == key_fingerprint(read_credentials(key_file))
    assert "apiKeys" not in key_fingerprint(cfg)


@pytest.mark.parametrize("text", ["COINBASE_API_KEY=only-a-key\n", "{}", "# nothing\n", '{"name": "x"}'])
def test_an_incomplete_key_file_is_refused(tmp_path: Path, text: str) -> None:
    path = tmp_path / "bad.key"
    path.write_text(text)
    with pytest.raises(GateRefused, match="lacks"):
        read_credentials(path)


def test_broken_json_is_refused_without_echoing_it(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"name": "SECRET-ish", ')
    with pytest.raises(GateRefused) as exc:
        read_credentials(path)
    assert "SECRET-ish" not in str(exc.value)


def test_the_repo_dotenv_is_not_read(tmp_path: Path, key_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A .env in the working directory (the dry run's View key) must not leak into the probe's credentials."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".env").write_text("COINBASE_API_KEY=VIEW-KEY-OF-THE-DRY-RUN\nCOINBASE_API_SECRET=view-secret\n")
    monkeypatch.chdir(cwd)
    cfg = read_credentials(key_file)
    assert cfg.api_key is not None and cfg.api_key.get_secret_value() == "organizations/o/apiKeys/k1"


def test_secrets_of_covers_the_forms_a_pem_can_take(key_file: Path) -> None:
    forms = secrets_of(read_credentials(key_file))
    assert "organizations/o/apiKeys/k1" in forms and PEM in forms and PEM.replace("\n", "\\n") in forms
    assert any("FAKEFAKE" in f for f in forms)


# --------------------------------------------------------------------------------------- stage chain


def rec(
    stage: str, *, ok: bool = True, at: datetime = NOW, cfg: ExecProbeConfig = ENABLED, **kw: object
) -> StageRecord:
    return StageRecord(stage, ok, at, config_hash(cfg), **kw)  # type: ignore[arg-type]


def test_preflight_has_no_predecessor(tmp_path: Path) -> None:
    assert check_chain(tmp_path, "preflight", ENABLED, NOW) is None


def test_each_stage_needs_the_previous_ok_of_today_with_the_same_config(tmp_path: Path) -> None:
    with pytest.raises(StageBlocked, match="'preflight' has no ok outcome"):
        check_chain(tmp_path, "preview", ENABLED, NOW)
    write_stage(tmp_path, rec("preflight", ok=False))
    with pytest.raises(StageBlocked, match="no ok outcome"):
        check_chain(tmp_path, "preview", ENABLED, NOW)
    write_stage(tmp_path, rec("preflight"))
    check_chain(tmp_path, "preview", ENABLED, NOW)
    with pytest.raises(StageBlocked, match="not today"):
        check_chain(tmp_path, "preview", ENABLED, NOW + timedelta(days=1))
    other = ExecProbeConfig(enabled=True, max_entries_total=4)
    with pytest.raises(StageBlocked, match="another config"):
        check_chain(tmp_path, "preview", other, NOW)
    with pytest.raises(StageBlocked, match="'preview' has no ok outcome"):
        check_chain(tmp_path, "place-cancel", ENABLED, NOW)  # one step at a time


def test_the_stage_file_round_trips(tmp_path: Path) -> None:
    r = rec("preview", entry_mode="gtd", details={"gtd": {"errors": []}, "n": 3})
    write_stage(tmp_path, r)
    back = read_stage(tmp_path, "preview")
    assert back == r and (tmp_path / "preview.json").is_file()
    assert json.loads((tmp_path / "preview.json").read_text())["day"] == "2026-10-28"
    (tmp_path / "preview.json").write_text("{broken")
    assert read_stage(tmp_path, "preview") is None  # unreadable = not ok


def chain_until(out: Path, last: str, *, entry_mode: str = "gtd", at: datetime = NOW) -> None:
    for stage in ("preflight", "preview", "place-cancel", "expire", "reject", "stop-switch"):
        stage_at = at if stage != "stop-switch" else NOW
        write_stage(out, rec(stage, at=stage_at, entry_mode=entry_mode if stage == "preview" else None))  # type: ignore[arg-type]
        if stage == last:
            break


def test_live_accepts_a_reject_of_up_to_seven_days(tmp_path: Path) -> None:
    chain_until(tmp_path, "stop-switch", at=NOW - timedelta(days=6, hours=23))
    assert check_chain(tmp_path, "live", ENABLED, NOW) is not None
    chain_until(tmp_path, "stop-switch", at=NOW - timedelta(days=7, hours=1))
    with pytest.raises(StageBlocked, match="older than 7 days"):
        check_chain(tmp_path, "live", ENABLED, NOW)


def test_live_needs_the_stop_switch_proved_today(tmp_path: Path) -> None:
    chain_until(tmp_path, "stop-switch")
    write_stage(tmp_path, rec("stop-switch", at=NOW - timedelta(days=1)))
    with pytest.raises(StageBlocked, match="not today"):
        check_chain(tmp_path, "live", ENABLED, NOW)


def test_live_needs_every_earlier_stage_ok_with_this_config(tmp_path: Path) -> None:
    chain_until(tmp_path, "stop-switch")
    write_stage(tmp_path, rec("preview", ok=False, entry_mode="gtd"))
    with pytest.raises(StageBlocked, match="'preview'"):
        check_chain(tmp_path, "live", ENABLED, NOW)


def test_the_gtc_fallback_is_not_accepted_unless_the_user_wrote_it(tmp_path: Path) -> None:
    chain_until(tmp_path, "stop-switch", entry_mode="gtc_fallback")
    with pytest.raises(StageBlocked, match="has not accepted the GTC fallback"):
        check_chain(tmp_path, "live", ENABLED, NOW)
    accepted = ExecProbeConfig(enabled=True, accept_gtc_fallback=True)
    for stage in ("preflight", "preview", "place-cancel", "expire", "reject", "stop-switch"):  # the hash changed
        r = read_stage(tmp_path, stage)
        assert r is not None
        write_stage(
            tmp_path, StageRecord(r.stage, r.ok, r.at, config_hash(accepted), r.entry_mode, r.skipped, r.details)
        )
    assert check_chain(tmp_path, "live", accepted, NOW) is not None


def test_the_expire_stage_can_be_skipped_and_the_chain_goes_on(tmp_path: Path) -> None:
    chain_until(tmp_path, "place-cancel", entry_mode="gtc_fallback")
    assert check_chain(tmp_path, "expire", ENABLED, NOW) is not None  # allowed to start: the runner skips it
    skipped = skipped_record("expire", ENABLED, NOW, "not applicable with the GTC fallback")
    assert skipped.ok and skipped.skipped
    write_stage(tmp_path, skipped)
    assert read_stage(tmp_path, "expire") == skipped
    assert check_chain(tmp_path, "reject", ENABLED, NOW) is not None


# ----------------------------------------------------------------------------------------- stage intents

META = ProductMeta(
    product_id="BTC-EUR", base_currency="BTC", quote_currency="EUR", base_increment=D("0.00000001"),
    quote_increment=D("0.01"), base_min_size=D("0.00000001"), quote_min_size=D("1"),
)  # fmt: skip


def test_the_far_order_is_two_euros_five_percent_below_the_bid() -> None:
    i = far_buy_intent("place-cancel", D("80000.00"), META, ENABLED, NOW, tif_s=300)
    assert i.entry.limit_price == D("76000.00") and i.entry.expires_on_exchange and i.entry.time_in_force_s == 300
    assert D("2.00") <= i.base_size * i.entry.limit_price < D("2.00") + D("0.001")  # rounded UP to the increment
    assert i.decision_id.startswith("probe-test:place-cancel:") and not i.decision_id.startswith("probe:")
    assert i.entry.type == "limit_post_only"


def test_the_crossing_order_sits_at_the_ask_rounded_up() -> None:
    i = crossing_buy_intent("reject", D("80000.001"), META, ENABLED, NOW, tif_s=300)
    assert i.entry.limit_price == D("80000.01") and i.entry.type == "limit_post_only"
    assert i.base_size * i.entry.limit_price >= D("2.00")
