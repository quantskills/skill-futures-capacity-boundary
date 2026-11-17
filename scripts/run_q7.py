"""Q7 全流程：订单 → 分钟窗口 → 资金档 × 假设 → 分区、约束归因、扰动、保留期、压力日、ADV 基线。"""
from __future__ import annotations

import json
import math
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from futures_replay.data import build_panel, manifest_of  # noqa: E402
from capacity import (ASSUMPTIONS, CAPITALS, MinuteBook, evaluate_capital, orders_at,  # noqa: E402
                      per_trade_metrics, _daily_sigma)
from futures_replay.data import TICK as _TICK  # noqa: E402
from futures_replay.sim import StrategyS0  # noqa: E402

PRODUCTS = ["RB", "I", "CU", "AL", "M", "P", "MA", "TA", "SA", "AP", "JD"]
FULL = ("20200102", "20261113")
DEV = ("20200102", "20231229")
HOLD = ("20240102", "20261113")
OUT = HERE / "out"
BASELINE_SCRIPT = HERE / "vendor" / "capacity_tca_6ae79f7.py"   # 冻结基线：原样附带，固定 commit
BASELINE_COMMIT = "6ae79f7"


def v_stress_for(panel, q, start, end):
    i0, i1 = panel.idx(start), panel.idx(end)
    v = panel.f["volume"][i0:i1]
    return {p: float(np.nanquantile(v[:, j][v[:, j] > 0], q)) for j, p in enumerate(panel.products)}


def gross_annual(tr):
    p = tr.pnl[tr.i0:tr.i1]
    return float(np.nanmean(p) * 252)


def run_window(panel, start, end, mb, label, capitals=CAPITALS, assumptions=ASSUMPTIONS):
    sig_d = _daily_sigma(panel)
    rows, trades_all, cache = [], [], {}
    for C in capitals:
        trades, summ, tr = orders_at(panel, C, start, end)
        if trades.empty:
            continue
        trades = per_trade_metrics(trades, mb)
        trades["capital"] = C
        trades_all.append(trades)
        g = gross_annual(tr)
        cache[C] = (trades, tr, g, summ)
        for A in assumptions.values():
            vs = v_stress_for(panel, A.stress_vol_q, start, end)
            r = evaluate_capital(panel, trades, tr, A, g, C, vs, sig_d)
            r.update({"window": label, "gross_ret_ideal": summ.get("ann_ret"), "sharpe_ideal": summ.get("sharpe")})
            rows.append(r)
    return pd.DataFrame(rows), pd.concat(trades_all, ignore_index=True), cache


def zones(table: pd.DataFrame) -> pd.DataFrame:
    out = []
    for C, g in table.groupby("capital"):
        ok = {r.assume: (r.ok_all_upper and r.ok_floor) for r in g.itertuples()}
        floor_ok = bool(g[g.assume == "central"].ok_floor.iloc[0])
        if not floor_ok:
            z = "资金过小"
        elif ok.get("pessimistic"):
            z = "安全"
        elif ok.get("central"):
            z = "条件"
        elif ok.get("optimistic"):
            z = "高风险"
        else:
            z = "失效"
        oks = {r.assume: (r.ok_size and r.ok_floor) for r in g.itertuples()}
        if not floor_ok:
            zs = "资金过小"
        elif oks.get("pessimistic"):
            zs = "安全"
        elif oks.get("central"):
            zs = "条件"
        elif oks.get("optimistic"):
            zs = "高风险"
        else:
            zs = "失效"
        fc = g[g.assume == "central"].failed.iloc[0]
        out.append({"capital": C, "zone": z, "zone_size_only": zs, "failed_central": fc,
                    "failed_pessimistic": g[g.assume == "pessimistic"].failed.iloc[0],
                    "net_ret_central": float(g[g.assume == "central"].net_annual_ret.iloc[0])})
    return pd.DataFrame(out).sort_values("capital")


def max_ok_capital_size(table: pd.DataFrame, assume: str) -> float:
    """只看规模约束（参与率、移仓、保证金跑道、持仓占比）的最大可行资金档。"""
    g = table[(table.assume == assume)].sort_values("capital")
    cap = 0.0
    for r in g.itertuples():
        if r.ok_size:
            cap = r.capital
        elif r.ok_floor:
            break
    return cap


def max_ok_capital(table: pd.DataFrame, assume: str) -> float:
    g = table[(table.assume == assume)].sort_values("capital")
    ok = g[g.ok_all_upper & g.ok_floor]
    if ok.empty:
        return 0.0
    # 取第一个失败档之前的最大通过档（单调意义下的上限）
    cap = 0.0
    for r in g.itertuples():
        if r.ok_all_upper:
            cap = r.capital
        elif r.ok_floor:
            break
    return cap


def product_binding(panel, trades: pd.DataFrame, A, start, end) -> pd.DataFrame:
    """按品种：参与率、移仓旧腿参与率、持仓占比各自首个失效资金档。"""
    vw = f"v_{A.window_min}"
    rows = []
    for p in panel.products:
        first = {}
        for C, g in trades[trades["product"] == p].groupby("capital"):
            reb = g[g.kind == "trade"]
            rol = g[g.kind == "roll_out"]
            rp = (reb.lots.abs() / reb[vw]).replace([np.inf], np.nan).quantile(0.95) if vw in reb and len(reb) else np.nan
            ro = (rol.lots.abs() / rol[vw]).replace([np.inf], np.nan).quantile(0.95) if vw in rol and len(rol) else np.nan
            if np.isfinite(rp) and rp > A.rho_max and "participation" not in first:
                first["participation"] = C
            if np.isfinite(ro) and ro > A.rho_max and "roll" not in first:
                first["roll"] = C
        rows.append({"product": p, **{f"first_fail_{k}": v for k, v in first.items()}})
    df = pd.DataFrame(rows)
    cols = [c for c in df.columns if c.startswith("first_fail_")]
    df["binding"] = df[cols].idxmin(axis=1, skipna=True).str.replace("first_fail_", "") if cols else None
    df["binding_capital"] = df[cols].min(axis=1) if cols else np.nan
    return df.sort_values("binding_capital")


def tornado(panel, cache, start, end) -> pd.DataFrame:
    sig_d = _daily_sigma(panel)
    base = ASSUMPTIONS["central"]
    fields = ["rho_max", "window_min", "adverse_share", "lock_days", "oi_share_max", "margin_hike", "stress_vol_q"]
    out = []

    def cap_for(A):
        best = 0.0
        vs = v_stress_for(panel, A.stress_vol_q, start, end)
        for C in sorted(cache):
            trades, tr, g, _ = cache[C]
            r = evaluate_capital(panel, trades, tr, A, g, C, vs, sig_d)
            if r["ok_size"]:          # 只看规模约束；经济可行性另算
                best = C
            else:
                break
        return best
    c0 = cap_for(base)
    for fld in fields:
        lo = replace(base, **{fld: getattr(ASSUMPTIONS["optimistic"], fld)}, name=f"{fld}=opt")
        hi = replace(base, **{fld: getattr(ASSUMPTIONS["pessimistic"], fld)}, name=f"{fld}=pess")
        co, cp = cap_for(lo), cap_for(hi)
        out.append({"assumption": fld, "central_value": getattr(base, fld), "opt_value": getattr(lo, fld),
                    "pess_value": getattr(hi, fld), "cap_central": c0, "cap_if_opt": co, "cap_if_pess": cp,
                    "swing_x": (co / cp) if cp > 0 else np.inf})
    return pd.DataFrame(out).sort_values("swing_x", ascending=False)


def econ_sensitivity(cache, base_c) -> dict:
    """经济可行性：在最小资金档上（几乎没有冲击）只看固定成本（费用 + 半价差）占毛收益的比例，按费用与半价差取值扫描。"""
    from futures_replay.data import build_panel  # noqa: F401
    trades, tr, g, _ = cache[min(cache)]
    reb = trades[trades.kind == "trade"]
    rol = trades[trades.kind == "roll_out"]
    yrs = max(1e-9, (tr.i1 - tr.i0) / 252)
    out = {"capital": min(cache), "gross_annual": g, "grid": []}
    tick = reb["product"].map(_TICK).values
    rtick = rol["product"].map(_TICK).values
    for fee in (0.5, 1.0, 2.0):
        for hs in (0.0, 0.5, 1.0):
            c = ((fee * 1e-4 + hs * tick / reb.price.values) * reb.notional.values).sum()
            c += ((fee * 1e-4 + hs * rtick / rol.price.values) * rol.notional.values * 2).sum()
            ca = c / yrs
            out["grid"].append({"fee_bp": fee, "half_spread_tick": hs, "fixed_cost_annual": ca,
                                "fixed_cost_share": ca / g if g > 0 else None})
    return out


def volume_forecast_error(panel, start, end) -> pd.DataFrame:
    i0, i1 = panel.idx(start), panel.idx(end)
    v = pd.DataFrame(panel.f["volume"], columns=panel.products)
    fc = v.shift(1).rolling(20, min_periods=10).median()
    ratio = (v / fc).iloc[i0:i1]
    rows = []
    for p in panel.products:
        x = ratio[p].replace([np.inf, -np.inf], np.nan).dropna()
        x = x[x > 0]
        rows.append({"product": p, "realized_over_forecast_p05": float(x.quantile(0.05)),
                     "p50": float(x.quantile(0.5)), "p95": float(x.quantile(0.95))})
    return pd.DataFrame(rows)


def stress_days(panel, start, end) -> dict:
    """事前规则：品种日成交量处于样本最低 5%，或 |收益| 处于最高 5%，或收盘在涨跌停。"""
    from futures_replay.sim import chain_returns
    i0, i1 = panel.idx(start), panel.idx(end)
    r = np.abs(chain_returns(panel))[i0:i1]
    v = panel.f["volume"][i0:i1]
    c, lu, ld = panel.f["close"][i0:i1], panel.f["limit_up"][i0:i1], panel.f["limit_down"][i0:i1]
    flags = {}
    for j, p in enumerate(panel.products):
        lowv = v[:, j] <= np.nanquantile(v[:, j], 0.05)
        hir = r[:, j] >= np.nanquantile(r[:, j], 0.95)
        lim = (np.abs(c[:, j] - lu[:, j]) < 1e-9) | (np.abs(c[:, j] - ld[:, j]) < 1e-9)
        flags[p] = set(panel.dates[i0:i1][lowv | hir | lim])
    return flags


def roll_decay(panel, start, end, kmax=15) -> pd.DataFrame:
    """换月后第 k 个交易日，旧合约日成交量 / 换月当日旧合约成交量。用于判断移仓最晚能拖几天。"""
    from futures_replay.data import _read_daily, fix_double_sided
    rows = []
    i0, i1 = panel.idx(start), panel.idx(end)
    for j, prod in enumerate(panel.products):
        c = panel.contract[:, j]
        for i in range(max(1, i0), i1):
            if c[i] != c[i - 1] and c[i - 1] is not None:
                old = _read_daily(c[i - 1])
                if old is None:
                    continue
                old = fix_double_sided(old.reset_index(), panel.exchange[prod]).set_index("date")
                ds = list(panel.dates[i:i + kmax + 1])
                v = old.reindex(ds)["volume"].values
                if len(v) and np.isfinite(v[0]) and v[0] > 0:
                    for k, x in enumerate(v):
                        rows.append({"product": prod, "switch_date": panel.dates[i], "k": k, "ratio": float(x / v[0]) if np.isfinite(x) else 0.0})
    return pd.DataFrame(rows)


def delay_cost_sample(trades, mb, A, capital, n=150, seed=7) -> pd.DataFrame:
    vw = f"v_{A.window_min}"
    t = trades[(trades.capital == capital) & (trades.kind == "trade")].copy()
    t["rho"] = t.lots.abs() / t[vw]
    t = t[t.rho > A.rho_max]
    if t.empty:
        return pd.DataFrame()
    t = t.sample(min(n, len(t)), random_state=seed)
    rows = []
    for r in t.itertuples():
        x = mb.capped_execution(r.contract, r.date, r.lots, A.rho_max, np.sign(r.lots))
        rows.append({"product": r.product, "date": r.date, "rho": r.rho, **x})
    return pd.DataFrame(rows)


def run_baseline(trades_base: pd.DataFrame, panel, alpha: float, nav: float, outdir: Path) -> dict:
    """冻结基线：skill-strategy-capacity-tca 默认参数，日成交额 20 日均值作 ADV。"""
    outdir.mkdir(parents=True, exist_ok=True)
    t = trades_base[trades_base.kind == "trade"].copy()
    t = t.assign(symbol=t["product"], side=np.where(t.lots > 0, "buy", "sell"), notional=t.notional,
                 date=t["date"].map(lambda x: f"{str(x)[:4]}-{str(x)[4:6]}-{str(x)[6:8]}"))
    t[["date", "symbol", "side", "notional"]].to_csv(outdir / "trades.csv", index=False)
    amt = pd.DataFrame(panel.f["amount"], index=panel.dates, columns=panel.products).rolling(20, min_periods=5).mean()
    adv = amt.stack().reset_index()
    adv.columns = ["date", "symbol", "adv_notional"]
    adv["date"] = adv["date"].map(lambda x: f"{str(x)[:4]}-{str(x)[4:6]}-{str(x)[6:8]}")
    adv = adv[adv.date.isin(set(t.date))]
    adv.to_csv(outdir / "adv.csv", index=False)
    cmd = [sys.executable, str(BASELINE_SCRIPT), "--trades", str(outdir / "trades.csv"),
           "--adv", str(outdir / "adv.csv"), "--alpha", f"{alpha:.6f}", "--nav", f"{nav:.0f}", "--out", str(outdir), "--no-html"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    res = {"cmd": " ".join(cmd[1:]), "returncode": p.returncode, "stderr_tail": p.stderr[-400:]}
    js = outdir / "capacity_tca.json"
    if js.exists():
        res["result"] = json.loads(js.read_text())
    res["baseline_commit"] = BASELINE_COMMIT
    res["baseline_script_sha256_16"] = __import__("hashlib").sha256(BASELINE_SCRIPT.read_bytes()).hexdigest()[:16]
    return res


def main(products=None, full_w=None, dev_w=None, hold_w=None, capitals=None, out=None, assumptions=None):
    global OUT
    products = products or PRODUCTS
    FULL_, DEV_, HOLD_ = full_w or FULL, dev_w or DEV, hold_w or HOLD
    capitals = capitals or CAPITALS
    if assumptions:
        ASSUMPTIONS.clear()
        ASSUMPTIONS.update(assumptions)
    OUT = Path(out) if out else OUT
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    panel = build_panel(products)
    mb = MinuteBook()
    full, trades, cache = run_window(panel, *FULL_, mb, "full", capitals=capitals)
    print("full done", round(time.time() - t0), "s", flush=True)
    dev, _, _ = run_window(panel, *DEV_, mb, "dev", capitals=capitals)
    hold, _, _ = run_window(panel, *HOLD_, mb, "holdout", capitals=capitals)
    print("windows done", round(time.time() - t0), "s", flush=True)
    table = pd.concat([full, dev, hold], ignore_index=True)
    z = zones(full)
    caps = {w: {a: max_ok_capital(table[table.window == w], a) for a in ASSUMPTIONS} for w in ["full", "dev", "holdout"]}
    caps_size = {w: {a: max_ok_capital_size(table[table.window == w], a) for a in ASSUMPTIONS} for w in ["full", "dev", "holdout"]}
    binding = product_binding(panel, trades, ASSUMPTIONS["central"], *FULL_)
    torn = tornado(panel, cache, *FULL_)
    vfe = volume_forecast_error(panel, *FULL_)
    sd = stress_days(panel, *FULL_)
    trades["stress_day"] = [r.date in sd.get(r.product, set()) for r in trades.itertuples()]
    A = ASSUMPTIONS["central"]
    vw = f"v_{A.window_min}"
    reb = trades[trades.kind == "trade"].copy()
    reb["rho"] = reb.lots.abs() / reb[vw]
    stress_cmp = reb.groupby(["capital", "stress_day"]).rho.quantile(0.95).unstack()
    first_c = next((C for C in sorted(cache) if (stress_cmp.loc[C].get(False, 0) or 0) > A.rho_max), None)
    delay = delay_cost_sample(trades, mb, A, first_c) if first_c else pd.DataFrame()
    rd = roll_decay(panel, *FULL_)
    rd.to_csv(OUT / "roll_decay.csv", index=False)
    base_c = 1e8 if 1e8 in cache else sorted(cache)[len(cache) // 2]
    base_trades, base_tr, base_g, base_s = cache[base_c]
    baseline = run_baseline(base_trades, panel, alpha=max(1e-6, base_s.get("ann_ret", 0.0)), nav=base_c, outdir=OUT / "baseline_adv")
    meta = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "elapsed_s": round(time.time() - t0, 1),
            "windows": {"full": FULL_, "dev": DEV_, "holdout": HOLD_}, "capitals": capitals,
            "assumptions": {k: asdict(v) for k, v in ASSUMPTIONS.items()}, "max_ok_capital": caps, "max_ok_capital_size_only": caps_size,
            "first_capital_participation_binding_central": first_c, "data": manifest_of(panel),
            "strategy": {"name": StrategyS0.name, "version": StrategyS0.version, "ideal_summary_base": base_s, "base_capital": base_c},
            "econ_sensitivity": econ_sensitivity(cache, base_c), "baseline": baseline,
            "minute_coverage": float(reb[vw].notna().mean())}
    table.to_csv(OUT / "capital_table.csv", index=False)
    z.to_csv(OUT / "zones.csv", index=False)
    binding.to_csv(OUT / "product_binding.csv", index=False)
    torn.to_csv(OUT / "tornado.csv", index=False)
    vfe.to_csv(OUT / "volume_forecast_error.csv", index=False)
    stress_cmp.to_csv(OUT / "stress_vs_normal_rho_p95.csv")
    delay.to_csv(OUT / "delay_cost_sample.csv", index=False)
    trades.to_parquet(OUT / "trades_by_capital.parquet", index=False)
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({"caps": caps, "first_c": first_c, "elapsed": meta["elapsed_s"]}, default=str))
    print(z.to_string(index=False))
    return OUT


if __name__ == "__main__":
    main()
