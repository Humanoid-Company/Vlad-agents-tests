"""Web search (tool `web_search`, from Assistant-agent): the Tavily client, its limits, and how the
tool answers the voice model — mocked, no network."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import httpx

import tools.web_search_tool as web_search_tool
from integrations.web_search import (
    HARD_MAX_RESULTS,
    FakeWebSearchProvider,
    TavilyWebSearchProvider,
    WebSearchHit,
    WebSearchRateLimiter,
    WebSearchResult,
    clamp_max_results,
    normalize_query,
    search_web,
)
from tools.executor import ToolExecutionContext
from tools.live_schemas import LIVE_BACKEND_TOOLS
from voice.conversation import REMEMBERED_TOOL_PREFIXES


def test_clamp_max_results():
    assert clamp_max_results(None) == 5
    assert clamp_max_results(3) == 3
    assert clamp_max_results(50) == HARD_MAX_RESULTS
    assert clamp_max_results(0) == 1
    assert clamp_max_results("nope") == 5


def test_normalize_query_and_empty():
    assert normalize_query("  hello   world ") == "hello world"
    result = search_web("", provider=FakeWebSearchProvider())
    assert result.error == "empty_query"
    assert result.results == []


def test_normal_search_returns_structured_hits():
    provider = FakeWebSearchProvider(
        [
            WebSearchHit(
                title="OpenAI news",
                url="https://example.com/a",
                snippet="Something new",
                source="example.com",
                published_at="2026-09-30",
            )
        ]
    )
    result = search_web(
        "OpenAI latest news",
        max_results=5,
        recency_days=1,
        provider=provider,
        rate_limiter=WebSearchRateLimiter(max_per_turn=10),
    )
    assert result.error is None
    assert result.query == "OpenAI latest news"
    assert len(result.results) == 1
    assert result.to_dict()["results"][0]["url"] == "https://example.com/a"
    assert provider.calls[0]["recency_days"] == 1
    assert provider.calls[0]["max_results"] == 5


def test_max_results_clamped_above_hard_max():
    provider = FakeWebSearchProvider(
        [WebSearchHit(title=f"t{i}", url=f"https://x/{i}", snippet="s") for i in range(20)]
    )
    result = search_web(
        "q",
        max_results=99,
        provider=provider,
        rate_limiter=WebSearchRateLimiter(max_per_turn=10),
    )
    assert provider.calls[0]["max_results"] == HARD_MAX_RESULTS
    assert len(result.results) == HARD_MAX_RESULTS


def test_timeout_and_provider_error():
    timeout_p = FakeWebSearchProvider()
    timeout_p.fail_with = "timeout"
    r1 = search_web("q", provider=timeout_p, rate_limiter=WebSearchRateLimiter(max_per_turn=10))
    assert r1.error == "web_search_timeout"
    assert r1.results == []

    bad = FakeWebSearchProvider()
    bad.fail_with = "unavailable"
    r2 = search_web("q", provider=bad, rate_limiter=WebSearchRateLimiter(max_per_turn=10))
    assert r2.error == "web_search_unavailable"


def test_empty_provider_results():
    result = search_web(
        "obscure",
        provider=FakeWebSearchProvider([]),
        rate_limiter=WebSearchRateLimiter(max_per_turn=10),
    )
    assert result.error is None
    assert result.results == []


def test_rate_limit_per_delegation():
    limiter = WebSearchRateLimiter(max_per_turn=2)
    provider = FakeWebSearchProvider([WebSearchHit("t", "https://x", "s")])
    assert search_web("a", provider=provider, rate_limiter=limiter, delegation_id="d1").error is None
    assert search_web("b", provider=provider, rate_limiter=limiter, delegation_id="d1").error is None
    limited = search_web("c", provider=provider, rate_limiter=limiter, delegation_id="d1")
    assert limited.error == "web_search_rate_limited"
    # New delegation resets.
    assert search_web("d", provider=provider, rate_limiter=limiter, delegation_id="d2").error is None


def test_tavily_parsing_and_http_errors():
    provider = TavilyWebSearchProvider("fake-key")

    class Resp:
        status_code = 200

        def json(self):
            return {
                "results": [
                    {
                        "title": "FastAPI",
                        "url": "https://fastapi.tiangolo.com/",
                        "content": "Latest docs",
                        "published_date": "2026-01-01",
                    }
                ]
            }

    with patch("integrations.web_search.httpx.Client") as client_cls:
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.post.return_value = Resp()
        client_cls.return_value = client
        result = provider.search("FastAPI", max_results=5, recency_days=7, timeout_s=5)
    assert result.error is None
    assert result.results[0].title == "FastAPI"
    assert result.results[0].source == "fastapi.tiangolo.com"
    assert result.results[0].published_at == "2026-01-01"

    with patch("integrations.web_search.httpx.Client") as client_cls:
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.post.side_effect = httpx.TimeoutException("slow")
        client_cls.return_value = client
        timed = provider.search("x", max_results=3, recency_days=None, timeout_s=1)
    assert timed.error == "web_search_timeout"

    with patch("integrations.web_search.httpx.Client") as client_cls:
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False

        class Bad:
            status_code = 503

            def json(self):
                return {}

        client.post.return_value = Bad()
        client_cls.return_value = client
        unavailable = provider.search("x", max_results=3, recency_days=None, timeout_s=1)
    assert unavailable.error == "web_search_unavailable"


def test_missing_api_key_without_provider():
    result = search_web(
        "q",
        api_key="",
        rate_limiter=WebSearchRateLimiter(max_per_turn=10),
    )
    assert result.error == "web_search_unavailable"


def test_live_schema_includes_web_search():
    tool = next(t for t in LIVE_BACKEND_TOOLS if t["name"] == "web_search")
    props = tool["parameters"]["properties"]
    assert {"query", "max_results", "recency_days"} <= set(props)
    assert tool["parameters"]["required"] == ["query"]


def test_web_and_desktop_register_web_search_off_the_event_loop():
    import server.web_users as web_users
    from assistant import Assistant

    user = web_users.WebUser(client_id="web-search-0123456789ab")
    assert user.executor.is_offloaded("web_search")
    desktop = Assistant.__new__(Assistant)
    desktop._web_search_limiter = WebSearchRateLimiter(max_per_turn=3)
    for name in ("_live_set_name", "_live_set_voice_style", "_live_change_language",
                 "_live_end_conversation"):
        setattr(desktop, name, lambda args, ctx: None)
    assert Assistant._build_tool_executor(desktop).is_offloaded("web_search")


def test_tool_tells_the_model_the_answer_and_keeps_it_in_memory():
    hits = [WebSearchHit("Курс НБУ", "https://bank.gov.ua", "Офіційний курс: 41,2 грн за долар", source="bank.gov.ua")]
    with patch.object(web_search_tool, "search_web", return_value=WebSearchResult(query="курс долара", results=hits)):
        found = web_search_tool.web_search_tool_result({"query": "курс долара"})
    assert found.ok and "41,2 грн" in found.message and "URL не зачитуй" in found.message
    assert "web_search".startswith(REMEMBERED_TOOL_PREFIXES)  # a new call after a pause still knows it

    down = WebSearchResult(query="x", results=[], error="web_search_unavailable")
    with patch.object(web_search_tool, "search_web", return_value=down):
        failed = web_search_tool.web_search_tool_result({"query": "x"})
    assert not failed.ok and "недоступний" in failed.message


def test_web_user_search_is_limited_per_request():
    import server.web_users as web_users

    user = web_users.WebUser(client_id="web-search-limit-0123456789")
    provider = FakeWebSearchProvider([WebSearchHit("T", "https://example.com", "s")])
    real = search_web

    def fake(query, **kwargs):
        kwargs.pop("api_key", None)
        return real(query, provider=provider, **kwargs)

    ctx = ToolExecutionContext(session_id="s", delegation_id="d1")
    with patch.object(web_search_tool, "search_web", side_effect=fake):
        results = [asyncio.run(user.executor.execute("web_search", {"query": f"q{i}"}, ctx)) for i in range(3)]
    assert [r.ok for r in results] == [True, True, False]  # WEB_SEARCH_MAX_CALLS_PER_TURN = 2
    assert results[-1].status == "rate_limited"


def test_backend_answers_a_search_short_and_plain():
    """She starts speaking only once the backend has written its whole answer: long answers with markdown
    (bold figures, lists of news) and a second search one after another kept her silent. Measured on the
    real backend with Tavily: 12/12 answers right, 4.8 s median instead of 6.0 s, one search each."""
    from prompts.backend_prompt import BACKEND_PROMPT

    assert "At most two searches per question" in BACKEND_PROMPT and "both queries at once" in BACKEND_PROMPT
    assert "one to three spoken sentences" in BACKEND_PROMPT and "no markdown, no bold" in BACKEND_PROMPT


def test_backend_answers_known_facts_without_searching():
    """Feedback: «Хто такий Павло Скоропадський?» went to the web (and found his palace). Biographies,
    history, science and definitions are answered from knowledge; the query names the asked-about person."""
    from prompts.backend_prompt import BACKEND_PROMPT

    assert "companies, people," not in BACKEND_PROMPT  # «people» as a search topic sent biographies to the web
    assert "famous people's lives" in BACKEND_PROMPT and "without searching" in BACKEND_PROMPT
    assert "a person → their full name, not a place or thing named after them" in BACKEND_PROMPT
