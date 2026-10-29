"""Read-only probe of the Coinbase account and market data needed to finalize the Chronos v2 spec.

It never places, previews or cancels orders. It prints a summary and writes
data/probe/coinbase_probe.json, which contains no secrets.

Usage (inside the dev container, from the repo root, keys from env only):
    docker compose run --rm -e COINBASE_API_KEY -e COINBASE_API_SECRET dev \
        python -m script.probe_coinbase --products BTC-EUR ETH-EUR SOL-EUR XLM-EUR

Without keys (--public-only) it checks only public market data: spreads, min sizes,
ATR on 1h/6h/1d, 1m history depth and unauthenticated level2.
"""

import argparse
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
from coinbase.rest import RESTClient
from coinbase.websocket import WSClient
from pydantic import ValidationError

from modules.config import CoinbaseConfig, FallbackFees
from modules.market.indicators_batch import wilder_atr
from modules.risk.costs import CostModel, FeeRates, round_bps

# Barrier grid evaluated for the quick feasibility check, in ATR units of each timeframe
BARRIER_GRID = [(3.0, 2.0), (4.0, 2.0)]
# ATR timeframes: Coinbase granularity -> look-back window that stays under 350 candles
ATR_TIMEFRAMES = {"1h": ("ONE_HOUR", 12), "6h": ("SIX_HOUR", 60), "1d": ("ONE_DAY", 120)}


def as_dict(obj):
    """SDK responses are objects in recent versions and dicts in older ones."""
    return obj.to_dict() if hasattr(obj, "to_dict") else obj


def probe_permissions(client):
    p = as_dict(client.get_api_key_permissions())
    return {k: p.get(k) for k in ("can_view", "can_trade", "can_transfer", "portfolio_type")}


def probe_fees(client):
    s = as_dict(client.get_transaction_summary())
    tier = s.get("fee_tier", {}) or {}
    return {
        "pricing_tier": tier.get("pricing_tier"),
        "maker_bps": float(tier["maker_fee_rate"]) * 1e4 if tier.get("maker_fee_rate") else None,
        "taker_bps": float(tier["taker_fee_rate"]) * 1e4 if tier.get("taker_fee_rate") else None,
        "total_volume_30d": s.get("total_volume"),
    }


def probe_product(client, product_id):
    p = as_dict(client.get_public_product(product_id=product_id))
    keys = ("status", "trading_disabled", "price", "base_increment", "quote_increment",
            "base_min_size", "quote_min_size", "volume_24h", "approximate_quote_24h_volume",
            # inputs of the spot allowlists (spec sez. 4.1.6): the values seen here define SPOT_VENUES
            "product_type", "product_venue", "future_product_details", "base_currency_id",
            "quote_currency_id", "cancel_only", "view_only")
    out = {k: p.get(k) for k in keys}

    book = as_dict(client.get_public_product_book(product_id=product_id, limit=1))
    pricebook = book.get("pricebook", {})
    bid = float(pricebook["bids"][0]["price"])
    ask = float(pricebook["asks"][0]["price"])
    out["best_bid"], out["best_ask"] = bid, ask
    out["spread_bps"] = round((ask - bid) / ((ask + bid) / 2) * 1e4, 2)
    return out


def get_candles(client, product_id, start, end, granularity):
    r = as_dict(client.get_public_candles(
        product_id=product_id, start=str(int(start.timestamp())), end=str(int(end.timestamp())),
        granularity=granularity))
    candles = r.get("candles", [])
    return sorted(candles, key=lambda c: int(c["start"]))


def atr_bps(candles, period=14):
    """Wilder ATR in bps of the last close. The definition is modules.market.indicators_batch.wilder_atr
    (SMA seed, TR_0 = H-L): the same one the features and the labels use (spec sez. 2.2.1.b)."""
    if len(candles) < period:
        return None
    high = np.array([float(c["high"]) for c in candles])
    low = np.array([float(c["low"]) for c in candles])
    close = np.array([float(c["close"]) for c in candles])
    return round(float(wilder_atr(high, low, close, period)[-1] / close[-1] * 1e4), 1)


def probe_history_depth(client, product_id):
    """Checks whether 1m candles exist at several look-back depths (for the backfill plan)."""
    now = datetime.now(UTC)
    depth = {}
    for days in (30, 365, 730, 1095):
        start = now - timedelta(days=days)
        candles = get_candles(client, product_id, start, start + timedelta(minutes=300), "ONE_MINUTE")
        depth[f"{days}d"] = len(candles)
    return depth


def fees_for_feasibility(account):
    """Account fees if both maker and taker were read, otherwise the presumed lowest tier (flagged assumed)."""
    if isinstance(account, dict) and account.get("maker_bps") is not None and account.get("taker_bps") is not None:
        try:
            return FeeRates(maker_bps=account["maker_bps"], taker_bps=account["taker_bps"], assumed=False)
        except ValidationError:
            pass  # e.g. maker above taker: unusable, fall back
    fb = FallbackFees()
    return FeeRates(maker_bps=fb.maker_bps, taker_bps=fb.taker_bps, assumed=True)


def feasibility(atrs, spread_bps, fees):
    """Quick check of the asymmetric cost model against ATR-based barriers.

    Costs come only from modules.risk.costs.CostModel (spec sez. 0.3). p_breakeven is the minimum
    P(TP first) for a positive expected value; None means the take-profit does not cover its own costs.
    required_lift is p_breakeven minus the P(TP first) of a driftless random walk.
    """
    rows = []
    for entry_mode in ("taker", "maker"):
        cm = CostModel(fees=fees, entry_mode=entry_mode, spread_bps=spread_bps)
        for tf, atr in atrs.items():
            if atr is None:
                continue
            for k_tp, k_sl in BARRIER_GRID:
                tp, sl = k_tp * atr, k_sl * atr
                p_be = cm.p_breakeven(tp, sl)
                lift = cm.required_lift(tp, sl)
                rows.append({
                    "entry": entry_mode, "atr_tf": tf, "k_tp": k_tp, "k_sl": k_sl,
                    "tp_bps": round_bps(tp), "sl_bps": round_bps(sl),
                    "cost_tp_bps": round_bps(cm.cost_tp), "cost_sl_bps": round_bps(cm.cost_sl),
                    "p_breakeven": None if p_be is None else round(p_be, 3),
                    "p_random": round(k_sl / (k_tp + k_sl), 3),
                    "required_lift": None if lift is None else round(lift, 3),
                })
    return rows


def probe_level2(product_id, key, secret, timeout_s=10.0):
    """Subscribes to level2 and waits for the first l2_data message or an error."""
    result = {"status": "timeout", "detail": None}
    done = threading.Event()

    def on_message(raw):
        msg = json.loads(raw)
        if msg.get("type") == "error":
            result.update(status="error", detail=msg.get("message"))
            done.set()
        elif msg.get("channel") == "l2_data":
            result.update(status="ok", detail="l2_data received")
            done.set()

    try:
        ws = WSClient(api_key=key, api_secret=secret, on_message=on_message, retry=False)
        ws.open()
        ws.subscribe(product_ids=[product_id], channels=["level2"])
        done.wait(timeout_s)
        ws.close()
    except Exception as e:  # report, never crash the probe
        result.update(status="exception", detail=f"{type(e).__name__}: {e}")
    return result


def print_feasibility(report):
    fees = report["fees_used_for_feasibility"]
    label = "ASSUMED" if fees.get("assumed") else "account"
    print(f"\nFeasibility (fees {label}: maker {fees['maker_bps']} bps, taker {fees['taker_bps']} bps)")
    print("p_be = min P(TP first) for positive EV; lift = p_be - P(TP first) of a random walk")
    print(f"{'product':<9} {'entry':<6} {'atr':<4} {'k':<8} {'tp':>6} {'cost_tp':>8} {'sl':>6} {'cost_sl':>8}"
          f" {'p_be':>6} {'lift':>6}")
    for pid, entry in report["products"].items():
        for r in entry.get("feasibility") or []:
            p_be = "n/a" if r["p_breakeven"] is None else f"{r['p_breakeven']:.2f}"
            lift = "n/a" if r["required_lift"] is None else f"{r['required_lift']:+.2f}"
            print(f"{pid:<9} {r['entry']:<6} {r['atr_tf']:<4} {r['k_tp']:.0f}/{r['k_sl']:.0f}{'':<5}"
                  f"{r['tp_bps']:>6} {r['cost_tp_bps']:>8} {r['sl_bps']:>6} {r['cost_sl_bps']:>8}"
                  f" {p_be:>6} {lift:>6}")


def section(report, name, fn, *args):
    try:
        report[name] = fn(*args)
    except Exception as e:
        report[name] = {"error": f"{type(e).__name__}: {e}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--products", nargs="+", default=["BTC-EUR", "ETH-EUR", "SOL-EUR", "XLM-EUR"])
    ap.add_argument("--public-only", action="store_true")
    ap.add_argument("--out", default="data/probe/coinbase_probe.json")
    args = ap.parse_args()

    creds = CoinbaseConfig()  # COINBASE_API_KEY / COINBASE_API_SECRET from env only
    authed = creds.has_credentials and not args.public_only
    key = creds.api_key.get_secret_value() if authed and creds.api_key else None
    secret = creds.api_secret.get_secret_value() if authed and creds.api_secret else None
    public = RESTClient()
    private = RESTClient(api_key=key, api_secret=secret) if authed else None

    report = {"generated_at": datetime.now(UTC).isoformat(), "authenticated": authed}
    if authed:
        section(report, "permissions", probe_permissions, private)
        section(report, "fees", probe_fees, private)
    fees = fees_for_feasibility(report.get("fees"))
    report["fees_used_for_feasibility"] = fees.model_dump(mode="json", exclude={"as_of"})

    now = datetime.now(UTC)
    report["products"] = {}
    for pid in args.products:
        entry = {}
        section(entry, "meta", probe_product, public, pid)
        atrs = {}
        for tf, (granularity, days) in ATR_TIMEFRAMES.items():
            try:
                atrs[tf] = atr_bps(get_candles(public, pid, now - timedelta(days=days), now, granularity))
            except Exception as e:
                atrs[tf] = None
                entry[f"atr_{tf}_error"] = f"{type(e).__name__}: {e}"
        entry["atr_14_bps"] = atrs
        section(entry, "history_1m_candles_per_300min", probe_history_depth, public, pid)
        meta = entry.get("meta")
        spread = meta.get("spread_bps") if isinstance(meta, dict) else None
        entry["feasibility"] = feasibility(atrs, spread, fees) if spread is not None else None
        report["products"][pid] = entry

    probe_pid = args.products[0]
    report["level2_unauthenticated"] = probe_level2(probe_pid, None, None)
    if authed:
        report["level2_authenticated"] = probe_level2(probe_pid, key, secret)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)

    print(json.dumps(report, indent=2))
    print_feasibility(report)
    perms = report.get("permissions", {})
    if perms.get("can_trade"):
        print("\nWARNING: this key can trade. For the dry run create a View-only key.")
    print(f"\nSaved to {args.out}")


if __name__ == "__main__":
    main()
