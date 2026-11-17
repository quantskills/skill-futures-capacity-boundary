"""Q7 期货容量边界：资金放大后在哪里失效。

对一个冻结策略的订单，在一组资金档上逐约束检查，给出分区（安全/条件/高风险/失效）与最先失效约束。
约束：资金下限（整手取整）、日内参与率、冲击与成本、移仓压力、保证金跑道（T_liq vs T_run）、持仓占比（限仓代理）。
真实冲击无法校准：冲击系数只给假设网格，结论是"假设下的容量区间"。
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from futures_replay.data import Panel, load_min  # noqa: E402
from futures_replay.sim import Config, StrategyS0, simulate, summary, chain_returns  # noqa: E402

CAPITALS = [5e6, 1e7, 2e7, 5e7, 1e8, 2e8, 5e8, 1e9, 2e9, 5e9]


@dataclass
class Assume:
    """一套假设。optimistic / central / pessimistic 三套事前固定，不按结果调整。"""
    name: str
    impact_y: float          # 平方根冲击系数 Y
    rho_max: float           # 窗口参与率上限
    window_min: int          # 执行窗口（分钟）
    adverse_share: float     # 压力情景中逆向触板的名义占比 φ
    lock_days: int           # 无流动性锁板天数
    oi_share_max: float      # 单合约持仓占持仓量上限（限仓代理）
    fee_bp: float            # 单边费用 bp
    half_spread_tick: float  # 半价差（tick）
    margin_hike: float       # 压力期保证金上调（百分点）
    stress_vol_q: float      # 压力期日成交量分位数（取历史低分位）
    floor_err: float = 0.25  # 资金下限：整手取整误差容忍
    cost_share_max: float = 0.5   # 成本占毛收益上限
    cost_cap_frac: float = 0.01   # 毛收益不为正时的退路：年化成本占资金上限


ASSUMPTIONS = {
    "optimistic": Assume("optimistic", 0.3, 0.20, 60, 0.50, 1, 0.10, 0.5, 0.5, 0.0, 0.10, cost_cap_frac=0.02),
    "central": Assume("central", 0.5, 0.10, 30, 0.75, 2, 0.05, 1.0, 0.5, 0.03, 0.05, cost_cap_frac=0.01),
    "pessimistic": Assume("pessimistic", 1.0, 0.05, 15, 1.00, 3, 0.02, 2.0, 1.0, 0.05, 0.02, cost_cap_frac=0.005),
}


# ---------------------------------------------------------------- 订单
def orders_at(panel: Panel, capital: float, start: str, end: str, strat_cls=StrategyS0) -> tuple[pd.DataFrame, dict, object]:
    """理想成交下的冻结策略成交清单（费用、滑点为 0，参与率不设限），由容量模块自己加约束。"""
    cfg = Config(capital=capital, fee_bp=0.0, slip_ticks=0.0, part_cap=1.0)
    tr = simulate(panel, strat_cls(cfg), cfg, start=start, end=end)
    rows = []
    for (i, j, c, lots, px, kind) in tr.fills:
        rows.append({"i": i, "date": panel.dates[i], "product": panel.products[j], "contract": c, "lots": float(lots),
                     "price": px, "mult": panel.mult[i, j], "kind": kind})
    df = pd.DataFrame(rows)
    if len(df):
        df["notional"] = (df.lots.abs() * df.price * df.mult)
    return df, summary(tr), tr


# ---------------------------------------------------------------- 分钟窗口
class MinuteBook:
    def __init__(self):
        self.cache: dict[str, pd.DataFrame] = {}

    def day(self, contract: str, date: str) -> pd.DataFrame:
        if contract not in self.cache:
            m = load_min(contract, "5")
            if len(m):
                m["hm"] = m["datetime"].dt.strftime("%H%M")
            self.cache[contract] = m
        m = self.cache[contract]
        if m.empty:
            return m
        d = m[(m.trading_date == date) & (m.hm >= "0905") & (m.hm <= "1500")]
        return d

    def window_stats(self, contract: str, date: str, wins=(15, 30, 60)) -> dict:
        d = self.day(contract, date)
        if d.empty:
            return {}
        out = {"v_day_session": float(d.volume.sum()), "arrival": float(d.open.iloc[0]),
               "sigma_day_5m": float(np.log(d.close).diff().std() * math.sqrt(len(d))) if len(d) > 3 else np.nan}
        for w in wins:
            k = max(1, w // 5)
            dw = d.iloc[:k]
            out[f"v_{w}"] = float(dw.volume.sum())
            lr = np.log(dw.close).diff().dropna()
            out[f"sigma_{w}"] = float(lr.std() * math.sqrt(k)) if len(lr) > 2 else np.nan
        return out

    def capped_execution(self, contract: str, date: str, lots: float, rho_max: float, side: float) -> dict:
        """按 ρ_max × 每根 5 分钟成交量 逐根成交，最多跨 3 个交易日；返回完成根数与相对到达价的执行偏差（bp，正=不利）。"""
        m = self.cache.get(contract)
        if m is None or m.empty:
            self.day(contract, date)
            m = self.cache.get(contract)
        if m is None or m.empty:
            return {}
        d = m[(m.trading_date >= date) & (m.hm >= "0905") & (m.hm <= "1500")]
        days = sorted(d.trading_date.unique())[:3]
        d = d[d.trading_date.isin(days)]
        if d.empty:
            return {}
        arrival = float(d.open.iloc[0])
        rem, notional, filled, bars = abs(lots), 0.0, 0.0, 0
        for r in d.itertuples():
            bars += 1
            cap = math.floor(rho_max * r.volume)
            x = min(rem, cap)
            if x > 0:
                px = (r.open + r.close) / 2.0
                notional += x * px
                filled += x
                rem -= x
            if rem <= 0:
                break
        if filled == 0:
            return {"bars": bars, "complete": False, "is_bp": np.nan}
        vwap = notional / filled
        return {"bars": bars, "complete": rem <= 0, "residual_share": rem / abs(lots),
                "is_bp": float(side * (vwap / arrival - 1.0) * 1e4)}


# ---------------------------------------------------------------- 主计算
@dataclass
class Q7Result:
    capital_table: pd.DataFrame
    trade_table: pd.DataFrame
    roll_table: pd.DataFrame
    runway_table: pd.DataFrame
    zones: pd.DataFrame
    tornado: pd.DataFrame
    meta: dict = field(default_factory=dict)


def per_trade_metrics(trades: pd.DataFrame, mb: MinuteBook) -> pd.DataFrame:
    stats = {}
    for (c, d) in trades[["contract", "date"]].drop_duplicates().itertuples(index=False):
        stats[(c, d)] = mb.window_stats(c, d)
    rows = []
    for r in trades.itertuples(index=False):
        s = stats.get((r.contract, r.date), {})
        rows.append({**r._asdict(), **s})
    return pd.DataFrame(rows)


def daily_volume_profile(panel: Panel, start: str, end: str) -> pd.DataFrame:
    i0, i1 = panel.idx(start), panel.idx(end)
    v = pd.DataFrame(panel.f["volume"][i0:i1], index=panel.dates[i0:i1], columns=panel.products)
    oi = pd.DataFrame(panel.f["open_interest"][i0:i1], index=panel.dates[i0:i1], columns=panel.products)
    return v, oi


# ---------------------------------------------------------------- 约束检查
def _daily_sigma(panel: Panel) -> np.ndarray:
    r = chain_returns(panel)
    s = pd.DataFrame(r).rolling(20, min_periods=10).std().values
    return s


def evaluate_capital(panel: Panel, trades: pd.DataFrame, tr, A: Assume, gross_annual_pnl: float,
                     capital: float, v_stress: dict, sig_d: np.ndarray) -> dict:
    """单个资金档 × 单套假设的约束检查。trades 已带分钟窗口统计。"""
    out = {"capital": capital, "assume": A.name}
    t = trades.copy()
    vw = f"v_{A.window_min}"
    reb = t[t.kind == "trade"]
    rol_o = t[t.kind == "roll_out"]
    # 1 资金下限：整手取整误差
    errs, zeros = [], 0
    for (i, j, dq, tgt, w, _due) in tr.orders:
        if w == 0:
            continue
        eq = tr.equity[i]
        s = panel.f["settlement"][i, j] * panel.mult[i, j]
        want = abs(w) * eq
        got = abs(tgt) * s
        if tgt == 0:
            zeros += 1
        errs.append(abs(got - want) / want if want > 0 else np.nan)
    errs = np.array(errs, dtype=float)
    out["floor_median_err"] = float(np.nanmedian(errs)) if len(errs) else np.nan
    out["floor_zero_share"] = zeros / max(1, len(errs))
    out["ok_floor"] = bool(out["floor_median_err"] <= A.floor_err and out["floor_zero_share"] <= 0.2)
    # 2 日内参与率
    rho = (reb.lots.abs() / reb[vw]).replace([np.inf], np.nan) if vw in reb else pd.Series(dtype=float)
    out["rho_p50"] = float(rho.quantile(0.5)) if rho.notna().any() else np.nan
    out["rho_p95"] = float(rho.quantile(0.95)) if rho.notna().any() else np.nan
    out["rho_cov"] = float(rho.notna().mean()) if len(rho) else 0.0
    out["ok_participation"] = bool(not np.isfinite(out["rho_p95"]) or out["rho_p95"] <= A.rho_max)
    # 3 成本：费用 + 半价差 + 平方根冲击（日 σ、日量；成交量时间下窗口不改变该估计）
    ii, jj = reb.i.values, reb["product"].map({p: k for k, p in enumerate(panel.products)}).values
    vd = panel.f["volume"][ii, jj]
    sd = sig_d[ii, jj]
    tick = reb["product"].map(panel.tick).values
    with np.errstate(invalid="ignore", divide="ignore"):
        frac = A.fee_bp * 1e-4 + A.half_spread_tick * tick / reb.price.values + A.impact_y * sd * np.sqrt(reb.lots.abs().values / vd)
    frac = np.where(np.isfinite(frac), frac, A.fee_bp * 1e-4)
    cost = float(np.nansum(frac * reb.notional.values))
    fixed_frac = A.fee_bp * 1e-4 + A.half_spread_tick * tick / reb.price.values
    cost_fixed = float(np.nansum(np.where(np.isfinite(fixed_frac), fixed_frac, A.fee_bp * 1e-4) * reb.notional.values))
    rcost = 0.0
    if len(rol_o):
        rc = (A.fee_bp * 1e-4 + A.half_spread_tick * rol_o["product"].map(panel.tick).values / rol_o.price.values) * rol_o.notional.values * 2
        rcost = float(np.nansum(rc))
    yrs = max(1e-9, (tr.i1 - tr.i0) / 252)
    out["cost_annual"] = (cost + rcost) / yrs
    out["cost_fixed_annual"] = (cost_fixed + rcost) / yrs
    out["cost_impact_annual"] = (cost - cost_fixed) / yrs
    out["fixed_cost_share"] = out["cost_fixed_annual"] / gross_annual_pnl if gross_annual_pnl > 0 else np.nan
    out["gross_annual"] = gross_annual_pnl
    out["cost_share"] = out["cost_annual"] / gross_annual_pnl if gross_annual_pnl > 0 else np.nan
    out["net_annual_ret"] = (gross_annual_pnl - out["cost_annual"]) / capital
    out["gross_edge"] = bool(gross_annual_pnl > 0)
    if out["gross_edge"]:
        out["ok_cost"] = bool(out["cost_annual"] <= A.cost_share_max * gross_annual_pnl)
    else:   # 退路：策略本身没有毛收益时，只能按"成本占资金"的预算判断
        out["ok_cost"] = bool(out["cost_annual"] / capital <= A.cost_cap_frac)
    out["cost_frac_capital"] = out["cost_annual"] / capital
    # 4 移仓：旧腿窗口参与率
    if len(rol_o) and vw in rol_o:
        rr = (rol_o.lots.abs() / rol_o[vw]).replace([np.inf], np.nan)
        out["roll_rho_p95"] = float(rr.quantile(0.95)) if rr.notna().any() else np.nan
    else:
        out["roll_rho_p95"] = np.nan
    out["ok_roll"] = bool(not np.isfinite(out["roll_rho_p95"]) or out["roll_rho_p95"] <= A.rho_max)
    # 5 保证金跑道 + 6 持仓占比
    bad, n, oi_max, gaps = 0, 0, 0.0, []
    lim = np.array([panel.limit_pct[p] for p in panel.products])
    bm = np.array([panel.base_margin[p] for p in panel.products])
    vs = np.array([v_stress[p] for p in panel.products])
    for i in np.where(np.isfinite(tr.target[:, 0]))[0]:
        tgt = tr.target[i]
        if not np.any(tgt != 0):
            continue
        n += 1
        notion = np.abs(tgt) * panel.f["settlement"][i] * panel.mult[i]
        notion = np.where(np.isfinite(notion), notion, 0.0)
        eq = tr.equity[i]
        loss = A.adverse_share * float(np.sum(notion * lim))
        maint = float(np.sum(notion * (bm + 0.03 + A.margin_hike)))
        t_run = math.floor((eq - maint) / loss) if loss > 0 else 999
        with np.errstate(invalid="ignore", divide="ignore"):
            dl = np.ceil(np.abs(tgt) / (A.rho_max * vs))
        dl = np.where(np.isfinite(dl), dl, 0.0)
        t_liq = A.lock_days + float(dl.max())
        gaps.append(t_liq - t_run)
        if t_liq > t_run:
            bad += 1
        oi = panel.f["open_interest"][i]
        with np.errstate(invalid="ignore", divide="ignore"):
            share = np.where(oi > 0, np.abs(tgt) / oi, 0.0)
        oi_max = max(oi_max, float(np.nanmax(share)))
    out["runway_fail_share"] = bad / max(1, n)
    out["runway_gap_p95"] = float(np.percentile(gaps, 95)) if gaps else np.nan
    out["ok_runway"] = bool(out["runway_fail_share"] <= 0.05)
    out["oi_share_max"] = oi_max
    out["ok_oi"] = bool(oi_max <= A.oi_share_max)
    checks = ["ok_floor", "ok_participation", "ok_cost", "ok_roll", "ok_runway", "ok_oi"]
    out["ok_all_upper"] = all(out[c] for c in checks if c != "ok_floor")
    out["ok_size"] = all(out[c] for c in ["ok_participation", "ok_roll", "ok_runway", "ok_oi"])   # 只看规模约束，不看赚不赚钱
    out["failed"] = ",".join(c[3:] for c in checks if not out[c])
    return out
