"""Runtime limits of the engine: LLM timeout, retries and the process-wide cap on concurrent LLM requests."""

import asyncio

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from bebbi_deep_research.engine.llm import LLM, LlmSettings, llm_limiter
from bebbi_deep_research.services.jobs import make_agent_factory
from bebbi_deep_research.utils.app_config import AppConfig

REQUIRED = {
    "RA_TOKEN": "secret-token",
    "HMAC_SECRET": "h",
    "RA_LLM_BASE_URL": "http://llm/v1",
    "RA_SEARCH_URL": "http://search/search",
    "RA_FIRECRAWL_URL": "http://fc",
}


def test_llm_timeout_retries_and_concurrency_come_from_the_environment():
    s = AppConfig.from_env(REQUIRED)
    assert (s.llm_timeout_seconds, s.llm_max_retries, s.llm_concurrency) == (1800, 3, 6)
    s = AppConfig.from_env(
        {**REQUIRED, "RA_LLM_TIMEOUT_SECONDS": "300", "RA_LLM_MAX_RETRIES": "0", "RA_LLM_CONCURRENCY": "2"}
    )
    assert (s.llm_timeout_seconds, s.llm_max_retries, s.llm_concurrency) == (300, 0, 2)
    for bad in ({"RA_LLM_CONCURRENCY": "0"}, {"RA_LLM_TIMEOUT_SECONDS": "0"}, {"RA_LLM_MAX_RETRIES": "-1"}):
        with pytest.raises(ValueError):
            AppConfig.from_env({**REQUIRED, **bad})


async def test_agent_factory_passes_the_limits_and_shares_one_limiter():
    s = AppConfig.from_env({**REQUIRED, "RA_LLM_TIMEOUT_SECONDS": "300", "RA_LLM_MAX_RETRIES": "1"})
    make = make_agent_factory(None, s)
    a, b = make("job1"), make("job2")
    try:
        for role in ("think", "fast"):
            cfg = a.llm.agents[role].config
            assert (cfg.llm_timeout, cfg.llm_max_retries) == (300, 1)
        assert a.llm._embed_client.timeout == 300
        assert a.llm.limiter is b.llm.limiter and a.llm.limiter.max_running == 6
    finally:
        await a.llm.aclose()
        await b.llm.aclose()
        await a.tools.http.aclose()
        await b.tools.http.aclose()


async def test_the_limiter_caps_concurrent_requests_across_llm_objects():
    """Two jobs (two LLM objects) with ten parallel calls each never have more than the limit in flight."""
    running, peak = 0, 0

    async def slow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return ModelResponse(parts=[TextPart("ok")])

    limiter = llm_limiter(3)
    llms = [LLM(LlmSettings(base_url="http://llm/v1"), model=FunctionModel(slow), limiter=limiter) for _ in range(2)]
    answers = await asyncio.gather(*(llm.text("fast", "s", f"u{i}", "t") for llm in llms for i in range(10)))
    assert answers == ["ok"] * 20
    assert peak == 3


async def test_embedding_batches_wait_for_the_limiter():
    running, peak = 0, 0

    class Embedder:
        async def embed(self, batch, input_type):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

            class R:
                embeddings = [[1.0, 0.0] for _ in batch]

            return R()

    llm = LLM(LlmSettings(base_url="http://llm/v1"), limiter=llm_limiter(2))
    llm.embedder = Embedder()  # ty: ignore[invalid-assignment]
    await asyncio.gather(*(llm.embed(["x"] * 70) for _ in range(4)))  # 4 x 3 batches
    assert peak == 2
    await llm.aclose()
