"""Q7 报告：report.md / report.html（单文件，Chart.js 走 cdnjs，离线时文字完整）/ report.json / capacity.parquet。"""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent / "out"

GLOSSARY = [
    ("参与率 participation", "你的单子占同一时间段市场成交量的比例。超过 10% 左右，冲击成本模型就不太可信，单子也可能做不完。"),
    ("平方根冲击 square-root impact", "冲击成本大致随交易量占比的平方根增长：量翻 4 倍，冲击约翻 2 倍。系数无法用公开数据校准，这里只给网格。"),
    ("保证金跑道 margin runway", "极端行情里，账户权益还能撑几天不被追保（T_run）。"),
    ("平仓时间 time to liquidate", "按参与率上限和锁板天数，把仓位平完要几天（T_liq）。T_liq 大于 T_run 就是资金放大后的失效。"),
    ("移仓压力 roll pressure", "换月时旧合约的成交量会断崖下降，越晚移仓旧腿越难平。"),
    ("限仓代理 OI share", "单合约持仓占全市场持仓量的比例；交易所限仓细则未逐品种核对，用比例上限代替。"),
    ("资金下限 capital floor", "期货一手就是一大块，资金太小时按整手取整，实际风险会偏离目标。"),
    ("保留集 holdout", "2024 年以后的数据在定好假设之前不看，用来检验结论稳不稳。"),
]


def md_table(df: pd.DataFrame, index: bool = False, floatfmt: str = ".4g") -> str:
    """不依赖 tabulate 的 Markdown 表格。"""
    d = df.reset_index() if index else df
    def f(x):
        if isinstance(x, (float, np.floating)):
            return "—" if not np.isfinite(x) else format(x, floatfmt)
        return str(x)
    head = "| " + " | ".join(map(str, d.columns)) + " |"
    sep = "|" + "---|" * len(d.columns)
    rows = ["| " + " | ".join(f(v) for v in r) + " |" for r in d.itertuples(index=False)]
    return "\n".join([head, sep] + rows)


def fmt_cap(x):
    if not np.isfinite(x) or x <= 0:
        return "—"
    return f"{x / 1e8:.2f} 亿" if x >= 1e8 else f"{x / 1e4:.0f} 万"


def build_summary(OUTD=OUT) -> dict:
    meta = json.loads((OUTD / "meta.json").read_text())
    table = pd.read_csv(OUTD / "capital_table.csv")
    zones = pd.read_csv(OUTD / "zones.csv")
    binding = pd.read_csv(OUTD / "product_binding.csv")
    torn = pd.read_csv(OUTD / "tornado.csv")
    vfe = pd.read_csv(OUTD / "volume_forecast_error.csv")
    stress = pd.read_csv(OUTD / "stress_vs_normal_rho_p95.csv")
    delay = pd.read_csv(OUTD / "delay_cost_sample.csv") if (OUTD / "delay_cost_sample.csv").stat().st_size > 5 else pd.DataFrame()
    rd = pd.read_csv(OUTD / "roll_decay.csv") if (OUTD / "roll_decay.csv").exists() else pd.DataFrame()
    caps = meta["max_ok_capital"]
    full = table[table.window == "full"]
    c = full[full.assume == "central"].sort_values("capital")
    first_fail = c[~c.ok_all_upper].head(1)
    first_fail_c = float(first_fail.capital.iloc[0]) if len(first_fail) else np.nan
    first_fail_what = str(first_fail.failed.iloc[0]) if len(first_fail) else ""
    floor = full[(full.assume == "central") & (~full.ok_floor)].capital.max()
    gross_edge = bool(c.gross_edge.iloc[0]) if "gross_edge" in c else True
    base = meta.get("baseline", {}).get("result", {})
    caps_size = meta.get("max_ok_capital_size_only", {})
    cs = c[~c.ok_size] if "ok_size" in c else c.iloc[0:0]
    size_fail_c = float(cs.capital.iloc[0]) if len(cs) else np.nan
    size_fail_what = ",".join(x for x in str(cs.failed.iloc[0]).split(",") if x and x != "cost") if len(cs) else ""
    min_c = c.iloc[0]
    econ = meta.get("econ_sensitivity", {})
    roll_first = int((binding.binding == "roll").sum()) if "binding" in binding else 0
    weakest = binding.sort_values("binding_capital").iloc[0] if len(binding) and binding.binding_capital.notna().any() else None
    return {"caps_size": caps_size, "size_fail_c": size_fail_c, "size_fail_what": size_fail_what,
            "fixed_share_central": float(min_c.fixed_cost_share) if "fixed_cost_share" in min_c else np.nan,
            "gross_ret": meta.get("strategy", {}).get("ideal_summary_base", {}).get("ann_ret"),
            "gross_sharpe": meta.get("strategy", {}).get("ideal_summary_base", {}).get("sharpe"),
            "econ": econ, "roll_first": roll_first, "n_products": len(binding), "weakest": weakest,
            "meta": meta, "table": table, "zones": zones, "binding": binding, "torn": torn, "vfe": vfe, "stress": stress,
            "delay": delay, "rd": rd, "caps": caps, "first_fail_c": first_fail_c, "first_fail_what": first_fail_what,
            "floor_fail_max": float(floor) if np.isfinite(floor) else np.nan, "gross_edge": gross_edge, "baseline": base}


def baseline_breakeven(base: dict):
    be = (base.get("capacity_curve", {}) or {}).get("breakeven", {}) if isinstance(base, dict) else {}
    return be.get("aum") if isinstance(be, dict) else None


CN = {"participation": "日内窗口参与率", "roll": "换月旧腿参与率", "oi": "持仓占比（限仓代理）", "runway": "保证金跑道",
      "cost": "成本占毛收益", "floor": "资金下限"}


def cn(names: str) -> str:
    return "、".join(CN.get(x, x) for x in str(names).split(",") if x)


def plain_text(S: dict) -> str:
    cz = S["caps_size"].get("full", {})
    dev, hold = S["caps_size"].get("dev", {}), S["caps_size"].get("holdout", {})
    same = dev.get("central") == hold.get("central")
    lines = []
    g = S.get("gross_ret")
    lines.append(f"被测策略 S0（公开信号：商品期货 63 日截面动量 + 25% 不交易带）在真实合约账本上不扣成本年化 {g:+.1%}"
                 f"（夏普 {S.get('gross_sharpe', float('nan')):.2f}）。" if g is not None else "被测策略 S0 为公开信号。")
    lines.append(f"规模上（只看参与率、换月旧腿、保证金跑道、持仓占比）：中性假设最大可行资金档 {fmt_cap(cz.get('central', 0))}，"
                 f"乐观 {fmt_cap(cz.get('optimistic', 0))}，悲观 {fmt_cap(cz.get('pessimistic', 0))}；"
                 + ("开发期与保留期结论一致。" if same else f"开发期 {fmt_cap(dev.get('central', 0))}、保留期 {fmt_cap(hold.get('central', 0))}，结论随时间变化。"))
    w = S.get("weakest")
    if S["roll_first"]:
        lines.append(f"最先坏的是换月旧腿：{S['n_products']} 个品种里有 {S['roll_first']} 个是移仓先于日常调仓超限"
                     + (f"，最弱的 {w['product']} 在 {fmt_cap(w['binding_capital'])} 时旧腿窗口参与率就超过上限。" if w is not None else "。"))
    if np.isfinite(S["size_fail_c"]):
        lines.append(f"中性假设下从 {fmt_cap(S['size_fail_c'])} 开始，超限的约束是：{cn(S['size_fail_what'])}。")
    fs = S.get("fixed_share_central")
    if fs is not None and np.isfinite(fs):
        grid = S.get("econ", {}).get("grid", [])
        lo = min((x["fixed_cost_share"] for x in grid if x.get("fixed_cost_share") is not None and x["half_spread_tick"] > 0), default=None)
        lines.append(f"经济上：即使在最小资金档，费用加半价差也吃掉毛收益的 {fs:.0%}（中性），"
                     + ("任何规模都超过 50% 的阈值。" if fs > 0.5 else "低于 50% 的阈值。")
                     + (f"成本里最大的一块是半价差；只要半价差不为零，费用再低也只能降到约 {lo:.0%}。" if lo is not None else ""))
    be = baseline_breakeven(S["baseline"])
    if be:
        lines.append(f"对照：股票 ADV 框架（skill-strategy-capacity-tca 默认参数）只算平方根冲击，给出盈亏平衡资金约 {fmt_cap(be)}，"
                     "看不到固定成本和换月旧腿。")
    lines.append("真实冲击无法用公开数据校准，以上是假设下的容量区间，不是资金上限承诺。")
    return "\n".join(lines)


def to_parquet(S: dict, OUTD=OUT):
    """BUILD §11 字段：一行一个 资金档×假设，外加一行总结。"""
    rows = []
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    ver = S["meta"]["data"]["input_digest"]
    for r in S["table"].itertuples():
        rows.append({"trade_date": S["meta"]["windows"][r.window][1] if r.window in S["meta"]["windows"] else None,
                     "build_id": "LSY04-Q7", "build_name": "期货容量边界", "target_id": f"S0@{int(r.capital)}",
                     "result_type": f"capacity_check:{r.window}:{r.assume}", "result_value": float(bool(r.ok_all_upper)),
                     "result_json": json.dumps({k: (None if (isinstance(v, float) and not np.isfinite(v)) else v) for k, v in r._asdict().items() if k != "Index"}, ensure_ascii=False, default=str),
                     "data_version": ver, "update_time": ts})
    rows.append({"trade_date": S["meta"]["windows"]["full"][1], "build_id": "LSY04-Q7", "build_name": "期货容量边界",
                 "target_id": "S0", "result_type": "summary", "result_value": float(S["caps_size"]["full"]["central"]),
                 "result_json": json.dumps({"caps": S["caps"], "caps_size_only": S["caps_size"], "fixed_cost_share_central": S["fixed_share_central"],
                                            "baseline_breakeven_aum": baseline_breakeven(S["baseline"]), "first_size_fail_capital": S["size_fail_c"],
                                            "first_size_fail": S["size_fail_what"]}, ensure_ascii=False, default=str),
                 "data_version": ver, "update_time": ts, "plain_text": plain_text(S)})
    df = pd.DataFrame(rows)
    df.to_parquet(OUTD / "capacity.parquet", index=False)
    return df


def md(S: dict, OUTD=OUT) -> str:
    m = S["meta"]
    z = S["zones"].copy()
    z["capital"] = z.capital.map(fmt_cap)
    full = S["table"][S["table"].window == "full"]
    keep = ["capital", "assume", "rho_p95", "roll_rho_p95", "cost_frac_capital", "net_annual_ret", "runway_fail_share", "oi_share_max", "floor_median_err", "failed"]
    t = full[[c for c in keep if c in full]].copy()
    t["capital"] = t.capital.map(fmt_cap)
    b = S["binding"].copy()
    for c in [x for x in b.columns if x.startswith("first_fail") or x == "binding_capital"]:
        b[c] = b[c].map(lambda x: fmt_cap(x) if pd.notna(x) else "未失效")
    tor = S["torn"].copy()
    for c in ["cap_central", "cap_if_opt", "cap_if_pess"]:
        tor[c] = tor[c].map(fmt_cap)
    caps = pd.DataFrame(S["caps_size"]).T.map(fmt_cap)
    econ = pd.DataFrame(S["econ"].get("grid", []))
    rdm = S["rd"].groupby("k").ratio.median().round(3).to_dict() if len(S["rd"]) else {}
    dl = S["delay"]
    base = S["baseline"]
    be = baseline_breakeven(base)
    parts = [f"# 期货策略放大资金后在哪里失效：容量边界报告\n",
             f"> 生成时间 {m['generated']}；数据 PandaData（输入指纹 `{m['data']['input_digest']}`）；被测策略 S0（公开信号，不是推荐策略）；"
             "研究与教育用途，不构成投资建议。\n",
             "## 结论（人话）\n", plain_text(S).replace("\n", "\n\n"), "\n",
             "## 分区（全样本 2020-01-02 至 2026-11-13）\n",
             "`zone_size_only` 只看规模约束（参与率、换月旧腿、保证金跑道、持仓占比、资金下限）；`zone` 再加上经济约束（成本不超过毛收益的 50%）。\n",
             md_table(z), "\n",
             "## 规模约束下的最大可行资金档（全样本 / 开发期 / 保留期）\n", md_table(caps, index=True), "\n",
             "## 经济可行性：固定成本占毛收益（最小资金档，几乎没有冲击）\n",
             md_table(econ, floatfmt=".3g") if len(econ) else "无", "\n",
             "开发期 2020–2023 与保留期 2024–2026 分别回放。保留期是在三套假设定死之后才计算的，两段差异代表结论的时变性。\n",
             "## 逐资金档 × 假设的约束读数\n", md_table(t, floatfmt=".4f"), "\n",
             "## 品种层面：最先失效的约束（中性假设）\n", md_table(b), "\n",
             "## 单项假设扰动（规模约束）：结论最怕哪一个假设错\n", md_table(tor, floatfmt=".3g"), "\n",
             "## 换月后旧合约成交量衰减（第 k 个交易日 / 换月当日，中位数）\n", json.dumps(rdm, ensure_ascii=False), "\n",
             "## 压力日与平常日的参与率 p95（中性假设窗口）\n", md_table(S["stress"], floatfmt=".4f"), "\n",
             "压力日按事前规则选：品种日成交量处于样本最低 5%，或日收益绝对值处于最高 5%，或收盘在涨跌停。\n",
             "## 成交量预测误差（实际 / 前 20 日中位数）\n", md_table(S["vfe"], floatfmt=".3f"), "\n",
             "参与率是事后口径（用实际成交量）。事前只能用历史中位数预测，p05 列给出预测偏乐观时参与率会被低估多少倍。\n"]
    if len(dl):
        parts += ["## 参与率受限时的拆单延迟代价（真实 5 分钟线，样本）\n",
                  f"抽样 {len(dl)} 笔超限单，按上限逐根成交：完成率 {dl.complete.mean():.0%}，相对到达价执行偏差中位数 {dl.is_bp.median():.1f} bp（正为不利），"
                  f"p90 {dl.is_bp.quantile(0.9):.1f} bp。这一项是真实数据测得的，不含自身冲击。\n"]
    parts += ["## 冻结基线：股票 ADV 框架直接套用\n",
              f"`skill-strategy-capacity-tca`（commit `{m['baseline'].get('baseline_commit')}`，默认参数）用 20 日平均日成交额作 ADV，"
              f"同一批订单、资金 1 亿：盈亏平衡容量读数 = {be if be is not None else '见 baseline_adv/capacity_tca.json'}。返回码 {m['baseline'].get('returncode')}。\n",
              "基线只看日均量，看不到日内窗口、旧合约悬崖、锁板和保证金跑道。两者的差异就是本报告要说明的决策变化。\n",
              "## 适用边界\n",
              "- 冲击系数无法用公开数据校准，只给网格；结论是假设下的容量区间，不是资金上限承诺。\n"
              "- 限仓用持仓占比代理，交易所限仓细则未逐品种核对。\n"
              "- 费用按名义 bp 统一假设，不是逐品种费率表（费用校准属于 Q1 的范围）。\n"
              "- 执行方式固定为日盘开盘后 W 分钟窗口，不比较执行算法（属于 Q16 的范围）；换月规则固定为主力标签切换日开盘换月，不比较换月规则（属于 Q2 的范围）。\n"
              "- 2020 年前成交量与持仓量为双边口径，主样本从 2020-01-02 开始。\n"
              "- 只测了一个公开策略；换成换手更高或品种更冷门的策略，容量会显著不同。\n",
              "## 术语\n", "\n".join(f"- **{a}**：{b}" for a, b in GLOSSARY), "\n"]
    text = "\n".join(parts)
    (OUTD / "report.md").write_text(text)
    return text


def html_report(S: dict, OUTD=OUT) -> str:
    full = S["table"][S["table"].window == "full"].sort_values("capital")
    caps_x = [fmt_cap(c) for c in sorted(full.capital.unique())]
    series = {a: full[full.assume == a].sort_values("capital").rho_p95.round(4).tolist() for a in ["optimistic", "central", "pessimistic"]}
    roll_s = {a: full[full.assume == a].sort_values("capital").roll_rho_p95.round(4).tolist() for a in ["central"]}
    net = full[full.assume == "central"].sort_values("capital").net_annual_ret.round(4).tolist()
    rdm = S["rd"].groupby("k").ratio.median().round(3) if len(S["rd"]) else pd.Series(dtype=float)
    tor = S["torn"]
    zcolor = {"安全": "#2e7d32", "条件": "#f9a825", "高风险": "#ef6c00", "失效": "#c62828", "资金过小": "#546e7a"}
    zone_cells = "".join(f"<div class='z' style='background:{zcolor.get(r.zone_size_only, '#555')}'><b>{fmt_cap(r.capital)}</b><span>{r.zone_size_only}</span><small>{html.escape(','.join(x for x in str(r.failed_central).split(',') if x and x != 'cost'))}</small></div>" for r in S["zones"].itertuples())
    md_text = (OUTD / "report.md").read_text()
    data = {"caps": caps_x, "series": series, "roll": roll_s, "net": net, "rd_k": list(map(int, rdm.index)), "rd_v": rdm.tolist(),
            "tor_l": tor.assumption.tolist(), "tor_v": [float(x) if np.isfinite(x) else 0 for x in tor.swing_x]}
    caps = S["caps_size"]["full"]
    out = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>期货容量边界</title>
<style>:root{{--bg:#0f1218;--fg:#e8eaed;--mut:#9aa0a6;--card:#171b23;--line:#2a2f3a}}body{{background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;margin:0;padding:24px 16px}}
.w{{max-width:1100px;margin:auto}}h1{{font-size:22px}}h2{{font-size:17px;border-left:3px solid #4f8cff;padding-left:8px;margin-top:28px}}
.k{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}.c{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}}
.c b{{font-size:20px;display:block}}.c span{{color:var(--mut);font-size:13px}}.zs{{display:flex;flex-wrap:wrap;gap:6px}}.z{{border-radius:8px;padding:6px 10px;min-width:92px}}.z b{{display:block}}.z span{{font-size:13px}}.z small{{display:block;font-size:11px;opacity:.85}}
canvas{{background:var(--card);border-radius:10px;padding:8px;max-height:320px}}.g2{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}}
pre{{white-space:pre-wrap;background:var(--card);padding:12px;border-radius:10px;border:1px solid var(--line);font-size:13px;overflow:auto}}.note{{color:var(--mut);font-size:13px}}</style></head>
<body><div class="w"><h1>期货策略放大资金后在哪里失效</h1>
<p class="note">被测策略 S0 为公开信号（商品期货 63 日截面动量 + 25% 不交易带，周频），只作为被测对象，不是推荐策略。数据 PandaData，输入指纹 {S['meta']['data']['input_digest']}。研究与教育用途，不构成投资建议。</p>
<div class="c" style="margin:12px 0"><pre style="background:none;border:0;padding:0;margin:0">{html.escape(plain_text(S))}</pre></div>
<div class="k"><div class="c"><b>{fmt_cap(caps['central'])}</b><span>规模约束：中性假设最大可行资金档</span></div><div class="c"><b>{fmt_cap(caps['optimistic'])}</b><span>乐观假设</span></div>
<div class="c"><b>{fmt_cap(caps['pessimistic'])}</b><span>悲观假设</span></div><div class="c"><b>{S['fixed_share_central']:.0%}</b><span>固定成本占毛收益（最小资金档，中性）</span></div>
<div class="c"><b>{fmt_cap(baseline_breakeven(S['baseline']) or 0)}</b><span>对照：ADV 框架的盈亏平衡资金</span></div></div>
<h2>规模分区（只看规模约束）</h2><div class="zs">{zone_cells}</div><p class="note">经济约束另算：见上方"固定成本占毛收益"。</p>
<h2>图表</h2><div class="g2"><canvas id="c1"></canvas><canvas id="c2"></canvas><canvas id="c3"></canvas><canvas id="c4"></canvas></div>
<h2>完整读数</h2><pre>{html.escape(md_text)}</pre></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script>const D={json.dumps(data, ensure_ascii=False)};
if(window.Chart){{Chart.defaults.color='#c9ccd1';Chart.defaults.borderColor='#2a2f3a';
new Chart(c1,{{type:'line',data:{{labels:D.caps,datasets:[{{label:'参与率 p95 乐观',data:D.series.optimistic}},{{label:'中性',data:D.series.central}},{{label:'悲观',data:D.series.pessimistic}},{{label:'移仓旧腿 p95 中性',data:D.roll.central,borderDash:[5,4]}}]}},options:{{plugins:{{title:{{display:true,text:'窗口参与率 vs 资金'}}}},scales:{{y:{{type:'logarithmic'}}}}}}}});
new Chart(c2,{{type:'bar',data:{{labels:D.caps,datasets:[{{label:'中性假设扣成本后年化收益',data:D.net}}]}},options:{{plugins:{{title:{{display:true,text:'净收益 vs 资金'}}}}}}}});
new Chart(c3,{{type:'line',data:{{labels:D.rd_k,datasets:[{{label:'旧合约成交量 / 换月当日（中位数）',data:D.rd_v}}]}},options:{{plugins:{{title:{{display:true,text:'换月后旧合约流动性悬崖'}}}}}}}});
new Chart(c4,{{type:'bar',data:{{labels:D.tor_l,datasets:[{{label:'乐观/悲观 规模容量之比',data:D.tor_v}}]}},options:{{indexAxis:'y',plugins:{{title:{{display:true,text:'单项假设扰动'}}}}}}}});}}
</script></body></html>"""
    (OUTD / "report.html").write_text(out)
    return out


def main(OUTD=OUT):
    S = build_summary(OUTD)
    md(S, OUTD)
    html_report(S, OUTD)
    to_parquet(S, OUTD)
    (OUTD / "report.json").write_text(json.dumps({"plain_text": plain_text(S), "caps": S["caps"], "caps_size_only": S["caps_size"],
                                                  "fixed_cost_share_central": S["fixed_share_central"], "baseline_breakeven_aum": baseline_breakeven(S["baseline"]), "first_fail_capital": S["first_fail_c"],
                                                  "first_fail": S["first_fail_what"], "gross_edge": S["gross_edge"]}, ensure_ascii=False, indent=1, default=str))
    print(plain_text(S))


if __name__ == "__main__":
    main()
