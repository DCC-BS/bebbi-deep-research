"""The LLM layer on backend-common's pydantic-ai agents: scripted models, and real OpenAI-shaped HTTP exchanges."""

import json

import httpx
import pytest
from pydantic_ai import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from bebbi_deep_research.engine.llm import LLM, SKIPPED, LlmSettings, RoleAgent, ToolBudget, budgeted_tools, parse_json

from .scripted_llm import ScriptedLLM, say


def completion(content, finish="stop"):
    """An OpenAI chat completion as the endpoint returns it."""
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def llm_over_http(monkeypatch, replies, **settings):
    """LLM whose role agents talk to a fake OpenAI endpoint; returns (llm, request bodies)."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=replies.pop(0))

    monkeypatch.setattr(
        RoleAgent, "_build_http_client", lambda self: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return LLM(LlmSettings(base_url="http://llm/v1", **settings)), seen


async def test_text_request_has_model_budget_and_thinking_flag(monkeypatch):
    llm, seen = llm_over_http(monkeypatch, [completion("  Hallo Strasse  "), completion("ok")], api_key="")
    assert await llm.text("fast", "sys", "user", step="s") == "Hallo Strasse"
    body = seen[0]
    assert body["model"] == "gemma-fast" and body["max_completion_tokens"] == 4000  # pydantic-ai: the newer OpenAI name
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert llm.usage.calls == 1 and llm.usage.total == 15 and llm.usage.by_step == {"s": 15}
    await llm.text("think", "sys", "user", step="t")
    assert seen[1]["model"] == "gemma-think" and seen[1]["chat_template_kwargs"] == {"enable_thinking": True}


async def test_extra_body_per_role_is_sent(monkeypatch):
    extra = {"think": {"chat_template_kwargs": {"enable_thinking": False}, "top_k": 20}}
    llm, seen = llm_over_http(monkeypatch, [completion("a")], extra_body=extra)
    await llm.text("think", "s", "u", step="t")
    assert seen[0]["top_k"] == 20 and seen[0]["chat_template_kwargs"] == {"enable_thinking": False}


async def test_eszett_is_replaced_in_answers(monkeypatch):
    llm, _ = llm_over_http(monkeypatch, [completion("Die Strasse ist groß.")])
    assert await llm.text("fast", "s", "u", step="s") == "Die Strasse ist gross."


async def test_empty_answer_is_retried_with_twice_the_budget():
    budgets = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        budgets.append((info.model_settings or {}).get("max_tokens"))
        return ModelResponse(parts=[TextPart("" if len(budgets) < 3 else "Antwort")])

    llm = LLM(LlmSettings(base_url="http://llm/v1"), model=FunctionModel(respond))
    assert await llm.text("fast", "s", "u", step="s", max_tokens=100) == "Antwort"
    assert budgets[0] == 100 and budgets[-1] == 200


async def test_json_retries_once_and_falls_back_to_default():
    llm = ScriptedLLM([say("kein json"), say('```json\n{"a": 1}\n```')])
    assert await llm.json("fast", "s", "u", step="j") == {"a": 1}
    assert "Return ONLY the JSON object" in llm.calls[1]["user"]
    llm = ScriptedLLM([say("nein"), say("auch nicht")])
    assert await llm.json("fast", "s", "u", step="j", default={}) == {}


async def test_json_ignores_a_bare_list_answer():
    llm = ScriptedLLM([say("[1, 2, 3]"), say('{"questions": []}')])
    assert await llm.json("fast", "s", "u", step="j") == {"questions": []}


async def test_tool_loop_skips_calls_over_budget_and_asks_for_the_final_answer():
    ran = []

    async def dispatch(name, args):
        ran.append(args["q"])
        return f"result {args['q']}"

    spec = {
        "type": "function",
        "function": {
            "name": "search",
            "description": "search",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        },
    }
    llm = ScriptedLLM(
        [say(calls=[("search", {"q": "a"}), ("search", {"q": "b"}), ("search", {"q": "c"})]), say("Ende")]
    )
    budget = ToolBudget(max_calls=2)
    out = await llm.run_tools(
        "think", "SYS", "Frage", budgeted_tools([spec], dispatch, budget), "focus", budget=budget, final_prompt="FINAL"
    )
    assert out == "Ende" and sorted(ran) == ["a", "b"]
    assert llm.calls[0]["tools"] == ["search"] and llm.calls[1]["tools"] == []  # tools hidden once the budget is used
    assert not llm.calls[0]["instructions"].endswith("FINAL") and llm.calls[1]["instructions"].endswith("FINAL")
    returns = [
        p.content for m in llm.calls[1]["messages"] for p in getattr(m, "parts", []) if p.part_kind == "tool-return"
    ]
    assert SKIPPED in returns and llm.usage.by_step["focus"] > 0


async def test_tool_loop_without_any_answer_raises():
    llm = ScriptedLLM([say(""), say(""), say(""), say(""), say("")])
    with pytest.raises(UnexpectedModelBehavior):
        await llm.run_tools("think", "s", "u", [], "focus", budget=ToolBudget(max_calls=0))


def test_parse_json_prefers_the_object_when_a_dict_is_wanted():
    text = 'Hier [Hinweis] ist {"a": 1}'
    assert parse_json(text, want=dict) == {"a": 1}
    assert parse_json("[1, 2]", want=dict) is None
    assert parse_json("[1, 2]") == [1, 2]
    assert parse_json('{"a": 1,}', want=dict) == {"a": 1}


async def test_embeddings_use_their_own_endpoint_when_set():
    """A gateway serves chat and embeddings under different paths (KDKP: /kis/gemma4/v1 and /kis/embeddings/v1)."""
    same = LLM(LlmSettings(base_url="http://llm/v1", api_key="k"))
    assert str(same._embed_client.base_url) == "http://llm/v1/" and same._embed_client.api_key == "k"
    own = LLM(LlmSettings(base_url="http://llm/v1", api_key="k", embed_base_url="http://emb/v1", embed_api_key="e"))
    assert str(own._embed_client.base_url) == "http://emb/v1/" and own._embed_client.api_key == "e"
    await same.aclose()
    await own.aclose()
