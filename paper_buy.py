
import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

load_dotenv()

client = TradingClient(
    os.environ["ALPACA_KEY"],
    os.environ["ALPACA_SECRET"],
    paper=True
)

order = MarketOrderRequest(
    symbol="SPY",
    qty=1,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.DAY
)

confirm = input("输入 YES 提交模拟买单：")

if confirm == "YES":
    result = client.submit_order(order_data=order)
    print("Order ID:", result.id)
    print("Status:", result.status)
else:
    print("订单已取消")
