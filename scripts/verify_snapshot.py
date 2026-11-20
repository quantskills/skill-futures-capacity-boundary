"""安装并核对评审用的冻结行情快照：python scripts/verify_snapshot.py <快照 zip 或所在目录>...

快照（LSY04-Q7_frozen_data_snapshot_part1of4.zip … part4of4.zip）随量枢院成果附件提供（行情数据不随公开仓库分发）。本脚本：
1. 把各 zip 里 data/ 下的文件解压到仓库根目录 data/（scripts/futures_replay/data.py 默认先找这里；拒绝越界路径）；
2. 逐文件核对 sha256 与仓库里提交的清单 references/data_snapshot_manifest.json，确认没有缺文件；
3. 核对 dominant 与 daily 的指纹和冻结运行 examples/sample_out/meta.json 的 input_sha256_16 / input_digest 一致，
   min5 的指纹与拉取日志 fetch_log.jsonl 一致；
4. 核对 11 个品种齐全：每个品种都有主力序列、日线、合约信息和 5 分钟线。
通过后运行 python scripts/build.py --out q7_out，生成容量表与换月曲线。
"""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"
MANIFEST = REPO / "references" / "data_snapshot_manifest.json"
FROZEN_META = REPO / "examples" / "sample_out" / "meta.json"


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def extract(zpath: Path) -> int:
    n = 0
    with zipfile.ZipFile(zpath) as z:
        for m in z.namelist():
            if not m.startswith("data/") or m.endswith("/"):
                continue
            dest = (REPO / m).resolve()
            if not str(dest).startswith(str(DATA.resolve())):
                raise SystemExit(f"拒绝越界路径：{m}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(m))
            n += 1
    return n


def main(args: list[str]) -> int:
    zips = []
    for a in args:
        p = Path(a).expanduser()
        zips += sorted(p.glob("LSY04-Q7_frozen_data_snapshot_part*.zip")) if p.is_dir() else [p]
    if not zips:
        raise SystemExit("没有找到快照 zip")
    for z in zips:
        print(f"解压 {z.name}：{extract(z)} 个文件")
    man = json.loads(MANIFEST.read_text())
    bad = []
    for arc, info in man["files"].items():
        p = REPO / arc
        if not p.exists():
            bad.append(f"缺少 {arc}")
        elif sha(p) != info["sha256"]:
            bad.append(f"sha256 不一致 {arc}")
    frozen = json.loads(FROZEN_META.read_text())["data"]
    fp = {}
    for key in frozen["input_sha256_16"]:
        p = DATA / f"{key}.parquet"
        fp[key] = sha(p)[:16] if p.exists() else None
    mism = [k for k, v in frozen["input_sha256_16"].items() if fp.get(k) != v]
    if mism:
        bad.append(f"{len(mism)} 个 dominant/daily 文件与冻结指纹不一致，例如 {mism[:3]}")
    digest = hashlib.sha256(json.dumps(fp, sort_keys=True).encode()).hexdigest()[:16]
    if digest != frozen["input_digest"]:
        bad.append(f"输入总摘要 {digest} ≠ 冻结 {frozen['input_digest']}")
    logged = {}
    for line in (DATA / "fetch_log.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r.get("event") == "min" and r.get("sha256"):
            logged[r["symbol"]] = r["sha256"]
    min_files = sorted((DATA / "min5").glob("*.parquet"))
    min_bad = [f.stem for f in min_files if f.stem in logged and sha(f)[:16] != logged[f.stem]]
    if min_bad:
        bad.append(f"{len(min_bad)} 个 5 分钟线文件与拉取日志指纹不一致，例如 {min_bad[:3]}")
    for prod in man["products"]:
        if not (DATA / "dominant" / f"{prod}.parquet").exists():
            bad.append(f"{prod} 缺主力序列")
        for sub in ("daily", "detail", "min5"):
            if not any(f.name.startswith(prod) and f.name[len(prod)].isdigit() for f in (DATA / sub).glob("*.parquet")):
                bad.append(f"{prod} 缺 {sub}")
    if bad:
        print("核对失败：\n  " + "\n  ".join(bad))
        return 1
    print(f"核对通过：{len(man['files'])} 个文件 sha256 与清单一致；dominant/daily 指纹与冻结运行一致（input_digest {digest}）；"
          f"{len(min_files)} 个 5 分钟线文件中 {sum(1 for f in min_files if f.stem in logged)} 个与拉取日志指纹一致；"
          f"11 个品种 {','.join(man['products'])} 齐全。")
    print("下一步：python scripts/build.py --out q7_out")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    sys.exit(main(sys.argv[1:]))
