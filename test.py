import os

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

# Read the custom OpenAI-compatible endpoint from env, or override here.
# README example: http://localhost:8000/v1 for vLLM or http://localhost:1234/v1 for LM Studio
custom_backend_url = os.getenv("TRADINGAGENTS_LLM_BACKEND_URL", "http://localhost:8000/v1")
custom_agent = os.getenv("TRADINGAGENTS_LLM_AGENT", "auto")  # optional, if your endpoint requires it

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai_compatible"
config["backend_url"] = custom_backend_url
config["deep_think_llm"] = custom_agent      # whatever your endpoint serves
config["quick_think_llm"] = custom_agent     # whatever your endpoint serves
config["temperature"] = 0.0

print(f"Using custom OpenAI-compatible endpoint: {custom_backend_url}")
print("Provider:", config["llm_provider"])
print("Deep model:", config["deep_think_llm"])
print("Quick model:", config["quick_think_llm"])

# If your local endpoint requires an API key, set OPENAI_COMPATIBLE_API_KEY before running.
ta = TradingAgentsGraph(debug=True, config=config)
_, decision = ta.propagate("AAPL", "2024-11-01")
print(decision)
