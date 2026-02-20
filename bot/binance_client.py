import os
import ccxt

def make_exchange():
    ex = ccxt.binance({
        "apiKey": os.environ["BINANCE_API_KEY"],
        "secret": os.environ["BINANCE_API_SECRET"],
        "enableRateLimit": True,
        "options": {
            "defaultType": "spot",
        },
    })
    return ex
