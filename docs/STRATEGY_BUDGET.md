# Independent paper capital / 独立模拟预算

Alpaca and moomoo can each run with an independent USD 5,000 stock ledger,
without resetting broker balances. This means USD 10,000 across two separate
simulated accounts, with no transfers between them.

脚本按每家 5,000 USD 计算仓位和风控，券商余额不变。旧成交、日志、授权、
止损锁和历史报告保留。启用时建立新的统计起点，重启不会清零亏损。

## Calculation / 计算方式

- Equity = initial budget + FIFO realized stock profit + unrealized stock profit
  minus estimated moomoo NZ stock fees, applied consistently to both desks.
- Extra broker buying power, deposits, old profits and short-sale proceeds do not
  expand the budget. Actual broker cash/equity can further restrict trading.
- Positions and pending orders reserve gross exposure. Estimated opening and
  closing fees also reduce spendable budget.
- Allocation remains 10% target per stock, 40% single-stock hard cap and 100%
  combined gross exposure: initially about USD 500, USD 2,000 and USD 5,000.
  These amounts change with equity.
- Existing daily loss limits remain 1% for Alpaca and 2% for moomoo: initially
  USD 50 and USD 100. Gaps, slippage and order failures can exceed software limits.
- Stock entries only: new option entries are disabled. Entry limits, cooldowns,
  short eligibility checks and stop files remain in force.
- Fees include the NZ stock TAF schedule but exclude borrow charges, FX, taxes
  and actual slippage. Estimates are a comparison model, not broker invoices.

页面显示本轮预算权益、累计净盈亏和估算费用；当日收益按当天持久化基线计算。
旧历史保留。连接的模拟账户应专用于脚本；手动交易会影响对账。成交记录与实际
持仓不一致时暂停新开仓，已有持仓仍经过退出管理。

## Activation / 启用

### Capital-aware stock entries / 小资金入场适配

Budget-enabled executors assess whole-share affordability and order economics
before reserving an entry. They keep the existing 10% target allocation, 40%
single-stock cap, gross limit, stop/target rules and daily loss locks.
No fractional-share support or leverage is added.

The guard uses estimated NZ stock fees on both desks, the observed spread and
10 basis points of assumed round-trip slippage. Cost must be at most 35% of the
gross 2% target, and modeled net target profit must cover the modeled 1% stop
loss including costs. These are scenario checks, not expected returns or guaranteed
execution prices. Final Alpaca submission repeats a fee/slippage floor check;
the candidate check already includes the quote spread.

Modeled per-trade loss is limited to 0.5% of current strategy equity. Existing gross
exposure reserves 1% risk, plus estimated position exit fees and USD 2.10 per pending
entry for additional costs; this remains an estimate, especially during gaps or
large/partial fills. These reserves must fit the remaining daily loss allowance.
Budget equity below USD 2,000 permits long entries only; this is a conservative
local rule, not a statement about a broker's legal margin eligibility.

At USD 500 the target ticket is approximately USD 50: many stocks are unaffordable
and even affordable orders often fail the cost check. A lack of trades is an
intentional result; do not interpret this adaptation as proof that minute trading
is viable at that capital. At USD 10,000 the target ticket is approximately USD
1,000, so costs are a smaller proportion, but entries still need valid signals.

Both pages show guard evaluation counts and rejection reasons, including repeat
checks; these are not counts of unique trading opportunities. Daily records persist
under private `state/capital_guard/`. Existing exits run before entry guards.
This change has scenario/regression validation, not evidence of profitable market
performance or a new optimized buy/sell signal.

Configure and authorize PAPER/SIMULATE accounts following [Docker deployment](DOCKER.md).
Build the current paper image first. Stop workers; all enabled accounts must be
flat with no pending orders or unresolved intents. Setup checks the brokers
read-only and refuses to overwrite an existing budget.

With the same private runtime environment and Compose files as deployment:

```sh
docker compose -f compose.paper.yaml -f compose.host.yaml stop lab
docker compose -f compose.paper.yaml -f compose.host.yaml run --rm --no-deps lab activate-budget --capital 5000
docker compose -f compose.paper.yaml -f compose.host.yaml up -d --no-build --wait
```

Use the host override only where host networking is already enabled; otherwise
omit `-f compose.host.yaml` throughout. Failed checks require resolving the
condition before retrying. Setup never resets balances or submits liquidation orders.

Private account bindings and durable snapshots live under `state/strategy_budget/`;
never commit them or delete them to bypass losses or risk limits. This feature
grants no live-money permission and does not guarantee profitability.
