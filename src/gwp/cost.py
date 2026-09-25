"""Token counts and dollar cost per model call, from a dated price table in the repository."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from importlib import resources


def load_prices() -> dict:
    return json.loads(resources.files("gwp").joinpath("data/prices.json").read_text())


PRICES = load_prices()

# The research note's estimate for one live run with the default models (Haiku 4.5 reads a 5,000-token document
# and writes 500 tokens; Sonnet 5 proposes in two calls of 4,000 input and 600 output tokens; no caching):
# 5,000 x $1/M + 500 x $5/M + 8,000 x $2/M + 1,200 x $10/M = about $0.036. It is an estimate, not a measurement,
# and it leaves out retries. The offline synthetic token counts are not used for this: they measure nothing.
RESEARCH_USD_PER_RUN = 0.036


def canonical_model(model_id: str) -> str:
    """Map a Bedrock id such as global.anthropic.claude-haiku-4-5-20251001-v1:0 to claude-haiku-4-5."""
    if model_id in PRICES["models"]:
        return model_id
    if model_id in PRICES["aliases"]:
        return PRICES["aliases"][model_id]
    m = re.sub(r"^(global|us|eu|apac|jp|au)\.", "", model_id)
    m = re.sub(r"^anthropic\.", "", m)
    m = re.sub(r"-\d{8}(-v\d+(:\d+)?)?$", "", m)
    m = re.sub(r"-v\d+(:\d+)?$", "", m)
    return m


def price_for(model_id: str) -> dict | None:
    return PRICES["models"].get(canonical_model(model_id))


def usage_cost_usd(model_id: str, usage: dict) -> float | None:
    """Dollars for one call's usage. None when the model is not in the price table.

    Providers differ on whether cache tokens are already inside inputTokens.
    As Strands does, if input + output == total, cache is already inside input,
    so uncached input is input minus the cache counts.
    """
    price = price_for(model_id)
    if price is None:
        return None
    inp = usage.get("inputTokens", 0)
    out = usage.get("outputTokens", 0)
    total = usage.get("totalTokens", inp + out)
    cr = usage.get("cacheReadInputTokens", 0)
    cw = usage.get("cacheWriteInputTokens", 0)
    uncached = inp - cr - cw if inp + out == total and (cr or cw) else inp
    dollars = (uncached * price["input"] + out * price["output"] + cr * price["cache_read"]
               + cw * price["cache_write"]) / 1_000_000
    return round(dollars, 8)


@dataclass
class ModelCall:
    step: str  # reader | proposer
    attempt: int
    model_id: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    latency_ms: int
    cost_usd: float | None
    status: str  # ok | timeout | throttled | invalid_output | error
    synthetic: bool  # True when the tokens come from a scripted model or an estimate
    model_turns: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


def make_call(step: str, attempt: int, model_id: str, prompt_version: str, usage: dict, latency_ms: int,
              status: str, synthetic: bool, model_turns: int = 1) -> ModelCall:
    return ModelCall(
        step=step, attempt=attempt, model_id=model_id, prompt_version=prompt_version,
        input_tokens=usage.get("inputTokens", 0), output_tokens=usage.get("outputTokens", 0),
        cache_read_tokens=usage.get("cacheReadInputTokens", 0),
        cache_write_tokens=usage.get("cacheWriteInputTokens", 0),
        latency_ms=latency_ms, cost_usd=usage_cost_usd(model_id, usage), status=status, synthetic=synthetic,
        model_turns=model_turns,
    )
