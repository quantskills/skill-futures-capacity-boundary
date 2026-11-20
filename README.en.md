# Where a futures strategy breaks as capital scales: capacity boundary (skill-futures-capacity-boundary)

> QuantSkills Community Project, not reviewed, certified or endorsed by the maintainers. For quantitative research and education only. **Not investment advice, no return promises, no automatic order placement.**

When a futures strategy is scaled up, which constraint breaks first, and which assumption is that judgement most sensitive to? **The output is a capacity range, not a single capital limit.** Chinese version: [README.md](README.md).

## Findings (strategy S0, 2020-01-02 to 2026-11-13, 11 commodity futures)

S0 is a public signal (63-day cross-sectional momentum with a 25% no-trade band, weekly). It is only the object under test, not a recommended strategy. Full report: [examples/sample_out/report.md](examples/sample_out/report.md).

| Layer | Finding |
|---|---|
| Size constraints (participation, old roll leg, margin runway, open-interest share) | Largest feasible capital: **CNY 200m** central, 500m optimistic, 50m pessimistic; development and holdout periods agree |
| Zones | ≤50m safe; 100–200m conditional; 500m high risk; ≥1bn fail |
| First constraint to bind | **The old leg of the roll**, in all 11 products; for SA the old-leg window participation already exceeds 10% at 200m |
| Liquidity cliff of the old contract | Old-contract volume falls to 54% of pre-roll level on day 3, 37% on day 5, 16% on day 10 |
| Stress days | p95 participation on stress days is about 1.9× normal days, roughly halving capacity |
| Economics | Gross return +1.7%/yr (Sharpe 0.20); at the smallest size, fees plus half-spread already consume **88%** of gross return |
| Frozen baseline (equity ADV framework applied as is) | `skill-strategy-capacity-tca` (commit 6ae79f7) gives a break-even of about **CNY 106m**; it only models square-root impact and misses fixed costs, intraday windows and the roll leg |

**Decision change:** the ADV baseline suggests the strategy can run up to about 100m. This report says instead that it is uneconomic at any size (thin edge × high turnover), and that even for a strategy with enough edge the first thing to fix is the roll schedule, not daily rebalancing participation. A reliable negative result.

## Method

Constraints: capital floor (lot rounding), intraday participation in the first 15/30/60 minutes of the day session on the actual contract (5-minute bars), fees + half-spread + square-root impact with an assumed coefficient grid (0.3/0.5/1.0, not calibrated), roll pressure on old and new legs, margin runway under adverse limit moves plus margin hikes (failure when liquidation takes longer than the margin lasts), and open-interest share as a position-limit proxy. Three assumption sets were frozen before full results ([references/scope.md](references/scope.md)); the holdout (2024 onwards) was computed only after that.

## Usage

```bash
pip install -r requirements.txt
python scripts/fetch_data.py                 # PandaData download (credentials via environment variables)
python scripts/build.py --check              # validate inputs and data only
python scripts/build.py --out q7_out         # full run (about 1 minute)
python scripts/test.py                       # offline self-tests
```

In QuantStudio: place the repository under `quantskills-drafts/` in the workspace, publish it as a local skill via "AI create skill", then fill the session form and click "apply and run". The recorded QuantStudio v0.1.36 run is in [platform_evidence/quantstudio/](platform_evidence/quantstudio/); its results match the local run.

## Reviewer data snapshot

Market data is not redistributed in this public repository. The frozen data for the 11 products is provided as four zip attachments to the 量枢院 submission (`LSY04-Q7_frozen_data_snapshot_part{1..4}of4.zip`, sha256 listed in [README.md](README.md)). `python scripts/verify_snapshot.py <dir>` extracts them into `data/`, checks every file against [references/data_snapshot_manifest.json](references/data_snapshot_manifest.json) and the frozen `input_digest`, then `python scripts/build.py --out q7_out` reproduces `examples/sample_out/` byte for byte (verified in a clean copy).

## Data source, assumptions, parameters, limitations and risk

Data source: PandaData futures interfaces (dominant contracts, contract details, daily and 5-minute bars); measured data conventions are in [references/api_guide.md](references/api_guide.md) (e.g. volume and open interest on SHF/DCE/CZC were double-sided before 2020). Assumptions, parameters and known limitations are listed in [references/source_boundary.md](references/source_boundary.md). Main limitations: the impact coefficient is assumed, so results are capacity ranges under assumptions; position limits are proxied by open-interest share; fees use a uniform bp assumption; one public strategy only.

**QuantSkills Community Project, for quantitative research and education only. Not investment advice, no return promises.**
