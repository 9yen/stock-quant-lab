
import yfinance as yf
import pandas as pd

# 提前下载2020年数据用于200日均线预热
prices = yf.download(
    "SPY",
    start="2020-01-01",
    end="2026-10-01",
    auto_adjust=True,
    multi_level_index=False,
    progress=False
)

if prices.empty:
    raise RuntimeError("Failed to download SPY data")

# 当日开盘到下一交易日开盘的收益
prices["next_return"] = (
    prices["Open"].shift(-1) / prices["Open"] - 1
)

strategies = {}

for short, long in [(10, 30), (20, 50), (50, 200)]:

    ma_short = prices["Close"].rolling(short).mean()
    ma_long = prices["Close"].rolling(long).mean()

    # 收盘生成信号，下一交易日开盘执行
    signal = (ma_short > ma_long).astype(int)
    position = signal.shift(1).fillna(0)

    # 每次调整仓位成本为0.1%
    turnover = position.diff().abs().fillna(position)

    daily_return = (
        position * prices["next_return"]
        - turnover * 0.001
    )

    strategies[f"MA{short}/{long}"] = daily_return

# 加入两个基准
strategies["70/30"] = prices["next_return"] * 0.7
strategies["SPY"] = prices["next_return"]

results = pd.DataFrame(strategies)

# 确保所有策略使用相同的比较日期
results = results.loc["2021-01-01":"2026-09-30"].dropna()

def evaluate(returns):
    equity = (1 + returns).cumprod()
    total = (equity.iloc[-1] - 1) * 100

    # 包括起始净值1.0，计算回撤
    peak = equity.cummax().clip(lower=1.0)
    max_dd = (equity / peak - 1).min() * 100

    return total, max_dd

periods = {
    "2021-2023": results.loc["2021":"2023"],
    "2024-2026": results.loc["2024":"2026"]
}

for period_name, subset in periods.items():
    print(f"\n=== {period_name} ===")
    print(f"{'Strategy':<15}{'Return':>12}{'Max DD':>12}")

    for name in subset.columns:
        total, dd = evaluate(subset[name])

        print(
            f"{name:<15}"
            f"{total:>11.2f}%"
            f"{dd:>11.2f}%"
        )



# === Phase 6: Trading Behavior ===

print("\n=== STRATEGY BEHAVIOR ===")

for short, long in [(10, 30), (20, 50), (50, 200)]:

    ma_short = prices["Close"].rolling(short).mean()
    ma_long = prices["Close"].rolling(long).mean()

    signal = (ma_short > ma_long).astype(int)

    position = (
        signal.shift(1)
        .loc[results.index]
        .fillna(0)
        .astype(int)
    )

    # Assume zero shares at the start of the test
    changes = position.diff().fillna(position.iloc[0])

    buys = (changes == 1).sum()
    sells = (changes == -1).sum()
    exposure = position.mean() * 100

    returns = results[f"MA{short}/{long}"]

    volatility = returns.std()
    sharpe = (
        returns.mean() / volatility * (252 ** 0.5)
        if volatility > 0 else float("nan")
    )

    print(f"\nMA{short}/{long}")
    print(f"Exposure: {exposure:.2f}%")
    print(f"Buy signals: {buys}")
    print(f"Sell signals: {sells}")
    print(f"Sharpe: {sharpe:.2f}")
