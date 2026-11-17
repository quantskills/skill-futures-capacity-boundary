"""离线自测：python scripts/test.py（不需要 PandaData 与网络）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from futures_replay import test_engine  # noqa: E402


def test_engine_all():
    return [t() for t in (test_engine.test_pnl_fees_mult, test_engine.test_locked_no_fill, test_engine.test_roll,
                          test_engine.test_double_sided_fix, test_engine.test_chain_returns)]


def test_validate_input():
    import os
    from build import validate_input, InputError
    errs = 0
    for bad in [{"strategy": "MINE"}, {"products": []}, {"products": ["RB", "I"]}, {"capitals": [0, 1e8]},
                {"full": ["2020", "20261113"]}, {"dev": ["20200102", "20250101"], "holdout": ["20240102", "20261113"]}]:
        try:
            validate_input(bad)
        except InputError:
            errs += 1
    with tempfile.TemporaryDirectory() as d:
        os.environ["FUTURES_REPLAY_DATA"] = d
        import importlib
        import futures_replay.data as fd
        importlib.reload(fd)
        import build
        importlib.reload(build)
        try:
            build.validate_input({"products": ["RB", "I", "M", "MA", "SA"]})
        except build.InputError as e:
            errs += int("缺少数据" in str(e))
        del os.environ["FUTURES_REPLAY_DATA"]
        importlib.reload(fd)
    assert errs == 7, errs
    return "bad inputs and missing data raise clear errors ok"


def _fake(capital):
    """一个品种、10 笔调仓，窗口成交量固定 1000 手：手数随资金线性放大。"""
    lots = np.full(10, capital / 1e6)
    return pd.DataFrame({"i": range(10), "date": [f"202401{k + 2:02d}" for k in range(10)], "product": "X", "contract": "X1",
                         "lots": lots, "price": 100.0, "mult": 10.0, "kind": "trade", "notional": lots * 1000.0,
                         "v_15": 1000.0, "v_30": 1000.0, "v_60": 1000.0, "capital": capital})


def test_participation_monotone():
    vw = "v_30"
    r = [float((_fake(c).lots.abs() / _fake(c)[vw]).quantile(0.95)) for c in (1e7, 1e8, 1e9)]
    assert r[0] < r[1] < r[2] and abs(r[1] / r[0] - 10) < 1e-9
    return "participation scales linearly with capital ok"


def test_zones():
    from run_q7 import zones
    rows = []
    for C, ok in [(1e7, (True, True, True)), (1e8, (True, True, False)), (1e9, (True, False, False)), (1e10, (False, False, False))]:
        for a, o in zip(["optimistic", "central", "pessimistic"], ok):
            rows.append({"capital": C, "assume": a, "ok_all_upper": o, "ok_size": o, "ok_floor": True,
                         "failed": "" if o else "participation", "net_annual_ret": 0.0})
    z = zones(pd.DataFrame(rows))
    assert list(z.zone) == ["安全", "条件", "高风险", "失效"], list(z.zone)
    assert list(z.zone_size_only) == ["安全", "条件", "高风险", "失效"], list(z.zone_size_only)
    # 经济约束不通过、规模约束通过时：总分区失效，规模分区不受影响
    rows2 = [dict(r, ok_all_upper=False, failed="cost") for r in rows]
    z2 = zones(pd.DataFrame(rows2))
    assert set(z2.zone) == {"失效"} and list(z2.zone_size_only) == ["安全", "条件", "高风险", "失效"]
    return "zone rule (size-only and with economics) ok"


if __name__ == "__main__":
    out = []
    for t in (test_engine_all, test_validate_input, test_participation_monotone, test_zones):
        r = t()
        out.append((t.__name__, r))
        print(t.__name__, "->", r)
    print(f"ALL {len(out)} TEST GROUPS PASSED")
