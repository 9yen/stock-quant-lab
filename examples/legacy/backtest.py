
import yfinance as yf
import pandas as pd

# 1. 下载 SPY 的历史价格
df = yf.download(
    "SPY",
    start="2021-01-01",
    end="2026-10-01",
    auto_adjust=True,
    multi_level_index=False,
    progress=False
)

if df.empty:
    raise RuntimeError("无法下载行情，请检查网络或数据源")

# 2. 计算两条均线
df["MA20"] = df["Close"].rolling(20).mean()
df["MA50"] = df["Close"].rolling(50).mean()

# 3. 收盘后的策略信号
df["signal"] = (df["MA20"] > df["MA50"]).astype(int)

# 下一天开盘才能执行昨天的信号
df["position"] = df["signal"].shift(1).fillna(0)

# 4. 计算开盘到下一开盘的收益
df["next_return"] = (
    df["Open"].shift(-1) / df["Open"] - 1
)

df = df.dropna(subset=["MA50", "next_return"]).copy()

# 5. 每次买入或卖出，假设有 0.1% 成本
turnover = df["position"].diff().abs()
turnover = turnover.fillna(df["position"])

df["strategy_return"] = (
    df["position"] * df["next_return"]
    - turnover * 0.001
)

# 6. 计算资金曲线
strategy = (1 + df["strategy_return"]).cumprod()
buy_hold = (1 + df["next_return"]).cumprod()

total_return = (strategy.iloc[-1] - 1) * 100
market_return = (buy_hold.iloc[-1] - 1) * 100
drawdown = (strategy / strategy.cummax() - 1).min() * 100

print(f"Strategy return: {total_return:.2f}%")
print(f"Buy & hold return: {market_return:.2f}%")
print(f"Maximum drawdown: {drawdown:.2f}%")



import matplotlib.pyplot as plt

# 1. 假设初始资金为 10,000 美元
initial_capital = 10000

strategy_equity = strategy * initial_capital
market_equity = buy_hold * initial_capital

# 2. 策略回撤
drawdown_series = (
    strategy / strategy.cummax() - 1
) * 100

# 3. 绘制收益曲线
plt.figure(figsize=(12, 6))

plt.plot(
    strategy_equity.index,
    strategy_equity,
    label="MA20/MA50 Strategy"
)

plt.plot(
    market_equity.index,
    market_equity,
    label="SPY Buy & Hold"
)

plt.title("Strategy vs SPY")
plt.xlabel("Date")
plt.ylabel("Portfolio Value (USD)")
plt.legend()
plt.grid(True)
plt.tight_layout()
plt.savefig("equity_curve.png", dpi=150)
plt.show()

# 4. 绘制历史回撤
plt.figure(figsize=(12, 4))

plt.plot(
    drawdown_series.index,
    drawdown_series,
    label="Strategy Drawdown"
)

plt.title("Strategy Drawdown")
plt.xlabel("Date")
plt.ylabel("Drawdown (%)")
plt.legend()
plt.grid(True)
plt.tight_layout()
plt.savefig("drawdown.png", dpi=150)
plt.show()

print("Charts saved successfully!")


# === Phase 2: Strategy Diagnostics ===

def calculate_metrics(returns):
    equity = (1 + returns).cumprod()
    annual_return = equity.iloc[-1] ** (252 / len(returns)) - 1
    volatility = returns.std() * (252 ** 0.5)

    # 假设无风险收益率为 0
    sharpe = (
        returns.mean() / returns.std() * (252 ** 0.5)
        if returns.std() > 0 else float("nan")
    )

    max_drawdown = (equity / equity.cummax() - 1).min()

    return annual_return, volatility, sharpe, max_drawdown


strategy_stats = calculate_metrics(df["strategy_return"])
market_stats = calculate_metrics(df["next_return"])

exposure = df["position"].mean() * 100
trade_count = (df["position"].diff() == 1).sum()
if df["position"].iloc[0] == 1:
    trade_count += 1

print("\n=== QUANT STRATEGY ANALYSIS ===")
print(f"Market exposure: {exposure:.2f}%")
print(f"Number of entries: {trade_count}")

print("\nMetric          Strategy      SPY")
labels = ["Annual Return", "Volatility", "Sharpe Ratio", "Max Drawdown"]

for i, label in enumerate(labels):
    a = strategy_stats[i]
    b = market_stats[i]
    if i == 2:
        print(f"{label:16} {a:>8.2f} {b:>12.2f}")
    else:
        print(f"{label:16} {a*100:>7.2f}% {b*100:>11.2f}%")




# === Phase 3: Performance Attribution ===

print("\n=== YEARLY PERFORMANCE ===")
print(f"{'Year':<8}{'Strategy':>12}{'SPY':>12}{'Exposure':>12}")

for year, group in df.groupby(df.index.year):
    s = (1 + group["strategy_return"]).prod() - 1
    b = (1 + group["next_return"]).prod() - 1
    exposure = group["position"].mean()

    print(
        f"{year:<8}"
        f"{s*100:>11.2f}%"
        f"{b*100:>11.2f}%"
        f"{exposure*100:>11.2f}%"
    )

# === Market Exposure Analysis ===

invested = df["position"] == 1
cash = ~invested

market_while_in = (
    (1 + df.loc[invested, "next_return"]).prod() - 1
)

market_while_out = (
    (1 + df.loc[cash, "next_return"]).prod() - 1
)

print("\n=== EXPOSURE ANALYSIS ===")
print("Invested days:", invested.sum())
print("Cash days:", cash.sum())

print(
    f"SPY return on invested days: "
    f"{market_while_in*100:.2f}%"
)

print(
    f"SPY return on cash days: "
    f"{market_while_out*100:.2f}%"
)

# 估算回测交易成本造成的累计收益差异
gross_return = market_while_in
net_return = (
    (1 + df["strategy_return"]).prod() - 1
)

print(
    f"Return impact of trading costs: "
    f"{(gross_return-net_return)*100:.2f} percentage points"
)




# === Phase 4: 70/30 Benchmark ===

# 70% SPY + 30% Cash
# 假设每日再平衡、现金收益率为0
# 暂不计入再平衡交易成本

mix_returns = df["next_return"] * 0.70

def evaluate(name, returns):
    equity = (1 + returns).cumprod()

    total_return = (equity.iloc[-1] - 1) * 100

    annual_return = (
        equity.iloc[-1] ** (252 / len(returns)) - 1
    ) * 100

    volatility = returns.std() * (252 ** 0.5) * 100

    sharpe = (
        returns.mean() / returns.std() * (252 ** 0.5)
        if returns.std() > 0 else float("nan")
    )

    max_dd = (
        equity / equity.cummax() - 1
    ).min() * 100

    print(f"\n=== {name} ===")
    print(f"Total Return: {total_return:.2f}%")
    print(f"Annual Return: {annual_return:.2f}%")
    print(f"Volatility: {volatility:.2f}%")
    print(f"Sharpe: {sharpe:.2f}")
    print(f"Max Drawdown: {max_dd:.2f}%")


evaluate("MA20/MA50", df["strategy_return"])
evaluate("100% SPY", df["next_return"])
evaluate("70% SPY + 30% Cash", mix_returns)



# === Phase 5: Period Stability Test ===

benchmarks = pd.DataFrame({
    "MA20/50": df["strategy_return"],
    "70/30": df["next_return"] * 0.70,
    "100% SPY": df["next_return"]
})

def period_metrics(returns):
    equity = (1 + returns).cumprod()
    total = (equity.iloc[-1] - 1) * 100
    drawdown = (equity / equity.cummax() - 1).min() * 100
    return total, drawdown

periods = [
    ("2021-2023", benchmarks.loc["2021":"2023"]),
    ("2024-2026", benchmarks.loc["2024":"2026"])
]

for period_name, data in periods:
    print(f"\n=== {period_name} ===")
    print(f"{'Strategy':<15}{'Return':>12}{'Max DD':>12}")

    for name in data.columns:
        total, dd = period_metrics(data[name])
        print(f"{name:<15}{total:>11.2f}%{dd:>11.2f}%")
