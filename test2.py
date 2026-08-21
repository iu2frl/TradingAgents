import os
from datetime import date
from dotenv import load_dotenv

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

load_dotenv()  # Load environment variables from .env file

def analyze_stock(symbol: str):
    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = "openai_compatible"
    config["backend_url"] = os.getenv(
        "TRADINGAGENTS_LLM_BACKEND_URL",
        "http://localhost:8000/v1",
    )
    config["deep_think_llm"] = os.getenv("TRADINGAGENTS_LLM_MODEL", "my-model")
    config["quick_think_llm"] = os.getenv("TRADINGAGENTS_LLM_MODEL", "my-model")
    config["temperature"] = 0.0

    ta = TradingAgentsGraph(debug=False, config=config)

    # Use today as the analysis date, or a fixed date for backtesting/reproducibility
    trade_date = date.today().strftime("%Y-%m-%d")
    _, decision = ta.propagate(symbol, trade_date)

    print(f"Ticker: {symbol}")
    print(f"Decision: {decision}")

    if decision in {"Buy", "Overweight"}:
        action = "BUY"
    elif decision in {"Sell", "Underweight"}:
        action = "SELL"
    else:
        action = "HOLD"

    return action, decision


if __name__ == "__main__":
    symbol = input("Enter stock ticker: ").strip().upper()
    action, decision = analyze_stock(symbol)
    print(f"Action now: {action}")
    print(f"Detailed signal: {decision}")