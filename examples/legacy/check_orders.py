
import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

load_dotenv()

client = TradingClient(
    os.environ["ALPACA_KEY"],
    os.environ["ALPACA_SECRET"],
    paper=True
)

# 查看美股市场是否开市
clock = client.get_clock()
print("Market open:", clock.is_open)
print("Next market open:", clock.next_open)

# 查询最近 5 笔订单
orders = client.get_orders(
    filter=GetOrdersRequest(
        status=QueryOrderStatus.ALL,
        limit=5
    )
)

print("\n=== Recent Orders ===")
for order in orders:
    print("Stock:", order.symbol)
    print("Side:", order.side.value)
    print("Status:", order.status.value)
    print("Filled quantity:", order.filled_qty)
    print("Filled price:", order.filled_avg_price)
    print("-----------------------")

# 查看已成交持仓
print("\n=== Positions ===")
positions = client.get_all_positions()

if not positions:
    print("No open positions")

for position in positions:
    print("Stock:", position.symbol)
    print("Quantity:", position.qty)
    print("Average price:", position.avg_entry_price)
