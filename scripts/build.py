"""skill-futures-capacity-boundary 标准入口。

python scripts/build.py --out report_out                 # 默认：11 个品种、2020-01-02 至 2026-11-13、10 个资金档
python scripts/build.py --products RB,I,M,MA,SA --capitals 1e7,1e8,1e9 --out out_small
python scripts/build.py --check                          # 只校验输入与数据，不计算

程序化调用：
    from build import run
    res = run({"products": ["RB", "I"], "capitals": [1e7, 1e8]}, config={"out": "out"})
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

DEFAULT = {"products": ["RB", "I", "CU", "AL", "M", "P", "MA", "TA", "SA", "AP", "JD"],
           "full": ["20200102", "20261113"], "dev": ["20200102", "20231229"], "holdout": ["20240102", "20261113"],
           "capitals": [5e6, 1e7, 2e7, 5e7, 1e8, 2e8, 5e8, 1e9, 2e9, 5e9], "strategy": "S0"}
ASSUME_FIELDS = {"impact_y", "rho_max", "window_min", "adverse_share", "lock_days", "oi_share_max", "fee_bp",
                 "half_spread_tick", "margin_hike", "stress_vol_q", "floor_err", "cost_share_max", "cost_cap_frac"}


class InputError(ValueError):
    pass


def validate_input(input_data: dict | None) -> dict:
    """缺字段、空数据、类型错误时抛出明确异常；返回补齐默认值后的输入。"""
    from futures_replay.data import DATA_ROOT
    d = dict(DEFAULT)
    d.update(input_data or {})
    if d["strategy"] != "S0":
        raise InputError("目前只支持内置的冻结策略 S0（公开信号）。自定义订单输入尚未实现。")
    if not isinstance(d["products"], (list, tuple)) or not d["products"]:
        raise InputError("products 必须是非空列表")
    if len(d["products"]) < 5:
        raise InputError("S0 是截面策略，至少需要 5 个品种")
    caps = d["capitals"]
    if not caps or any((not isinstance(c, (int, float))) or c <= 0 for c in caps):
        raise InputError("capitals 必须是正数列表")
    d["capitals"] = sorted(float(c) for c in caps)
    for k in ("full", "dev", "holdout"):
        w = d[k]
        if len(w) != 2 or not all(isinstance(x, str) and len(x) == 8 and x.isdigit() for x in w) or w[0] >= w[1]:
            raise InputError(f"{k} 窗口格式应为 [YYYYMMDD, YYYYMMDD] 且起点早于终点")
    if d["dev"][1] >= d["holdout"][0]:
        raise InputError("开发期必须早于保留期，且两段不重叠")
    missing = [p for p in d["products"] if not (DATA_ROOT / "dominant" / f"{p}.parquet").exists()]
    if missing:
        raise InputError(f"缺少数据：{missing}。先运行 python scripts/fetch_data.py --products {','.join(missing)}（数据目录 {DATA_ROOT}）")
    # 5 分钟线缺失时日内参与率会被静默算空，这里提前拦下
    no_min = [p for p in d["products"]
              if not any(len(f.stem) > len(p) and f.stem[len(p)].isdigit() for f in (DATA_ROOT / "min5").glob(f"{p}*.parquet"))]
    if no_min:
        raise InputError(f"缺少 5 分钟线：{no_min}（数据目录 {DATA_ROOT}/min5）。用评审快照时先运行 python scripts/verify_snapshot.py <快照目录>")
    return d


def run(input_data: dict | None = None, config: dict | None = None) -> dict:
    """返回 {"out_dir", "plain_text", "caps", "first_fail", ...}。config 可含 out 与 assumptions 覆盖。"""
    import run_q7
    import report
    from capacity import ASSUMPTIONS
    d = validate_input(input_data)
    config = config or {}
    assumptions = dict(ASSUMPTIONS)
    for name, over in (config.get("assumptions") or {}).items():
        if name not in assumptions:
            raise InputError(f"未知假设集 {name}")
        bad = set(over) - ASSUME_FIELDS
        if bad:
            raise InputError(f"未知假设字段 {bad}")
        assumptions[name] = replace(assumptions[name], **over)
    out = Path(config.get("out", "q7_out")).resolve()
    run_q7.main(products=d["products"], full_w=tuple(d["full"]), dev_w=tuple(d["dev"]), hold_w=tuple(d["holdout"]),
                capitals=d["capitals"], out=out, assumptions=assumptions)
    report.main(out)
    res = json.loads((out / "report.json").read_text())
    res["out_dir"] = str(out)
    res["files"] = sorted(p.name for p in out.iterdir() if p.is_file())
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="期货策略放大资金后在哪里失效：容量边界")
    ap.add_argument("--products", default=",".join(DEFAULT["products"]))
    ap.add_argument("--capitals", default=",".join(f"{c:g}" for c in DEFAULT["capitals"]))
    ap.add_argument("--full", default=",".join(DEFAULT["full"]))
    ap.add_argument("--dev", default=",".join(DEFAULT["dev"]))
    ap.add_argument("--holdout", default=",".join(DEFAULT["holdout"]))
    ap.add_argument("--out", default="q7_out")
    ap.add_argument("--assumptions", default=None, help='JSON，如 {"central":{"impact_y":0.8}}')
    ap.add_argument("--check", action="store_true", help="只校验输入与数据")
    ap.add_argument("--data-dir", default=None, help="数据目录（也可用环境变量 FUTURES_REPLAY_DATA）")
    a = ap.parse_args(argv)
    if a.data_dir:
        import os
        os.environ["FUTURES_REPLAY_DATA"] = str(Path(a.data_dir).expanduser())
        import importlib
        import futures_replay.data as _fd
        importlib.reload(_fd)
    inp = {"products": a.products.split(","), "capitals": [float(x) for x in a.capitals.split(",")],
           "full": a.full.split(","), "dev": a.dev.split(","), "holdout": a.holdout.split(",")}
    try:
        if a.check:
            d = validate_input(inp)
            print(json.dumps({"ok": True, "input": d}, ensure_ascii=False, default=str))
            return 0
        res = run(inp, {"out": a.out, "assumptions": json.loads(a.assumptions) if a.assumptions else None})
    except InputError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **{k: res[k] for k in ("out_dir", "plain_text", "caps", "first_fail")}}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
