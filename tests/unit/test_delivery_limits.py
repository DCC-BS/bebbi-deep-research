"""Deliveries stay inside the limits of the Open WebUI delivery route (dr_delivery.py), which refuses more for good."""

import json

import httpx

from bebbi_deep_research.services.report import (
    DELIVERY_MAX_BODY,
    DELIVERY_MAX_CONTENT,
    DELIVERY_MAX_SOURCE_BYTES,
    DELIVERY_MAX_SOURCES,
    build_sources,
    fit_delivery,
    limit_sources,
    owui_source_events,
    ui_markdown,
)

from . import conftest
from .conftest import DELIVERY, running, submit, utc_window, wait_status


def route_accepts(body: dict) -> bool:
    """The checks of kdkp-open-web-ui image/site-packages/dr_delivery.py (_check_payload and the body cap)."""
    sources = body.get("sources") or []
    return (
        len(json.dumps(body).encode()) <= DELIVERY_MAX_BODY
        and len(body["content"]) <= DELIVERY_MAX_CONTENT
        and len(sources) <= DELIVERY_MAX_SOURCES
        and all("type" not in s and len(json.dumps(s)) <= DELIVERY_MAX_SOURCE_BYTES for s in sources)
    )


def report_with(n: int, extra_cites: dict[int, int] | None = None) -> str:
    """A report citing sources 1..n once each in order, plus `extra_cites` {source: more citations}."""
    sentences = [f"Aussage {i} [{i}]." for i in range(1, n + 1)]
    sentences += [f"Wieder {i} [{i}]." for i, k in (extra_cites or {}).items() for _ in range(k)]
    lines = [f"[{i}] Quelle {i}. https://example.ch/{i} (abgerufen am 2026-10-25)" for i in range(1, n + 1)]
    return (
        "# Bericht\n\n## Teil\n\n" + " ".join(sentences) + " Jahr [2024].\n\n## Quellen\n\n" + "\n".join(lines) + "\n"
    )


def test_most_cited_sources_are_kept_and_numbered_again():
    report = report_with(205, {203: 3, 205: 2})
    text, sources = limit_sources(report, build_sources(report))
    assert len(sources) == 200 and [s["n"] for s in sources] == list(range(1, 201))
    urls = [s["url"] for s in sources]
    assert "https://example.ch/203" in urls and "https://example.ch/205" in urls  # cited more often
    assert "https://example.ch/200" not in urls  # among the least cited, highest numbers go first
    md = ui_markdown(text, sources)
    assert "Aussage 203 [" + str(urls.index("https://example.ch/203") + 1) + "]." in md
    assert "Aussage 200." in md  # the dropped citation is gone, the sentence stays
    assert "\\[2024\\]" in md and "Die vollständige Quellenliste enthält die Word-Datei" in md
    assert md.count("https://example.ch/") == 200


def test_reports_within_the_limit_are_unchanged():
    report = report_with(3)
    sources = build_sources(report)
    assert limit_sources(report, sources) == (report, sources)


def test_too_long_text_and_too_large_sources_are_cut():
    big = {"source": {"name": "x" * 1000}, "document": ["y" * 300_000], "metadata": [{"source": "s", "name": "n"}]}
    envelope = {"chat_id": "c", "message_id": "m", "job_id": "j", "user_id": "u"}
    content, sources = fit_delivery("ä" * 3_000_000, [big] * 250, envelope)
    assert route_accepts({**envelope, "content": content, "sources": sources})
    assert content.endswith("Den vollständigen Bericht enthält die Word-Datei.*") and len(sources) <= 200
    small, same = fit_delivery("kurz", [{"document": ["d"]}], envelope)
    assert (small, same) == ("kurz", [{"document": ["d"]}])


async def test_a_report_with_too_many_sources_is_delivered(tmp_path, monkeypatch):
    monkeypatch.setattr(conftest, "REPORT", report_with(230))
    monkeypatch.setattr(conftest, "LEDGER", {})
    posted = []

    def handler(request):
        body = json.loads(request.content)
        posted.append(body)
        return httpx.Response(200 if route_accepts(body) else 400, json={})

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: E731
    async with running(tmp_path, http_factory=factory, night_window=utc_window(True), **DELIVERY) as (c, _):
        job = await submit(c, chat_id="c1", message_id="m1")
        await wait_status(c, job["job_id"], {"done"})
        for _ in range(100):
            j = (await c.get(f"/jobs/{job['job_id']}")).json()
            if j["delivery"]:
                break
            await conftest.asyncio.sleep(0.02)
        assert j["delivery"] == "ok" and len(posted[0]["sources"]) == 200
        assert len(j["sources"]) == 230  # the API and the Word file keep all of them
        assert len(owui_source_events(j["sources"])) == 230
