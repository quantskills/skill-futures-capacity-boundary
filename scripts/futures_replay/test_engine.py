"""回放账本手算校验（离线，不需要 PandaData）。python -m futures_replay.test_engine"""
from __future__ import annotations

import numpy as np

from .data import Panel, fix_double_sided
from .sim import Config, StrategyS0, simulate, chain_returns

import pandas as pd


def tiny_panel(T=12, roll_at=None, lock_day=None):
    """单品种 X，乘数 10，tick 1，涨跌停 ±5%。价格每天 +1%（结算价 = 收盘价）。"""
    dates = np.array([f"202401{d:02d}" for d in range(2, 2 + T)])
    px = 1000 * 1.01 ** np.arange(T)
    f = {c: px.reshape(-1, 1).copy() for c in ["open", "high", "low", "close", "settlement"]}
    f["open"] = (px / 1.005).reshape(-1, 1)
    f["pre_settlement"] = np.concatenate([[px[0] / 1.01], px[:-1]]).reshape(-1, 1)
    f["limit_up"] = f["pre_settlement"] * 1.05
    f["limit_down"] = f["pre_settlement"] * 0.95
    f["volume"] = np.full((T, 1), 1e5)
    f["open_interest"] = np.full((T, 1), 1e5)
    f["amount"] = np.full((T, 1), 1e9)
    f["vol_fixed"] = np.zeros((T, 1), dtype=bool)
    contract = np.array([["X2401.TST"]] * T, dtype=object)
    pf = {c: np.full((T, 1), np.nan) for c in ["open", "settlement", "pre_settlement", "close", "volume", "limit_up", "limit_down", "high", "low"]}
    pf_contract = np.full((T, 1), None, dtype=object)
    if roll_at is not None:
        contract[roll_at:, 0] = "X2405.TST"
        for c in pf:
            pf[c][roll_at:roll_at + 3, 0] = f[c][roll_at:roll_at + 3, 0] * (0.98 if c not in ("volume",) else 1.0)
        pf_contract[roll_at:roll_at + 3, 0] = "X2401.TST"
    if lock_day is not None:
        ld = f["limit_down"][lock_day, 0]
        for c in ["open", "high", "low", "close", "settlement"]:
            f[c][lock_day, 0] = ld
    prev = np.vstack([contract[:1], contract[:-1]])
    return Panel(dates, ["X"], {"X": "TST"}, contract, prev, f, pf, pf_contract, np.full((T, 1), 10.0),
                 {"X": 1.0}, {"X": 0.1}, {"X": 0.05})


class Fixed(StrategyS0):
    """第 1 天收盘给目标 +5 手，第 7 天收盘给目标 -3 手。"""
    def __init__(self, cfg, plan):
        super().__init__(cfg)
        self.plan = plan


def run_plan(panel, plan, cfg):
    """绕开周频信号：在 plan={i: lots} 指定日收盘下目标。"""
    st = StrategyS0(cfg)
    T = panel.T
    import futures_replay.sim as S
    orig = S.week_end_flags
    S.week_end_flags = lambda d: np.array([i in plan for i in range(T)])
    st.signal = lambda ret, i: np.array([1.0])
    st.weights = lambda sig, vol, el: np.array([1.0])
    st.is_stale = lambda panel, i, j: False
    st.lots = lambda w, eq, s, m, _p=plan: np.array([float(_p.get(st._i, 0.0))])
    _orig_signal = st.signal

    def sig(ret, i):
        st._i = i
        return np.array([1.0])
    st.signal = sig
    try:
        tr = simulate(panel, st, cfg)
    finally:
        S.week_end_flags = orig
    return tr


def test_pnl_fees_mult():
    p = tiny_panel()
    cfg = Config(capital=1e6, fee_bp=1.0, slip_ticks=1.0, stop_pct=0.5, dd_stop=0.9, margin_cap=1.0, min_products=1)
    tr = run_plan(p, {1: 5}, cfg)
    # 第 2 天开盘买 5 手：成交价 = 开盘 + 1 tick
    fill = p.f["open"][2, 0] + 1.0
    S = p.f["settlement"][:, 0]
    exp_day2 = 5 * (S[2] - fill) * 10
    fee2 = 5 * fill * 10 * 1e-4
    assert abs(tr.pnl[2] - exp_day2) < 1e-6, (tr.pnl[2], exp_day2)
    assert abs(tr.fees[2] - fee2) < 1e-9
    exp_day3 = 5 * (S[3] - S[2]) * 10
    assert abs(tr.pnl[3] - exp_day3) < 1e-6
    # 资金守恒：权益变化 = 当日盈亏 - 费用
    e = tr.equity
    for i in range(1, p.T):
        assert abs((e[i] - e[i - 1]) - (tr.pnl[i] - tr.fees[i])) < 1e-6
    return "pnl/fees/multiplier/conservation ok"


def test_locked_no_fill():
    p = tiny_panel(lock_day=8)
    cfg = Config(capital=1e6, fee_bp=0.0, slip_ticks=0.0, stop_pct=0.5, dd_stop=0.9, margin_cap=1.0, min_products=1)
    tr = run_plan(p, {1: 5, 7: 0}, cfg)
    # 第 8 天跌停锁板（最高=最低=跌停）：平多单不能成交，顺延到第 9 天
    assert tr.q[8, 0] == 5 and tr.q[9, 0] == 0, (tr.q[7:10, 0])
    assert any(e[2] == "locked" and e[0] == 8 for e in tr.events)
    return "locked bar blocks the exit, fills next day ok"


def test_roll():
    p = tiny_panel(roll_at=5)
    cfg = Config(capital=1e6, fee_bp=0.0, slip_ticks=0.0, stop_pct=0.5, dd_stop=0.9, margin_cap=1.0, min_products=1)
    tr = run_plan(p, {1: 5}, cfg)
    kinds = [f[5] for f in tr.fills]
    assert "roll_out" in kinds and "roll_in" in kinds
    ro = [f for f in tr.fills if f[5] == "roll_out"][0]
    assert ro[0] == 5 and ro[2] == "X2401.TST" and abs(ro[4] - p.pf["open"][5, 0]) < 1e-9
    # 换月日盈亏 = 旧腿 (开盘平仓价 - 昨结) + 新腿 (结算 - 开盘开仓价)
    S = p.f["settlement"][:, 0]
    exp = 5 * (p.pf["open"][5, 0] - S[4]) * 10 + 5 * (S[5] - p.f["open"][5, 0]) * 10
    assert abs(tr.pnl[5] - exp) < 1e-6, (tr.pnl[5], exp)
    assert tr.held[5, 0] == "X2405.TST"
    return "roll legs priced and marked correctly ok"


def test_double_sided_fix():
    df = pd.DataFrame({"date": ["20191231", "20200102"], "volume": [200.0, 100.0], "open_interest": [20.0, 10.0], "amount": [2e6, 1e6]})
    out = fix_double_sided(df, "SHF")
    assert list(out.volume) == [100.0, 100.0] and list(out.vol_fixed) == [True, False]
    assert list(fix_double_sided(df, "CFE").volume) == [200.0, 100.0]
    return "pre-2020 double-sided volume halved for SHF/DCE/CZC only ok"


def test_chain_returns():
    p = tiny_panel(roll_at=5)
    r = chain_returns(p)
    assert abs(r[3, 0] - 0.01) < 1e-9
    assert abs(r[5, 0] - (p.pf["close"][5, 0] / p.f["close"][4, 0] - 1)) < 1e-12
    return "chain returns use the held contract on roll days ok"


if __name__ == "__main__":
    for t in [test_pnl_fees_mult, test_locked_no_fill, test_roll, test_double_sided_fix, test_chain_returns]:
        print(t.__name__, "->", t())
    print("ALL ENGINE TESTS PASSED")
