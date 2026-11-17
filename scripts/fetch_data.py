"""LSY-04 共用数据拉取：主力序列、合约信息、日线、5 分钟线。可断点续拉。

用法: python fetch_data.py [--products RB,I,...] [--start 20190901] [--end 20261116]
输出: lsy04-data/{dominant,detail,daily,min5}/*.parquet + fetch_log.jsonl
凭证只从 ~/.pandadata/pandadata.env 或环境变量读取，不落盘、不打印。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent))
if "--data-dir" in sys.argv:   # 在导入数据层之前设定目录
    import os as _os
    _os.environ["FUTURES_REPLAY_DATA"] = str(Path(sys.argv[sys.argv.index("--data-dir") + 1]).expanduser())
from futures_replay.data import DATA_ROOT as ROOT  # noqa: E402
PRODUCTS = {  # 品种: 交易所
    "RB": "SHF", "I": "DCE", "CU": "SHF", "AL": "SHF", "M": "DCE", "P": "DCE",
    "MA": "CZC", "TA": "CZC", "SA": "CZC", "AP": "CZC", "JD": "DCE",
}


def init_panda():
    import panda_data
    env = {}
    p = Path(os.path.expanduser("~/.pandadata/pandadata.env"))
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    user = os.getenv("PANDA_USERNAME") or env.get("DEFAULT_USERNAME")
    pwd = os.getenv("PANDA_PASSWORD") or env.get("DEFAULT_PASSWORD")
    base = os.getenv("PANDA_BASE_URL") or env.get("JAVA_SERVICE_BASE_URL")
    if not user or not pwd:
        raise RuntimeError("缺少 PandaData 凭证")
    panda_data.init_token(username=user, password=pwd, base_url=base)
    return panda_data


def log(rec: dict):
    rec["ts"] = datetime.now().isoformat(timespec="seconds")
    with open(ROOT / "fetch_log.jsonl", "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def call(fn, retries=4, **kw):
    for i in range(retries):
        try:
            return fn(**kw)
        except Exception as e:  # noqa: BLE001
            msg = str(e)[:200]
            log({"event": "error", "fn": getattr(fn, "__name__", "?"), "kw": kw, "msg": msg, "try": i})
            if "500009" in msg:  # 单日流量超限，不再重试
                raise
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"failed after retries: {kw}")


def save(df: pd.DataFrame, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--products", default=",".join(PRODUCTS))
    ap.add_argument("--start", default="20190901")
    ap.add_argument("--end", default="20261116")
    ap.add_argument("--freq", default="5m")
    ap.add_argument("--data-dir", default=None, help="数据目录（也可用环境变量 FUTURES_REPLAY_DATA）")
    a = ap.parse_args()
    pdx = init_panda()
    for prod in a.products.split(","):
        ex = PRODUCTS[prod]
        dpath = ROOT / "dominant" / f"{prod}.parquet"
        if dpath.exists():
            dom = pd.read_parquet(dpath)
        else:
            dom = call(pdx.get_future_dominant, underlying_symbol=prod, start_date=a.start, end_date=a.end)
            dom["date"] = dom["date"].astype(str)
            save(dom, dpath)
        contracts = sorted(dom["symbol"].unique())
        spans = dom.groupby("symbol")["date"].agg(["min", "max"])
        for c in contracts:
            det_p = ROOT / "detail" / f"{c}.parquet"
            if not det_p.exists():
                save(call(pdx.get_future_detail, symbol=c), det_p)
            det = pd.read_parquet(det_p)
            delist = str(det["de_listed_date"].iloc[0]).replace("-", "")
            s = (datetime.strptime(spans.loc[c, "min"], "%Y%m%d") - timedelta(days=60)).strftime("%Y%m%d")
            e = (datetime.strptime(spans.loc[c, "max"], "%Y%m%d") + timedelta(days=75)).strftime("%Y%m%d")
            e = min(e, delist if delist.isdigit() else e, a.end)
            s = max(s, "20190601")
            dp = ROOT / "daily" / f"{c}.parquet"
            if not dp.exists():
                save(call(pdx.get_future_daily, symbol=c, start_date=s, end_date=e), dp)
            mp = ROOT / f"min{a.freq.rstrip('m')}" / f"{c}.parquet"
            if not mp.exists():
                t = time.time()
                m = call(pdx.get_future_min, symbol=c, start_date=s, end_date=e, frequency=a.freq)
                save(m, mp)
                log({"event": "min", "symbol": c, "rows": len(m), "start": s, "end": e,
                     "sec": round(time.time() - t, 1),
                     "sha256": hashlib.sha256(mp.read_bytes()).hexdigest()[:16]})
        log({"event": "product_done", "product": prod, "contracts": len(contracts)})
        print(prod, "done", len(contracts), flush=True)


if __name__ == "__main__":
    sys.exit(main())
