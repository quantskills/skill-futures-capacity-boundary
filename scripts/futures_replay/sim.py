"""日频回放器 + 被测策略 S0（公开信号，不是推荐策略）。

S0 = 商品期货 63 日截面动量（skill-futures-cta-alpha 因子目录中的 xsmom_63），周五收盘出信号、下一交易日开盘成交，
逆波动率加权，目标变化不足 25% 且方向不变时不调仓（不交易带），按风险卡执行。
选型记录见 ATTEMPTS.md：先试 63 日反转（该仓库自报方向），真实合约账本上毛收益为负，只做了一次方向翻转。
  R1 单品种止损 stop_pct；R2 组合回撤停机 dd_stop + 冷却；R3 保证金占用上限 margin_cap；
  R4 陈旧/缺失数据不新开仓；R5 主力标签切换当日开盘换月。

账本：真实合约逐日按结算价盯市。当日 PnL = q_prev*(S_t-S_{t-1}) + Δ*(S_t-p_fill)，乘以合约乘数；
费用 = |Δ|*p*mult*fee_bp；成交价 = 开盘价 ± slip_ticks*tick，并截在涨跌停内；
锁板（最高=最低=涨/跌停）方向上的单不成交，成交量为 0 不成交，单日成交 ≤ part_cap*当日成交量。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

import numpy as np

from .data import Panel


@dataclass
class Config:
    capital: float = 1e8
    gross_leverage: float = 1.5
    max_weight: float = 0.35          # 单品种 |w| 上限（占权益）
    lookback: int = 63
    vol_window: int = 20
    stop_pct: float = 0.08
    dd_stop: float = 0.15
    dd_cooldown: int = 5
    margin_cap: float = 0.25
    margin_addon: float = 0.03        # 期货公司在交易所之上的加收（假设）
    fee_bp: float = 1.0               # 单边费用，占名义（假设，非逐品种费率表）
    slip_ticks: float = 1.0
    part_cap: float = 0.10            # 单日成交 ≤ 10% 当日成交量
    min_products: int = 5
    stale_days: int = 1
    rebalance_band: float = 0.25      # 不交易带：目标变化不足 25% 且方向不变时不调仓

    def to_dict(self):
        return asdict(self)


@dataclass
class Trace:
    dates: np.ndarray
    products: list
    equity: np.ndarray
    pnl: np.ndarray
    fees: np.ndarray
    q: np.ndarray                     # (T,N) 收盘持仓手数
    held: np.ndarray                  # (T,N) 收盘持有合约
    target: np.ndarray                # (T,N) 当日收盘后生成的目标手数（NaN=无新目标）
    margin: np.ndarray                # (T,) 保证金占用
    fills: list = field(default_factory=list)
    events: list = field(default_factory=list)
    orders: list = field(default_factory=list)
    entry: np.ndarray | None = None   # (T,N) 收盘时止损参考价
    i0: int = 0
    i1: int = 0


def chain_returns(panel: Panel) -> np.ndarray:
    c = panel.f["close"]
    r = np.full(c.shape, np.nan)
    same = panel.contract[1:] == panel.contract[:-1]
    r1 = c[1:] / c[:-1] - 1.0
    roll = panel.pf["close"][1:] / c[:-1] - 1.0
    r[1:] = np.where(same, r1, roll)
    r[~np.isfinite(r)] = np.nan
    return r


def week_end_flags(dates: np.ndarray) -> np.ndarray:
    import datetime as _dt
    wk = np.array([_dt.date(int(d[:4]), int(d[4:6]), int(d[6:])).isocalendar()[:2] for d in dates])
    nxt = np.vstack([wk[1:], [[-1, -1]]])
    return np.any(wk != nxt, axis=1)


class StrategyS0:
    name = "S0_xsmom63_weekly_band25"
    version = "1.1.0"
    # 引擎层开关：默认均为正确行为；E13 的埋缺陷版本通过子类改写
    flags = {"rebase_entry_on_roll": True, "block_locked_fill": True, "margin_uses_hike": True,
             "resubmit_full_after_partial": False, "dd_peak_reset_on_rebalance": False}

    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ---- 信号与目标 ----
    def signal(self, ret: np.ndarray, i: int) -> np.ndarray:
        lb = self.cfg.lookback
        if i < lb:
            return np.full(ret.shape[1], np.nan)
        win = ret[i - lb + 1:i + 1]
        ok = np.isfinite(win).sum(0) >= lb - 2
        cum = np.nanprod(1.0 + win, axis=0) - 1.0
        s = cum
        s[~ok] = np.nan
        return s

    def vol(self, ret: np.ndarray, i: int) -> np.ndarray:
        w = ret[max(0, i - self.cfg.vol_window + 1):i + 1]
        v = np.nanstd(w, axis=0) * math.sqrt(252)
        return np.where(v > 0.02, v, 0.02)   # 波动率下限，防止低波动时仓位失控

    def weights(self, sig: np.ndarray, vol: np.ndarray, eligible: np.ndarray) -> np.ndarray:
        n = int(eligible.sum())
        w = np.zeros_like(sig)
        if n < self.cfg.min_products:
            return w
        idx = np.where(eligible)[0]
        order = idx[np.argsort(sig[idx])]
        k = max(1, n // 3)
        w[order[-k:]] = 1.0 / vol[order[-k:]]
        w[order[:k]] = -1.0 / vol[order[:k]]
        for _ in range(3):
            g = np.abs(w).sum()
            if g == 0:
                break
            w = w / g * self.cfg.gross_leverage
            w = np.clip(w, -self.cfg.max_weight, self.cfg.max_weight)
        return w

    def lots(self, w: np.ndarray, equity: float, settle: np.ndarray, mult: np.ndarray) -> np.ndarray:
        with np.errstate(invalid="ignore", divide="ignore"):
            x = w * equity / (settle * mult)
        x = np.where(np.isfinite(x), x, 0.0)
        return np.round(x)

    # ---- 风险卡 ----
    def stop_hit(self, side: float, entry: float, px: float) -> bool:
        if side == 0 or not np.isfinite(entry) or not np.isfinite(px) or entry <= 0:
            return False
        return side * (px / entry - 1.0) < -self.cfg.stop_pct

    def dd_hit(self, equity: float, peak: float) -> bool:
        return equity / peak - 1.0 < -self.cfg.dd_stop

    def margin_scale(self, occ: float, equity: float) -> float:
        cap = self.cfg.margin_cap * equity
        return 1.0 if occ <= cap or occ <= 0 else cap / occ

    def is_stale(self, panel: Panel, i: int, j: int) -> bool:
        lo = max(0, i - self.cfg.stale_days + 1)
        c = panel.f["close"][lo:i + 1, j]
        v = panel.f["volume"][lo:i + 1, j]
        return (not np.all(np.isfinite(c))) or np.all(v <= 0)

    def roll_needed(self, held, current) -> bool:
        return held is not None and current is not None and held != current


def _fill_price(open_px, side, tick, slip, lu, ld):
    p = open_px + side * slip * tick
    if np.isfinite(lu):
        p = min(p, lu)
    if np.isfinite(ld):
        p = max(p, ld)
    return p


def _locked_against(side, hi, lo, lu, ld, tick):
    if not (np.isfinite(hi) and np.isfinite(lo)) or hi - lo > 0.5 * tick:
        return False
    if side < 0 and np.isfinite(ld) and lo <= ld + 0.5 * tick:
        return True
    if side > 0 and np.isfinite(lu) and hi >= lu - 0.5 * tick:
        return True
    return False


def _queue(pending, pending_due, j, value, due):
    """同一目标已在排队时保留原成交日，避免重复触发把订单不断往后推。"""
    if np.isfinite(pending[j]) and pending[j] == value and pending_due[j] >= 0:
        return
    pending[j] = value
    pending_due[j] = due


def simulate(panel: Panel, strategy: StrategyS0 | None = None, cfg: Config | None = None,
             start: str | None = None, end: str | None = None, margin_mult: np.ndarray | None = None,
             exec_delay=0, fill_ratio: np.ndarray | None = None, reject: np.ndarray | None = None) -> Trace:
    """回放。margin_mult/fill_ratio/reject 为攻击钩子（T×N），exec_delay 为下单延迟天数。"""
    cfg = cfg or Config()
    st = strategy or StrategyS0(cfg)
    st.cfg = cfg
    T, N = panel.T, panel.N
    i0 = panel.idx(start) if start else 0
    i1 = min(T, panel.idx(end) + 1) if end else T
    ret = chain_returns(panel)
    wend = week_end_flags(panel.dates)
    f, pf = panel.f, panel.pf
    S = f["settlement"]
    tickv = np.array([panel.tick[p] for p in panel.products])
    bm = np.array([panel.base_margin.get(p, 0.1) for p in panel.products])
    mm = margin_mult if margin_mult is not None else np.ones((T, N))
    tr = Trace(panel.dates, panel.products, np.full(T, np.nan), np.zeros(T), np.zeros(T), np.zeros((T, N)),
               np.full((T, N), None, dtype=object), np.full((T, N), np.nan), np.zeros(T), i0=i0, i1=i1)
    tr.entry = np.full((T, N), np.nan)
    eq = cfg.capital
    peak = eq
    q = np.zeros(N)
    held = [None] * N
    last_settle = np.full(N, np.nan)
    entry = np.full(N, np.nan)
    pending = np.full(N, np.nan)          # 待成交目标手数
    pending_due = np.full(N, -1)          # 最早可成交日
    blocked = np.zeros(N, dtype=bool)     # 止损后到下次调仓前禁止再开
    cooldown = 0
    for i in range(i0, i1):
        pnl = 0.0
        fee = 0.0
        delta = np.zeros(N)
        fillp = np.full(N, np.nan)
        # 1) 开盘：换月
        for j in range(N):
            cur = panel.contract[i, j]
            if q[j] != 0 and st.roll_needed(held[j], cur):
                po, ps_prev = pf["open"][i, j], last_settle[j]
                no = f["open"][i, j]
                if panel.pf_contract[i, j] == held[j] and np.isfinite(po) and np.isfinite(no):
                    side = -np.sign(q[j])
                    p_old = _fill_price(po, side, tickv[j], cfg.slip_ticks, pf["limit_up"][i, j], pf["limit_down"][i, j])
                    pnl += q[j] * (p_old - ps_prev) * panel.mult[i - 1 if i > 0 else i, j]
                    fee += abs(q[j]) * p_old * panel.mult[i - 1 if i > 0 else i, j] * cfg.fee_bp * 1e-4
                    tr.fills.append((i, j, held[j], -q[j], p_old, "roll_out"))
                    p_new = _fill_price(no, -side, tickv[j], cfg.slip_ticks, f["limit_up"][i, j], f["limit_down"][i, j])
                    delta[j] = q[j]
                    fillp[j] = p_new
                    fee += abs(q[j]) * p_new * panel.mult[i, j] * cfg.fee_bp * 1e-4
                    tr.fills.append((i, j, cur, q[j], p_new, "roll_in"))
                    if st.flags["rebase_entry_on_roll"] and np.isfinite(entry[j]) and p_old > 0:
                        entry[j] *= p_new / p_old      # 止损参考价随换月平移，避免把升贴水当亏损
                    q[j] = 0.0
                    held[j] = cur
                    last_settle[j] = np.nan
                else:
                    tr.events.append((i, j, "roll_fail", held[j], cur))
            elif q[j] == 0:
                held[j] = cur
        # 2) 开盘：执行待成交目标
        for j in range(N):
            if not np.isfinite(pending[j]) or i < pending_due[j]:
                continue
            if q[j] != 0 and held[j] != panel.contract[i, j]:
                tr.events.append((i, j, "roll_pending", float(pending[j])))
                continue
            cur_pos = q[j] + delta[j]
            d = pending[j] - cur_pos
            if d == 0:
                pending[j] = np.nan
                continue
            side = np.sign(d)
            o, hi, lo = f["open"][i, j], f["high"][i, j], f["low"][i, j]
            lu, ld, vol = f["limit_up"][i, j], f["limit_down"][i, j], f["volume"][i, j]
            if reject is not None and reject[i, j]:
                tr.events.append((i, j, "rejected", float(d)))
                continue
            if not np.isfinite(o) or not np.isfinite(vol) or vol <= 0:
                tr.events.append((i, j, "no_liquidity", float(d)))
                continue
            if st.flags["block_locked_fill"] and _locked_against(side, hi, lo, lu, ld, tickv[j]):
                tr.events.append((i, j, "locked", float(d)))
                continue
            cap = math.floor(cfg.part_cap * vol)
            fr = fill_ratio[i, j] if fill_ratio is not None else 1.0
            fill = side * min(abs(d), cap, math.floor(abs(d) * fr) if fr < 1 else abs(d))
            if fill == 0:
                tr.events.append((i, j, "cap_zero", float(d)))
                continue
            p = _fill_price(o, side, tickv[j], cfg.slip_ticks, lu, ld)
            if delta[j] != 0 and np.isfinite(fillp[j]):
                fillp[j] = (fillp[j] * delta[j] + p * fill) / (delta[j] + fill) if (delta[j] + fill) != 0 else p
            else:
                fillp[j] = p
            delta[j] += fill
            fee += abs(fill) * p * panel.mult[i, j] * cfg.fee_bp * 1e-4
            tr.fills.append((i, j, panel.contract[i, j], float(fill), p, "trade"))
            new_pos = cur_pos + fill
            if new_pos == 0:
                entry[j] = np.nan
            elif np.sign(new_pos) != np.sign(cur_pos) or cur_pos == 0:
                entry[j] = p
            elif abs(new_pos) > abs(cur_pos):
                entry[j] = (entry[j] * abs(cur_pos) + p * abs(fill)) / abs(new_pos)
            if fill == d:
                pending[j] = np.nan
            else:
                tr.events.append((i, j, "partial", float(d), float(fill)))
                if st.flags["resubmit_full_after_partial"]:
                    pending[j] = pending[j] + fill   # 缺陷：把已成交部分再报一遍
        # 3) 收盘：按结算价盯市
        occ = 0.0
        for j in range(N):
            s = S[i, j]
            if held[j] is not None and held[j] != panel.contract[i, j] and panel.pf_contract[i, j] == held[j]:
                s = pf["settlement"][i, j]          # 换月未完成：仍按所持旧合约结算价盯市
            if not np.isfinite(s):
                s = last_settle[j] if np.isfinite(last_settle[j]) else (fillp[j] if np.isfinite(fillp[j]) else np.nan)
            m = panel.mult[i, j]
            if q[j] != 0 and np.isfinite(last_settle[j]) and np.isfinite(s):
                pnl += q[j] * (s - last_settle[j]) * m
            if delta[j] != 0 and np.isfinite(s):
                pnl += delta[j] * (s - fillp[j]) * m
            q[j] += delta[j]
            if q[j] == 0:
                entry[j] = np.nan
            last_settle[j] = s if np.isfinite(s) else last_settle[j]
            if q[j] != 0 and np.isfinite(s):
                occ += abs(q[j]) * s * m * (bm[j] * (mm[i, j] if st.flags["margin_uses_hike"] else 1.0) + cfg.margin_addon)
        eq += pnl - fee
        peak = max(peak, eq)
        tr.equity[i], tr.pnl[i], tr.fees[i], tr.margin[i] = eq, pnl, fee, occ
        tr.q[i] = q
        tr.held[i] = held
        tr.entry[i] = entry
        dly = int(exec_delay[i]) if isinstance(exec_delay, np.ndarray) else int(exec_delay)
        due = i + 1 + dly
        # 4) 收盘后风控
        if cooldown > 0:
            cooldown -= 1
        if st.dd_hit(eq, peak) and np.any(q != 0):
            tr.events.append((i, -1, "dd_stop", eq / peak - 1.0))
            for j in range(N):
                _queue(pending, pending_due, j, 0.0, due)
            cooldown = cfg.dd_cooldown
            peak = eq
        else:
            for j in range(N):
                px = S[i, j]
                if q[j] != 0 and st.stop_hit(np.sign(q[j]), entry[j], px):
                    tr.events.append((i, j, "stop", float(px / entry[j] - 1.0)))
                    _queue(pending, pending_due, j, 0.0, due)
                    blocked[j] = True
            sc = st.margin_scale(occ, eq)
            if sc < 1.0:
                tr.events.append((i, -1, "margin_scale", sc))
                for j in range(N):
                    base = pending[j] if np.isfinite(pending[j]) else q[j]
                    _queue(pending, pending_due, j, float(math.trunc(base * sc)), due)
        # 5) 调仓日
        if wend[i] and cooldown == 0 and i + 1 < T:
            sig = st.signal(ret, i)
            v = st.vol(ret, i)
            elig = np.isfinite(sig)
            for j in range(N):
                if elig[j] and st.is_stale(panel, i, j):
                    elig[j] = False
                    tr.events.append((i, j, "stale_skip"))
            w = st.weights(sig, v, elig)
            tgt = st.lots(w, eq, S[i], panel.mult[i])
            blocked[:] = False
            if st.flags["dd_peak_reset_on_rebalance"]:
                peak = eq   # 缺陷：每次调仓把回撤基准重置
            mmi = mm[i] if st.flags["margin_uses_hike"] else np.ones(N)
            sc = st.margin_scale(float(np.nansum(np.abs(tgt) * S[i] * panel.mult[i] * (bm * mmi + cfg.margin_addon))), eq)
            if sc < 1.0:
                tgt = np.trunc(tgt * sc)
            tr.target[i] = tgt
            for j in range(N):
                small = (tgt[j] != 0 and q[j] != 0 and np.sign(tgt[j]) == np.sign(q[j])
                         and abs(tgt[j] - q[j]) <= cfg.rebalance_band * abs(tgt[j]))
                if tgt[j] != q[j] and not small:
                    _queue(pending, pending_due, j, float(tgt[j]), due)
                    tr.orders.append((i, j, float(tgt[j] - q[j]), float(tgt[j]), float(w[j]), int(pending_due[j])))
    return tr


def summary(tr: Trace) -> dict:
    e = tr.equity[tr.i0:tr.i1]
    e = e[np.isfinite(e)]
    if len(e) < 2:
        return {}
    r = np.diff(e) / e[:-1]
    yrs = len(r) / 252
    dd = e / np.maximum.accumulate(e) - 1
    return {"ann_ret": float((e[-1] / e[0]) ** (1 / yrs) - 1) if yrs > 0 else np.nan,
            "ann_vol": float(r.std() * math.sqrt(252)), "sharpe": float(r.mean() / r.std() * math.sqrt(252)) if r.std() > 0 else np.nan,
            "max_dd": float(dd.min()), "fees_over_capital": float(tr.fees[tr.i0:tr.i1].sum() / e[0]),
            "n_fills": len(tr.fills), "n_events": len(tr.events), "days": len(e)}
