# 证据索引（按第四期评分项）

任务：LSY04-Q7「期货策略放大资金后在哪里失效」（TS20261019B2758A），运行平台 QuantStudio。

| 评分项 | 分 | 证据（文件 / 命令 / 预期结果） |
|---|---:|---|
| 完成度·核心功能 | 20 | `scripts/capacity.py`（六约束：资金下限、日内参与率、冲击与成本、移仓、保证金跑道、持仓占比）；`scripts/run_q7.py`（资金档 × 三套假设、分区、品种约束、假设扰动、开发期/保留期、压力日、成交量预测误差、拆单延迟、ADV 基线）。`python scripts/build.py --out q7_out` 约 1 分钟产出全部文件 |
| 完成度·输出可用 | 10 | `examples/sample_out/report.md` 的"结论（人话）""分区""规模约束下的最大可行资金档""品种层面最先失效的约束""单项假设扰动""冻结基线"各节；`report.json` 的 `plain_text`、`caps_size_only`、`fixed_cost_share_central`、`baseline_breakeven_aum` |
| 方法·数据来源 | 12 | `references/api_guide.md`（接口、字段、实测口径与证据）；`examples/sample_out/meta.json` 的 `data.input_sha256_16`（逐文件指纹）与 `input_digest`；`scripts/fetch_data.py` 写 `fetch_log.jsonl`；2020 年前双边口径修正 `futures_replay/data.py::fix_double_sided` |
| 方法·时点与算术 | 13 | `scripts/futures_replay/test_engine.py`：手算校验按结算价盯市、费用、乘数、资金守恒、锁板顺延、换月两腿定价、双边口径修正、主力链收益（`python scripts/test.py` 全部通过）；主力标签时点验证见 `references/api_guide.md`；分钟 K 线按结束时间、夜盘归属交易日 |
| 方法·对照与验证 | 10 | 冻结基线：`scripts/vendor/capacity_tca_6ae79f7.py`（原样附带）输出在 `examples/sample_out/baseline_adv/`；事前范围声明 `references/scope.md`（写定时间早于全量结果，见开发仓库提交 46eda71 01:57）；三套假设固定于 `scripts/capacity.py::ASSUMPTIONS`；保留期 2024–2026 单独计算（`capital_table.csv` 的 `window=holdout`）；尝试账本 `ATTEMPTS.md`（被测策略选型两次尝试均记录） |
| 交付·平台实测 | 10 | `platform_evidence/quantstudio/`：发布会话 `sessions/session-2f63684e-….json`（创作助手准备草稿并发布）；技能版本身份 `published_skill_manifest.json`（commit 85336537，treeDigest d9da6aeb…，origin local-authoring）；运行会话 `sessions/session-bee0a315-….json`（从表单发起，执行 build.py）；运行产物 `q7_out/`。QuantStudio 运行结果与本机运行逐项一致（`capital_table.csv` 前 12 列 diff 为空）。公开前只把会话与 `meta.json` 里的本机用户目录替换为 `~`、开发仓库路径替换为 `<dev-repo>`，并删去两个没有内容的空会话，其余原样 |
| 交付·复现追溯 | 8 | `meta.json`（生成时间、窗口、资金档、假设全文、输入指纹、基线命令与 commit、策略版本）；`capacity.parquet` 每行带 `data_version`；运行是确定性的（同输入同输出，无随机过程）；评审数据快照（1 个 zip 附件，1209 个文件，清单 `references/data_snapshot_manifest.json`）+ `scripts/verify_snapshot.py`：干净副本里核对通过后 `build.py` 输出与 `examples/sample_out/` 逐字节相同（见 README"评审数据快照"） |
| 交付·异常处理 | 7 | `scripts/build.py::validate_input`（非 S0 策略、品种少于 5 个、资金档非正、窗口格式错误、开发期晚于保留期、数据缺失时给出补数命令）；`scripts/test.py::test_validate_input`；`fetch_data.py` 重试与单日流量超限（500009）停止；毛收益不为正时成本约束的退路规则 |
| 文档·第三人上手 | 6 | `README.md`（安装、取数、校验、运行、自测、QuantStudio 用法）；`SKILL.md`（QuantSkills 2.1 声明 + QuantStudio 表单 `qsh-form`） |
| 文档·局限说明 | 4 | `references/source_boundary.md`（来源、假设表、参数、已知限制、风险边界）；`README.md` 适用边界一节 |
