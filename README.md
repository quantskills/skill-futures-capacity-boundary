# 期货策略放大资金后在哪里失效：容量边界（skill-futures-capacity-boundary）

> QuantSkills 社区项目（Community Project），未经维护者审核、认证或背书。仅供量化研究与教育示例，**不构成投资建议，不承诺收益，不自动下单**。

一个期货策略放大资金后，在哪里先坏、坏在哪个约束、这个判断最怕哪个假设错。**输出是容量区间，不是单一资金上限。**

## 结论（被测策略 S0，2020-01-02 至 2026-11-13，11 个商品期货品种）

被测策略 S0 是公开信号（63 日截面动量 + 25% 不交易带，周频），只作被测对象，不是推荐策略。完整报告见 [examples/sample_out/report.md](examples/sample_out/report.md) 和 [report.html](examples/sample_out/report.html)。

| 层面 | 结论 |
|---|---|
| 规模约束（参与率、换月旧腿、保证金跑道、持仓占比） | 中性假设最大可行资金档 **2 亿**，乐观 5 亿，悲观 5000 万；开发期与保留期结论一致 |
| 规模分区 | ≤5000 万安全；1–2 亿条件；5 亿高风险；≥10 亿失效 |
| 最先坏的约束 | **换月旧腿**：11 个品种全部是移仓先于日常调仓超限；最弱的 SA 在 2 亿时旧腿窗口参与率就超过 10% |
| 旧合约流动性悬崖 | 换月后第 3 天旧合约成交量剩 54%，第 5 天 37%，第 10 天 16%；移仓晚 5 天，旧腿参与率放大约 2.7 倍 |
| 压力日 | 压力日参与率 p95 约为平常日的 1.9 倍，容量约减半 |
| 经济可行性 | 毛收益年化 +1.7%（夏普 0.20）；最小资金档上费用 + 半价差已吃掉毛收益的 **88%**（中性），任何规模都超过 50% 的阈值；最大的一块是半价差 |
| 冻结基线（股票 ADV 框架直接套用） | `skill-strategy-capacity-tca`（commit 6ae79f7，默认参数）给出盈亏平衡资金约 **1.06 亿**：它只算平方根冲击，看不到固定成本、日内窗口和换月旧腿 |

**它改变了什么决策：** ADV 基线会让人以为这个策略在 1 亿以内可以跑。本报告给出两条不同的结论：经济上任何规模都不划算，问题在"优势薄 × 换手高"，不在规模；即便换一个优势足够的策略，最先需要处理的也是换月（提前、分多日移仓，SA 等品种限额），而不是日常调仓的参与率。这是可靠的负结果。

## 方法

| 约束 | 怎么算 |
|---|---|
| 资金下限 | 整手取整误差中位数超过 25% 判"资金过小" |
| 日内参与率 | 母单占日盘开盘后 15/30/60 分钟窗口内该**具体合约**真实成交量的比例（5 分钟线） |
| 冲击与成本 | 费用 + 半价差 + 平方根冲击 `Y·σ_日·√(Q/V_日)`；Y 取 0.3/0.5/1.0 网格，**不是校准值**；另报按参与率上限逐根成交的真实执行偏差（不含自身冲击） |
| 移仓压力 | 按冻结的换月规则，旧腿与新腿分别算窗口参与率；另给旧合约成交量衰减曲线 |
| 保证金跑道 | 压力情景（逆向触板 + 提保）下权益撑几天（T_run）对比按参与率上限和锁板平完仓要几天（T_liq），T_liq > T_run 判失效 |
| 持仓占比 | 单合约持仓占持仓量比例（限仓代理） |

三套假设（乐观 / 中性 / 悲观）在全量结果之前写定（[references/scope.md](references/scope.md)），保留期（2024 年起）在假设定死之后才计算。完整假设表见 [references/source_boundary.md](references/source_boundary.md)。

## 怎么用

```bash
pip install -r requirements.txt
python scripts/fetch_data.py                 # PandaData 拉取（需要凭证），可断点续拉
python scripts/build.py --check              # 只校验输入与数据
python scripts/build.py --out q7_out         # 完整计算（约 1 分钟）
python scripts/test.py                       # 离线自测（不需要 PandaData）
```

**在 QuantStudio 里**：把仓库放到工作区的 `quantskills-drafts/` 下，用"技能 → AI 创建技能"发布为本地技能；在会话的"本会话能力 → 参数"里填表单（Python 解释器、数据目录、品种、资金档、输出目录、运行方式），点"应用并运行"。本仓库在 QuantStudio v0.1.36 上的实际运行记录见 [platform_evidence/quantstudio/](platform_evidence/quantstudio/)。

## 评审数据快照（不用 PandaData 账号也能复跑）

行情数据不随公开仓库分发。原冻结的 11 个品种（RB、I、CU、AL、M、P、MA、TA、SA、AP、JD）行情快照作为量枢院成果附件提供，共 4 个 zip（1209 个文件：主力序列 11、日线 399、合约信息 399、5 分钟线 399、拉取日志 1）：

| 文件 | sha256 |
|---|---|
| `LSY04-Q7_frozen_data_snapshot_part1of4.zip` | `9196596c37098705ae2dbf975a4745011bd674259b83e1ea6a40ad3f0fe05198` |
| `LSY04-Q7_frozen_data_snapshot_part2of4.zip` | `988fe9949247be3a0099476abc545eb941407ba8b08cc6ec6693ac156f85da35` |
| `LSY04-Q7_frozen_data_snapshot_part3of4.zip` | `c88166523052bcc22efea98b39dbc7e4a67cade8e5ac4851a2a3ec84c3419741` |
| `LSY04-Q7_frozen_data_snapshot_part4of4.zip` | `02462d3720993ffe2ed79e1b921ce41c0b30c73f740521d0f2d9fcdb810fd002` |

```bash
python scripts/verify_snapshot.py <放 4 个 zip 的目录>   # 解压到仓库根目录 data/ 并核对
python scripts/build.py --out q7_out                     # 约 1 分钟，生成容量表与换月曲线
```

`verify_snapshot.py` 逐文件核对 sha256（清单 [references/data_snapshot_manifest.json](references/data_snapshot_manifest.json)），核对主力序列与日线指纹等于冻结运行 `examples/sample_out/meta.json` 的 `input_digest`（`3a216d03b7f24ea8`），5 分钟线指纹等于拉取日志，并确认 11 个品种的四类数据齐全。在不含本机任何数据的干净副本里实测：核对通过后 `build.py` 生成的 `capital_table.csv`、`roll_decay.csv`、`zones.csv`、`product_binding.csv`、`tornado.csv` 等 8 张表与 `examples/sample_out/` 逐字节相同。缺 5 分钟线时 `build.py` 会直接报错，不会静默算空。

## 输出

`report.md / report.html / report.json`、`capacity.parquet`（BUILD 结果表，汇总行带 `plain_text`）、逐档读数 `capital_table.csv`、分区 `zones.csv`、品种约束 `product_binding.csv`、假设扰动 `tornado.csv`、旧合约衰减 `roll_decay.csv`、压力日对比、成交量预测误差、拆单延迟样本、基线输出 `baseline_adv/`、元数据 `meta.json`（版本、逐文件输入指纹、窗口、假设、基线命令）。

## 数据口径（实测，文档没写或写错）

- SHF/DCE/CZC 在 2020 年前成交量、持仓量是双边口径（同合约持仓隔夜减半，CFE 不变），主样本从 2020-01-02 开始。
- 分钟线 `volume` 是当根成交量，不是文档写的"当日成交量"。
- 已到期合约的 `margin_rate` 是交割月提高后的最终值，不能当历史一般月份保证金率。
- 主力标签 D 日由 D-1 收盘持仓量交叉决定，D 日开盘换月不含未来信息。

详见 [references/api_guide.md](references/api_guide.md)。

## 适用边界

冲击系数靠假设，结论是假设下的容量区间，不是资金上限承诺；限仓用持仓占比代理；费用按名义 bp 统一假设（不做费用校准）；执行方式与换月规则固定，不比较算法；只测一个公开策略，换手更高或品种更冷门的策略容量会显著不同；自定义订单输入尚未实现。

**QuantSkills Community Project，仅供量化研究与教育示例，不构成投资建议，不承诺收益。**
