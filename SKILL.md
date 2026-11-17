---
name: skill-futures-capacity-boundary
description: 期货策略容量边界：对一个冻结的期货策略，在一组资金档上逐项检查资金下限、具体合约日内窗口参与率、平方根冲击成本、换月旧腿压力、保证金跑道（平仓天数对比追保天数）和持仓占比，给出安全/条件/高风险/失效分区、最先失效的约束和假设误差分析，并与股票 ADV 容量框架对照。Use when a futures strategy needs a capacity range instead of a single fake capital limit, or when asking where scaling up capital breaks a CTA. Research and education only, not investment advice.
license: GPL-3.0-only
supported-runtimes: [cursor, claude-code, codex, hermes, openclaw]
metadata:
  organization: QuantSkills
  organization_url: https://github.com/quantskills
  repository: skill-futures-capacity-boundary
  repository_url: https://github.com/quantskills/skill-futures-capacity-boundary
  project_type: skill
  collection: futures-capacity
  maintainer: ZLHad
  tags: [futures, capacity, participation, roll, margin, market-impact, pandadata]
quantSkills:
  schema_version: 2.1.0
  organization: quantskills
  organization_url: https://github.com/quantskills
  repository: skill-futures-capacity-boundary
  repository_url: https://github.com/quantskills/skill-futures-capacity-boundary
  project_type: skill
  license: GPL-3.0-only
  maintainer: ZLHad
  collection: futures-capacity
  catalog:
    category: "04"
    subcategory: 04.liquidity-risk
  workflow:
    primary_stage: risk
    workflow_stages: [data-ingestion, evaluation, risk, reporting]
  tags: [futures, capacity, participation, roll, margin, market-impact, pandadata]
  requires: [skill-strategy-capacity-tca]
  platforms: [cursor, claude-code, codex, hermes, openclaw]
  status: active
  validation_level: runnable
  maintainer_type: community
  summary_zh: "给期货策略一个容量区间而不是虚假的资金上限：按具体合约日内参与率、换月旧腿、保证金跑道与持仓占比逐档检查，标出最先失效的约束。"
  summary_en: "Capacity range, not a fake capital limit, for a frozen futures strategy: intraday participation, roll legs, margin runway and OI share per capital level, with assumption error analysis; research only."
  interface:
    mode: natural-language
---

```json qsh-form
{
  "version": 1,
  "task": {
    "placeholder": "可选：补充你关心的资金规模、品种或假设（例如把冲击系数中性值改成 0.8）",
    "required": false
  },
  "fields": [
    {
      "key": "python",
      "label": "Python 解释器（需已安装 numpy、pandas、pyarrow）",
      "type": "text",
      "default": "python3"
    },
    {
      "key": "data_dir",
      "label": "数据目录（PandaData 缓存）",
      "type": "text",
      "default": "~/.futures_replay/data"
    },
    {
      "key": "products",
      "label": "品种（逗号分隔，至少 5 个）",
      "type": "text",
      "default": "RB,I,CU,AL,M,P,MA,TA,SA,AP,JD"
    },
    {
      "key": "capitals",
      "label": "资金档（元，逗号分隔）",
      "type": "text",
      "default": "5e6,1e7,2e7,5e7,1e8,2e8,5e8,1e9,2e9,5e9"
    },
    {
      "key": "out",
      "label": "输出目录",
      "type": "text",
      "default": "q7_out"
    },
    {
      "key": "mode",
      "label": "运行方式",
      "type": "select",
      "default": "full",
      "options": [
        {
          "value": "full",
          "label": "完整计算并生成报告"
        },
        {
          "value": "check",
          "label": "只校验输入与数据"
        }
      ]
    }
  ],
  "prompt_template": "{{#task}}补充要求：\n{{task}}\n\n{{/task}}按本技能的 SKILL.md 运行期货容量边界分析：品种 {{products}}，资金档 {{capitals}}，输出目录 {{out}}，运行方式 {{mode}}。在本技能目录下执行：先运行 {{python}} scripts/build.py --check --products {{products}} --data-dir {{data_dir}}；数据缺失时按提示运行 {{python}} scripts/fetch_data.py --products <缺失品种> --data-dir {{data_dir}}（需要 PandaData 凭证）。mode=full 时再运行 {{python}} scripts/build.py --products {{products}} --capitals {{capitals}} --data-dir {{data_dir}} --out {{out}}（输出目录写到当前会话工作区）。完成后读取 {{out}}/report.json 与 report.md，用中文复述 plain_text 结论、规模分区、最先失效的约束、固定成本占毛收益、与 ADV 基线的差异，并逐条说明适用边界；冲击系数是假设不是校准值，不给买卖指令。"
}
```

# 期货策略容量边界（Futures Capacity Boundary）

> 项目状态：QuantSkills **社区项目（Community Project）**，未经维护者审核、认证或背书。仅供量化研究与教育示例，**不构成投资建议，不承诺收益，不自动下单**。

## 它回答什么

**一个期货策略放大资金后，在哪里先坏、坏在哪个约束、这个判断最怕哪个假设错。** 输出是容量区间，不是单一资金上限。

股票的 ADV 容量框架直接套到期货上，会漏掉四件事：

| 期货特有的约束 | 怎么算 | ADV 框架为什么看不到 |
|---|---|---|
| 日内窗口参与率 | 母单占执行窗口（15/30/60 分钟）内该**具体合约**真实成交量的比例 | ADV 把一天摊平；同一天分钟量中位数和最大值可差十几倍 |
| 换月旧腿压力 | 按冻结的换月规则，旧合约平仓腿的窗口参与率，以及换月后旧合约成交量的衰减曲线 | 只看主力合约，看不到旧合约的流动性悬崖 |
| 保证金跑道 | 压力情景（逆向触板 + 提保）下权益撑几天（T_run）对比按参与率上限和锁板平完仓要几天（T_liq）；T_liq > T_run 判失效 | 股票没有保证金和锁板的联动 |
| 资金下限与持仓占比 | 整手取整误差过大判"资金过小"；单合约持仓占持仓量比例作限仓代理 | 股票可以零碎买，没有限仓 |

## 怎么用

```bash
pip install -r requirements.txt
# PandaData 凭证：环境变量 PANDA_USERNAME / PANDA_PASSWORD，或 ~/.pandadata/pandadata.env
python scripts/fetch_data.py                      # 拉取主力序列、日线、5 分钟线（可断点续拉）
python scripts/build.py --check                   # 只校验输入与数据
python scripts/build.py --out q7_out              # 完整计算，生成报告
python scripts/test.py                            # 离线自测（不需要 PandaData）
```

程序化调用：

```python
import sys; sys.path.insert(0, "scripts")
from build import run
res = run({"products": ["RB", "I", "M", "MA", "SA"], "capitals": [1e7, 1e8, 1e9]}, config={"out": "out"})
print(res["plain_text"])
```

在 QuantStudio 里：技能页打开本技能，填表单（Python 解释器、数据目录、品种、资金档、输出目录、运行方式）后发起会话；模型在技能目录下执行上面的命令，结果写到会话工作区的输出目录，报告用结果工作台打开。

## 输入与输出

| 输入 | 说明 |
|---|---|
| products | 品种代码列表，至少 5 个（被测策略是截面策略） |
| capitals | 资金档列表（元） |
| full / dev / holdout | 全样本、开发期、保留期窗口；开发期必须早于保留期 |
| config.assumptions | 覆盖三套假设中的字段，例如 `{"central": {"impact_y": 0.8}}` |

| 输出文件 | 内容 |
|---|---|
| report.md / report.html / report.json | 人话结论、分区、逐档读数、品种层面最先失效约束、假设扰动、保留期对照、压力日、成交量预测误差、拆单延迟代价、ADV 基线对照、适用边界、术语 |
| capacity.parquet | BUILD 结果表：`trade_date / build_id / target_id / result_type / result_value / result_json / data_version / update_time`，汇总行带 `plain_text` |
| capital_table.csv、zones.csv、product_binding.csv、tornado.csv、roll_decay.csv 等 | 中间结果，便于复核 |
| meta.json | 版本、输入指纹（逐文件 sha256）、窗口、假设、基线命令与返回 |

## 方法要点

1. **被测策略 S0 是公开信号**（商品期货 63 日截面动量 + 25% 不交易带，周频，逆波动率加权，带止损、回撤停机、保证金上限、换月规则），只作为被测对象，不是推荐策略。
2. **真实合约账本**：按具体合约逐日结算价盯市，换月日旧腿按旧合约开盘价平仓，锁板方向不成交。
3. **三套假设事前固定**（乐观、中性、悲观），见 `scripts/capacity.py` 的 `ASSUMPTIONS` 和 [references/scope.md](references/scope.md)。分区：悲观下全部通过为"安全"，只在中性下通过为"条件"，只在乐观下通过为"高风险"，乐观下也不通过为"失效"。
4. **能实测的照实测**：窗口真实成交量、旧合约成交量衰减、按参与率上限逐根成交的执行偏差（不含自身冲击）。
5. **保留集**：2024 年以后在假设定死之后才计算。

## 适用边界

- 冲击系数无法用公开数据校准，结论是**假设下的容量区间**，不是资金上限承诺。
- 限仓用持仓占比代理，未逐品种核对交易所限仓细则；费用按名义 bp 统一假设，不是逐品种费率表。
- 执行方式固定为日盘开盘后 W 分钟窗口，换月规则固定为主力标签切换日开盘换月；不比较执行算法或换月规则。
- SHF/DCE/CZC 在 2020 年前成交量与持仓量为双边口径，主样本从 2020-01-02 开始。
- 目前只支持内置的冻结策略 S0；自定义订单输入尚未实现。

## 数据与接口

PandaData：`get_future_dominant`、`get_future_detail`、`get_future_daily`、`get_future_min`（5 分钟）。口径与实测发现见 [references/api_guide.md](references/api_guide.md)；来源、假设、参数、限制见 [references/source_boundary.md](references/source_boundary.md)。

## 术语（学术说法 → 人话）

- **参与率**：你的单子占同一时间段市场成交量的比例。
- **平方根冲击**：冲击成本大致随交易量占比的平方根增长；系数靠假设，不是校准值。
- **保证金跑道**：极端行情里权益还能撑几天不被追保。
- **移仓压力**：换月时旧合约成交量断崖下降，越晚移仓越难平。
- **保留集**：先不看的数据，用来检验结论稳不稳。

**QuantSkills Community Project，仅供量化研究与教育示例，不构成投资建议，不承诺收益。**
