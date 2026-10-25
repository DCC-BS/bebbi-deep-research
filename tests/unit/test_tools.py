"""Tools (Serper / Firecrawl mapping, fetch restrictions) and MCPClient against httpx.MockTransport."""

import json

import httpx
import pytest

from bebbi_deep_research.engine import metrics
from bebbi_deep_research.engine import tools as T
from bebbi_deep_research.engine.tools import MCPClient, Tools, fetchable

REAL_CLIENT = httpx.Client


def make_tools(handler):
    t = Tools("http://s/search", "skey", "http://fc", "fkey")
    t.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return t


async def test_search_maps_serper_results_and_sends_key_and_locale():
    seen = []

    def handler(req):
        seen.append((req.headers["X-API-KEY"], json.loads(req.content)))
        return httpx.Response(
            200,
            json={
                "organic": [
                    {"title": "A", "link": "https://a.ch/x", "snippet": "s", "date": "2024"},
                    {"title": "ohne Link"},
                ]
            },
        )

    res = await make_tools(handler).search("frage", "fr", num=3)
    assert res == [{"title": "A", "url": "https://a.ch/x", "snippet": "s", "date": "2024"}]
    assert seen == [("skey", {"q": "frage", "num": 3, "gl": "ch", "hl": "fr"})]


async def test_search_returns_nothing_on_errors_and_bad_json():
    assert await make_tools(lambda r: httpx.Response(200, text="<html>")).search("q") == []

    def boom(req):
        raise httpx.ConnectError("down")

    assert await make_tools(boom).search("q") == []


class LogRecorder:
    """Stands in for a structlog logger: records (level, event, fields)."""

    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda event, **fields: self.lines.append((level, event, fields))


@pytest.fixture
def logs(monkeypatch):
    rec = LogRecorder()
    monkeypatch.setattr(T, "logger", rec)
    return rec


@pytest.mark.parametrize(("status", "level"), [(401, "error"), (403, "error"), (429, "warning"), (503, "warning")])
async def test_search_http_errors_are_counted_and_logged_with_the_host_only(logs, status, level):
    t = make_tools(lambda r: httpx.Response(status, json={"message": "quota"}))
    assert await t.search("geheime frage") == []
    assert t.errors == {f"search:{status}": 1}
    assert metrics.value("research_tool_errors_total", tool="search", reason=str(status)) >= 1
    assert logs.lines == [(level, "search failed", {"tool": "search", "reason": str(status), "host": "s"})]
    assert "geheime" not in repr(logs.lines)


async def test_search_transport_errors_and_bad_json_are_counted(logs):
    def boom(req):
        raise httpx.ReadTimeout("slow")

    t = make_tools(boom)
    await t.search("q")
    t.http = make_tools(lambda r: httpx.Response(200, text="<html>")).http
    await t.search("q")
    assert t.errors == {"search:ReadTimeout": 1, "search:bad_json": 1}
    assert [line[0] for line in logs.lines] == ["warning", "warning"]


async def test_fetch_errors_are_counted_and_logged_with_the_page_host_only(logs):
    t = make_tools(lambda r: httpx.Response(500, json={"success": False}))
    assert await t.fetch_meta("https://www.bs.ch/private/path?x=1") == ("", "", "")
    assert t.errors == {"fetch:500": 1}
    assert logs.lines == [("warning", "fetch failed", {"tool": "fetch", "reason": "500", "host": "www.bs.ch"})]


async def test_successful_calls_count_no_errors(logs):
    t = make_tools(lambda r: httpx.Response(200, json={"organic": [], "data": {"markdown": "x"}}))
    await t.search("q")
    await t.fetch_meta("https://a.ch/")
    assert t.errors == {} and logs.lines == []


async def test_fetch_meta_maps_firecrawl_title_text_and_date():
    seen = []

    def handler(req):
        seen.append((req.url.path, req.headers["Authorization"], json.loads(req.content)["url"]))
        return httpx.Response(
            200, json={"data": {"markdown": "# Text", "metadata": {"title": "Titel", "modifiedTime": "2024-05-01"}}}
        )

    t = make_tools(handler)
    assert await t.fetch_meta("https://a.ch/p") == ("Titel", "# Text", "2024-05-01")
    assert await t.fetch("https://a.ch/p") == ("Titel", "# Text")
    assert seen[0] == ("/v2/scrape", "Bearer fkey", "https://a.ch/p")


async def test_fetch_meta_survives_missing_data_and_non_json():
    assert await make_tools(lambda r: httpx.Response(200, json={"data": None})).fetch_meta("https://a.ch/") == (
        "",
        "",
        "",
    )
    assert await make_tools(lambda r: httpx.Response(502, text="bad gateway")).fetch_meta("https://a.ch/") == (
        "",
        "",
        "",
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://a.ch/x",
        "javascript:alert(1)",
        "http://localhost:8080/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://10.0.0.5/x",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://172.16.0.1/",
        "http://0.0.0.0/",
        "http://[::ffff:127.0.0.1]/",
        "http://2130706433/",
        "http://0x7f.1/",
        "http://100.64.0.1/",
        "http://foo.local/",
        "http://research-agent.ns.svc/",
        "http://x.svc.cluster.local/",
        "http://db.internal/",
        "http://intranet/",
        "http://localhost./",
        "",
        "https:///x",
    ],
)
async def test_fetch_meta_refuses_internal_targets_without_a_request(url):
    def never(req):
        raise AssertionError("request must not be sent")

    assert not fetchable(url)
    assert await make_tools(never).fetch_meta(url) == ("", "", "")


@pytest.mark.parametrize(
    "url", ["https://www.bs.ch/x?a=1#f", "http://example.com", "https://8.8.8.8/", "https://sub.example.co.uk:8443/a"]
)
def test_public_urls_are_fetchable(url):
    assert fetchable(url)


# ------------------------------------------------------------------------------------------------ MCP
@pytest.fixture
def mcp(monkeypatch):
    monkeypatch.setattr(T.time, "sleep", lambda s: None)

    def build(handler, **kw):
        monkeypatch.setattr(T.httpx, "Client", lambda **k: REAL_CLIENT(transport=httpx.MockTransport(handler), **k))
        return MCPClient("http://mcp/", **kw)

    return build


def rpc(req):
    return json.loads(req.content)


def jsonrpc(req, result):
    return httpx.Response(
        200, json={"jsonrpc": "2.0", "id": rpc(req)["id"], "result": result}, headers={"mcp-session-id": "s1"}
    )


def standard(req, calls=None):
    body = rpc(req)
    if calls is not None:
        calls.append((body.get("method"), req.headers.get("mcp-session-id")))
    if "id" not in body:
        return httpx.Response(202)
    if body["method"] == "initialize":
        return jsonrpc(req, {"protocolVersion": "2025-06-18"})
    if body["method"] == "tools/list":
        return jsonrpc(req, {"tools": [{"name": "a"}, {"name": "b"}]})
    return jsonrpc(req, {"content": [{"type": "text", "text": "ergebnis"}]})


def test_mcp_json_flow_filters_tools_and_keeps_the_session(mcp):
    calls = []
    c = mcp(lambda r: standard(r, calls), allow=["a"])
    assert [t["name"] for t in c.tools()] == ["a"]
    assert c.call("a", {"x": 1}) == "ergebnis"
    assert (
        calls[0] == ("initialize", None)
        and calls[1][0] == "notifications/initialized"
        and calls[-1] == ("tools/call", "s1")
    )


def test_mcp_sse_response_takes_the_last_data_line(mcp):
    def handler(req):
        body = rpc(req)
        if body.get("method") == "tools/call":
            data = json.dumps(
                {"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": "sse"}]}}
            )
            return httpx.Response(
                200, text=f"event: message\ndata: {data}\n\n", headers={"content-type": "text/event-stream"}
            )
        return standard(req)

    assert mcp(handler).call("a", {"q": 1}) == "sse"


def test_mcp_sse_without_data_line_is_a_clear_error(mcp):
    def handler(req):
        if rpc(req).get("method") == "tools/list":
            return httpx.Response(200, text="event: ping\n\n", headers={"content-type": "text/event-stream"})
        return standard(req)

    with pytest.raises(RuntimeError, match="without a data line"):
        mcp(handler).tools()


def test_mcp_retries_5xx_then_succeeds(mcp):
    n = {"fails": 2}

    def handler(req):
        if rpc(req).get("method") == "tools/call" and n["fails"]:
            n["fails"] -= 1
            return httpx.Response(503)
        return standard(req)

    assert mcp(handler).call("a", {"q": 1}) == "ergebnis" and n["fails"] == 0


def test_mcp_retries_and_error_results_are_logged_and_counted(mcp, logs):
    n = {"fails": 1}

    def handler(req):
        body = rpc(req)
        if body.get("method") == "tools/call" and n["fails"]:
            n["fails"] -= 1
            return httpx.Response(503)
        if body.get("method") == "tools/call":
            return jsonrpc(req, {"isError": True, "content": [{"type": "text", "text": "overload"}]})
        return standard(req)

    before = metrics.value("research_mcp_errors_total", host="mcp", kind="retry")
    with pytest.raises(RuntimeError):
        mcp(handler).call("a", {"q": 1})
    assert metrics.value("research_mcp_errors_total", host="mcp", kind="retry") == before + 1
    assert [(level, event) for level, event, _ in logs.lines] == [
        ("warning", "mcp request retry"),
        ("warning", "mcp tool error result"),
        ("warning", "mcp tool error result"),
    ]
    assert logs.lines[0][2] == {"host": "mcp", "method": "tools/call", "attempt": 1, "reason": "503"}


def test_mcp_gives_up_after_the_configured_attempts(mcp):
    seen = []

    def handler(req):
        seen.append(1)
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        mcp(handler, retries=2)
    assert len(seen) == 2


def test_mcp_connection_refused_raises_after_few_attempts_on_connect(mcp):
    seen = []

    def handler(req):
        seen.append(1)
        raise httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError):
        mcp(handler, retries=3)
    assert len(seen) == 3


def test_mcp_deadline_stops_the_retries_early(mcp):
    seen = []

    def handler(req):
        seen.append(1)
        raise httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError):
        mcp(handler, retries=6, deadline=T.time.monotonic() - 1)
    assert len(seen) == 1


def test_mcp_404_reopens_the_session_and_repeats_the_call(mcp):
    state = {"sessions": 0, "stale": True}
    calls = []

    def handler(req):
        body = rpc(req)
        calls.append(body.get("method"))
        if body.get("method") == "tools/call" and state["stale"]:
            state["stale"] = False
            return httpx.Response(404)
        return standard(req)

    assert mcp(handler).call("a", {"q": 1}) == "ergebnis"
    assert calls.count("initialize") == 2 and calls[-1] == "tools/call"


def test_mcp_initialized_notification_error_is_not_swallowed(mcp):
    def handler(req):
        return httpx.Response(500) if "id" not in rpc(req) else standard(req)

    with pytest.raises(httpx.HTTPStatusError):
        mcp(handler)


def test_mcp_error_result_is_retried_once_then_raised(mcp):
    n = []

    def handler(req):
        if rpc(req).get("method") == "tools/call":
            n.append(1)
            return jsonrpc(req, {"isError": True, "content": [{"type": "text", "text": "overload"}]})
        return standard(req)

    with pytest.raises(RuntimeError, match="overload"):
        mcp(handler).call("a", {"q": 1})
    assert len(n) == 2


async def test_focus_embeds_only_the_head_of_a_huge_page():
    """A page of millions of characters must not be embedded in full (memory and CPU on every read)."""
    seen = []

    class Emb:
        async def embed(self, texts):
            seen.append(len(texts))
            return [[1.0, float(i % 3)] for i in range(len(texts))]

    out = await T.focus(Emb(), "x" * 2_000_000, ["q"], budget_chars=6000)
    assert seen[0] - 1 <= T.FOCUS_MAX_CHARS // (1200 - 150) + 1
    assert 0 < len(out) < 10_000
