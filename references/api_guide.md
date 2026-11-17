# 数据接口与口径（PandaData，期货）

凭证只从环境变量 `PANDA_USERNAME / PANDA_PASSWORD` 或 `~/.pandadata/pandadata.env` 读取，仓库不含凭证。数据目录：环境变量 `FUTURES_REPLAY_DATA`，否则仓库根目录 `data/`。

## 用到的接口

| 接口 | 用途 | 关键字段 |
|---|---|---|
| `get_future_dominant(underlying_symbol, start_date, end_date)` | 每日主力合约标签 | `date, symbol` |
| `get_future_detail(symbol)` | 合约规格 | `contract_multiplier, margin_rate, trading_hours, de_listed_date` |
| `get_future_daily(symbol, start_date, end_date)` | 具体合约日线 | `open, high, low, close, settlement, pre_settlement, limit_up, limit_down, volume, open_interest, amount` |
| `get_future_min(symbol, start_date, end_date, frequency="5m")` | 具体合约 5 分钟线 | `datetime, trading_date, open, high, low, close, volume, amount, open_interest` |

拉取范围：每个品种在样本期内当过主力的全部具体合约，从其首次成为主力前 60 天到最后一次是主力后 75 天（或退市日）。`scripts/fetch_data.py` 可断点续拉，每个文件写 sha256 到 `fetch_log.jsonl`；报告的 `meta.json` 记录全部输入文件指纹及总摘要 `input_digest`。

## 实测口径（文档没写或写错的地方）

| 项目 | 实测结论 | 证据 | 处理 |
|---|---|---|---|
| 2020 年前成交量、持仓量 | SHF/DCE/CZC 为**双边**口径，2020-01-01 起为单边；CFE 一直单边 | 同一合约持仓量隔夜减半：RB2005 2,948,358 → 1,443,715；CU2003 210,762 → 111,621；M2005 3,385,712 → 1,675,406；I2005 1,198,848 → 631,791；MA2005 1,557,018 → 681,258；TA2005 1,342,486 → 661,144；IF2001 73,026 → 81,731（不减半） | 2020 年前三家交易所的成交量、持仓量、成交额减半并打 `vol_fixed` 标；主样本从 2020-01-02 开始 |
| 分钟线 `volume` | 是当根成交量，不是文档写的"当日成交量" | RB2701 在 2026-10-30 的分钟成交量合计 597,934，与日线完全相等 | 按当根使用 |
| 分钟线时间戳 | K 线按结束时间标记；夜盘 `datetime` 为自然日，`trading_date` 为归属交易日 | 首根夜盘 K 线 21:01（1 分钟）/ 21:05（5 分钟） | 窗口按 `trading_date` 与结束时间选取 |
| 主力标签时点 | D 日的主力标签由 D-1 收盘持仓量交叉决定 | RB 前 5 次换月（2019-12-05、2020-03-20、2020-08-24、2020-12-04、2021-04-01）均在持仓交叉后的下一个交易日切换 | D 日开盘按标签换月，不含未来信息 |
| `margin_rate` | 已到期合约返回交割月提高后的最终值 | RB2105 返回 0.20，在交易的 RB2701 返回 0.07 | 历史回放使用品种当前在交易合约的一般月份保证金率，提保用情景处理 |
| 盘口、逐笔、手续费、限仓 | PandaData 期货接口中没有 | 检查了 `panda_data` 暴露的全部期货与席位函数 | 冲击系数只给假设网格；费用按名义 bp 统一假设；限仓用持仓占比代理 |

## 最小变动价位

RB 1、I 0.5、CU 10、AL 5、M 1、P 2、MA 1、TA 2、SA 1、AP 1、JD 1（交易所合约规格，用于半价差与滑点换算；未逐一联网核对历史变更）。

## 错误与限流

PandaData 有单日流量上限（错误码 500009），超限时停止并记录，次日续拉；其他错误重试 4 次，间隔递增。
