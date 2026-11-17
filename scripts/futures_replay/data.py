"""数据层：把 PandaData 落盘的具体合约数据整理成按交易日对齐的主力链面板。

口径（全部实测过，见 references/api_guide.md）：
- 主力标签 D 日由 D-1 收盘持仓量交叉决定（RB 前 5 次换月均在交叉后次日切换），D 日开盘按标签换月不含未来信息。
- SHF/DCE/CZC 在 2020-01-01 前成交量、持仓量为双边口径（同合约持仓隔夜减半，CFE 不变），此前数据减半并打标。
- 已到期合约的 detail.margin_rate 是交割月提高后的最终值，不能当历史一般月份保证金率；改用品种当前在交易合约的值。
- 分钟线 volume 是当根成交量（文档写成"当日"），K 线按结束时间标记，trading_date 为归属交易日。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

def _data_root() -> Path:
    """数据目录：环境变量 FUTURES_REPLAY_DATA 优先；否则依次找仓库根目录 data/、lsy04-data/、~/.futures_replay/data。"""
    env = os.getenv("FUTURES_REPLAY_DATA")
    if env:
        return Path(env).expanduser().resolve()
    root = Path(__file__).resolve().parents[2]
    for cand in (root / "data", root / "lsy04-data", Path.home() / ".futures_replay" / "data"):
        if cand.exists():
            return cand
    return root / "data"


DATA_ROOT = _data_root()
DOUBLE_SIDED_EXCH = {"SHF", "DCE", "CZC"}
SINGLE_SIDED_SINCE = "20200101"
ROLL_GRACE = 20   # 换月后旧合约价格保留的交易日数，用于换月失败后的重试
PRICE_COLS = ["open", "high", "low", "close", "settlement", "pre_settlement", "limit_up", "limit_down"]
VOL_COLS = ["volume", "open_interest", "amount"]


def fix_double_sided(df: pd.DataFrame, exchange: str, date_col: str = "date") -> pd.DataFrame:
    """2020 年前双边口径减半；返回新表并加 vol_fixed 标记列。"""
    out = df.copy()
    out["vol_fixed"] = False
    if exchange in DOUBLE_SIDED_EXCH:
        mask = out[date_col].astype(str).str.replace("-", "").str[:8] < SINGLE_SIDED_SINCE
        for c in VOL_COLS:
            if c in out:
                out.loc[mask, c] = out.loc[mask, c] / 2.0
        out.loc[mask, "vol_fixed"] = True
    return out


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


@dataclass
class Panel:
    """T×N 日度主力链面板。行 = 交易日，列 = 品种。"""
    dates: np.ndarray                 # (T,) str YYYYMMDD
    products: list[str]
    exchange: dict[str, str]
    contract: np.ndarray              # (T,N) object, D 日主力（D 日开盘后持有的合约）
    prev_contract: np.ndarray         # (T,N) object, D-1 日主力
    f: dict[str, np.ndarray]          # 主力合约字段 (T,N)
    pf: dict[str, np.ndarray]         # 换月后 ROLL_GRACE 日内旧合约字段 (T,N)，其余 = NaN
    pf_contract: np.ndarray           # (T,N) pf 字段对应的旧合约
    mult: np.ndarray                  # (T,N)
    tick: dict[str, float]
    base_margin: dict[str, float]     # 品种一般月份保证金率（当前在交易合约）
    limit_pct: dict[str, float]       # 品种常规涨跌停幅度（中位数估计）
    fingerprints: dict[str, str] = field(default_factory=dict)

    @property
    def T(self):
        return len(self.dates)

    @property
    def N(self):
        return len(self.products)

    def copy(self) -> "Panel":
        return Panel(self.dates.copy(), list(self.products), dict(self.exchange), self.contract.copy(),
                     self.prev_contract.copy(), {k: v.copy() for k, v in self.f.items()},
                     {k: v.copy() for k, v in self.pf.items()}, self.pf_contract.copy(), self.mult.copy(), dict(self.tick),
                     dict(self.base_margin), dict(self.limit_pct), dict(self.fingerprints))

    def idx(self, date: str) -> int:
        return int(np.searchsorted(self.dates, date))


def _read_daily(contract: str) -> pd.DataFrame | None:
    p = DATA_ROOT / "daily" / f"{contract}.parquet"
    if not p.exists():
        return None
    d = pd.read_parquet(p)
    d["date"] = d["date"].astype(str)
    return d.drop_duplicates("date").set_index("date").sort_index()


def _read_detail(contract: str) -> dict:
    p = DATA_ROOT / "detail" / f"{contract}.parquet"
    return pd.read_parquet(p).iloc[0].to_dict() if p.exists() else {}


# 最小变动价位（交易所合约规格，2026-11 核对口径；用于滑点 tick 换算）
TICK = {"RB": 1, "I": 0.5, "CU": 10, "AL": 5, "M": 1, "P": 2, "MA": 1, "TA": 2, "SA": 1, "AP": 1, "JD": 1}


def build_panel(products: list[str], start: str = "20191001", end: str = "20261116",
                cache: bool = True) -> Panel:
    cache_p = DATA_ROOT / f"panel_v2_{'_'.join(products)}_{start}_{end}.pkl"
    if cache and cache_p.exists():
        return pd.read_pickle(cache_p)
    doms, exch, fps = {}, {}, {}
    for prod in products:
        p = DATA_ROOT / "dominant" / f"{prod}.parquet"
        d = pd.read_parquet(p)
        d["date"] = d["date"].astype(str)
        d = d[(d.date >= start) & (d.date <= end)].drop_duplicates("date").set_index("date").sort_index()
        doms[prod] = d["symbol"]
        exch[prod] = d["symbol"].iloc[-1].split(".")[-1]
        fps[f"dominant/{prod}"] = file_sha(p)
    dates = np.array(sorted(set().union(*[set(s.index) for s in doms.values()])))
    T, N = len(dates), len(products)
    fields = PRICE_COLS + VOL_COLS
    f = {c: np.full((T, N), np.nan) for c in fields}
    pf = {c: np.full((T, N), np.nan) for c in ["open", "settlement", "pre_settlement", "close", "volume", "limit_up", "limit_down", "high", "low"]}
    f["vol_fixed"] = np.zeros((T, N), dtype=bool)
    contract = np.full((T, N), None, dtype=object)
    prev_contract = np.full((T, N), None, dtype=object)
    pf_contract = np.full((T, N), None, dtype=object)
    mult = np.full((T, N), np.nan)
    base_margin, limit_pct = {}, {}
    date_pos = {d: i for i, d in enumerate(dates)}
    for j, prod in enumerate(products):
        s = doms[prod]
        cache_daily = {}
        for c in s.unique():
            dd = _read_daily(c)
            if dd is not None:
                cache_daily[c] = fix_double_sided(dd.reset_index(), exch[prod]).set_index("date")
                fps[f"daily/{c}"] = file_sha(DATA_ROOT / "daily" / f"{c}.parquet")
            det = _read_detail(c)
            m = det.get("contract_multiplier", np.nan)
            for dt in s.index[s == c]:
                mult[date_pos[dt], j] = m
        last = s.iloc[-1]
        base_margin[prod] = float(_read_detail(last).get("margin_rate", np.nan))
        prev = None
        old, old_left = None, 0
        for dt, c in s.items():
            i = date_pos[dt]
            contract[i, j] = c
            prev_contract[i, j] = prev if prev is not None else c
            if prev is not None and prev != c:
                old, old_left = prev, ROLL_GRACE
            if old is not None and old_left > 0 and old != c:
                po = cache_daily.get(old)
                if po is not None and dt in po.index:
                    r = po.loc[dt]
                    for col in pf:
                        pf[col][i, j] = r[col]
                    pf_contract[i, j] = old
                old_left -= 1
            dd = cache_daily.get(c)
            if dd is not None and dt in dd.index:
                row = dd.loc[dt]
                for col in fields:
                    if col in row:
                        f[col][i, j] = row[col]
                f["vol_fixed"][i, j] = bool(row.get("vol_fixed", False))
            prev = c
        lu, ps = f["limit_up"][:, j], f["pre_settlement"][:, j]
        ratio = (lu / ps - 1.0)
        limit_pct[prod] = float(np.nanmedian(ratio[np.isfinite(ratio)])) if np.isfinite(ratio).any() else 0.05
    panel = Panel(dates, products, exch, contract, prev_contract, f, pf, pf_contract, mult, {p: float(TICK.get(p, 1)) for p in products},
                  base_margin, limit_pct, fps)
    if cache:
        pd.to_pickle(panel, cache_p)
    return panel


def load_min(contract: str, freq: str = "5") -> pd.DataFrame:
    p = DATA_ROOT / f"min{freq}" / f"{contract}.parquet"
    if not p.exists():
        return pd.DataFrame()
    m = pd.read_parquet(p)
    exch = contract.split(".")[-1]
    m["trading_date"] = m["trading_date"].astype(str)
    m = fix_double_sided(m, exch, date_col="trading_date")
    m["datetime"] = pd.to_datetime(m["datetime"])
    return m.sort_values("datetime").reset_index(drop=True)


def manifest_of(panel: Panel) -> dict:
    return {"products": panel.products, "start": panel.dates[0], "end": panel.dates[-1], "T": panel.T,
            "base_margin": panel.base_margin, "limit_pct": panel.limit_pct, "tick": panel.tick,
            "input_sha256_16": panel.fingerprints,
            "input_digest": hashlib.sha256(json.dumps(panel.fingerprints, sort_keys=True).encode()).hexdigest()[:16]}


def save_panel(panel: Panel, path) -> Path:
    """可移植保存：数值数组 → npz，合约代码等 → json。不依赖 pickle，跨 Python/pandas 版本可读。"""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    arrs = {f"f__{k}": v for k, v in panel.f.items()}
    arrs.update({f"pf__{k}": v for k, v in panel.pf.items()})
    arrs["mult"] = panel.mult
    np.savez_compressed(path / "panel_arrays.npz", **arrs)
    meta = {"dates": panel.dates.tolist(), "products": panel.products, "exchange": panel.exchange,
            "contract": panel.contract.tolist(), "prev_contract": panel.prev_contract.tolist(),
            "pf_contract": panel.pf_contract.tolist(), "tick": panel.tick, "base_margin": panel.base_margin,
            "limit_pct": panel.limit_pct, "fingerprints": panel.fingerprints}
    (path / "panel_meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    return path


def load_panel(path) -> Panel:
    path = Path(path)
    z = np.load(path / "panel_arrays.npz", allow_pickle=False)
    m = json.loads((path / "panel_meta.json").read_text())
    f = {k[3:]: z[k] for k in z.files if k.startswith("f__")}
    pf = {k[4:]: z[k] for k in z.files if k.startswith("pf__")}
    obj = lambda x: np.array(x, dtype=object)
    return Panel(np.array(m["dates"]), m["products"], m["exchange"], obj(m["contract"]), obj(m["prev_contract"]), f, pf,
                 obj(m["pf_contract"]), z["mult"], m["tick"], m["base_margin"], m["limit_pct"], m["fingerprints"])
