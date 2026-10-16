
import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient

load_dotenv()

client = TradingClient(
    api_key=os.environ["ALPACA_KEY"],
    secret_key=os.environ["ALPACA_SECRET"],
    paper=True
)

account = client.get_account()

print("Account status:", account.status)
print("Account equity: $", account.equity)
print("Buying power: $", account.buying_power)
