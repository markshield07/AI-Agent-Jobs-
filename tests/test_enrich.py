from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from jobagent.discovery.enrich import (
    MAX_MODEL_CHARS,
    ExtractedPosting,
    enrich_description,
    extract_css_description,
    extract_jsonld_description,
)
from jobagent.llm.backend import Completion, LLMError

URL = "https://jobs.example.com/postings/42"

# One sentence per paragraph so the text is recognisable after html_to_text; six
# paragraphs render to about 580 characters and clear the default 400-character
# gate, one paragraph does not.
SENTENCE = "We are hiring a Senior Software Engineer to build the payments platform in Python."
LONG_LINES = [f"{SENTENCE} Paragraph {i}." for i in range(6)]
LONG_HTML = "".join(f"<p>{line}</p>" for line in LONG_LINES)
SHORT_HTML = f"<p>{SENTENCE}</p>"


class FakeCompleter:
    name = "fake"

    def __init__(self, result: BaseModel | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        output: type[BaseModel],
        max_tokens: int = 16000,
        effort: str | None = None,
        cache_system: bool = False,
    ) -> Completion:
        self.calls.append({"system": system, "prompt": prompt, "output": output, "effort": effort})
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return Completion(result=self.result, input_tokens=10, output_tokens=5, backend="fake")


def client_for(body: str, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def page(body: str, head: str = "") -> str:
    return (
        "<!doctype html><html><head><title>Job</title><style>p{color:red}</style>"
        f"{head}</head><body><nav>Home | Jobs | Careers</nav>{body}"
        "<footer>Copyright Example Inc</footer></body></html>"
    )


def ld_script(payload: Any, raw: str | None = None) -> str:
    return f'<script type="application/ld+json">{raw or json.dumps(payload)}</script>'


def job_posting(description: Any = LONG_HTML, **extra: Any) -> dict[str, Any]:
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Senior Software Engineer",
        "description": description,
    }
    posting.update(extra)
    return posting


def lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line]


# ----------------------------------------------------------------- JSON-LD --


def test_jsonld_single_object_is_used_before_selectors_and_model():
    completer = FakeCompleter(ExtractedPosting(description="from the model", found=True))
    html = page(
        f'<div id="content">{LONG_HTML.replace("Python", "Rust")}</div>',
        head=ld_script(job_posting()),
    )

    text = enrich_description(URL, client=client_for(html), completer=completer)

    assert text is not None
    assert lines(text) == LONG_LINES
    assert "Rust" not in text, "JSON-LD wins over the #content container"
    assert "<p>" not in text
    assert completer.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(job_posting(), id="object"),
        pytest.param([{"@type": "Organization", "name": "Acme"}, job_posting()], id="list"),
        pytest.param(
            {"@context": "https://schema.org", "@graph": [{"@type": "WebSite"}, job_posting()]},
            id="graph",
        ),
        pytest.param({"@graph": [[{"@type": "Person"}], [job_posting()]]}, id="nested-lists"),
        pytest.param({"@type": "WebPage", "mainEntity": job_posting()}, id="main-entity"),
        pytest.param(job_posting(**{"@type": ["Thing", "JobPosting"]}), id="type-list"),
        pytest.param(job_posting(**{"@type": "https://schema.org/JobPosting"}), id="type-iri"),
    ],
)
def test_jsonld_shapes(payload):
    text = extract_jsonld_description(page("", head=ld_script(payload)))
    assert text is not None
    assert lines(text) == LONG_LINES


def test_jsonld_description_is_unescaped_and_stripped():
    html = page(
        "",
        head=ld_script(
            job_posting("&lt;p&gt;We build &amp;amp; ship.&lt;/p&gt;<ul><li>SQL</li></ul>")
        ),
    )
    assert lines(extract_jsonld_description(html) or "") == ["We build & ship.", "SQL"]


def test_jsonld_extract_has_no_length_gate():
    assert extract_jsonld_description(page("", head=ld_script(job_posting("Tiny.")))) == "Tiny."


@pytest.mark.parametrize(
    "head",
    [
        pytest.param(ld_script(None, raw="{not json at all"), id="malformed"),
        pytest.param(ld_script(None, raw=""), id="empty"),
        pytest.param(
            ld_script({"@type": "Organization", "description": LONG_HTML}), id="not-a-job"
        ),
        pytest.param(ld_script(job_posting(**{"@type": "JobPostingList"})), id="wrong-type"),
        pytest.param(ld_script({"@type": "JobPosting", "title": "x"}), id="no-description"),
        pytest.param(ld_script(job_posting(["a", "list"])), id="description-not-a-string"),
        pytest.param(ld_script(job_posting("   ")), id="blank-description"),
        pytest.param(ld_script(job_posting(42)), id="description-number"),
        pytest.param(ld_script(job_posting(None)), id="description-null"),
        pytest.param(ld_script("just a string"), id="scalar"),
        pytest.param(
            f'<script type="text/javascript">{json.dumps(job_posting())}</script>', id="js"
        ),
        pytest.param("", id="absent"),
    ],
)
def test_unusable_jsonld_is_ignored(head):
    assert extract_jsonld_description(page("", head=head)) is None


def test_malformed_jsonld_falls_through_to_the_valid_block_and_then_css():
    head = ld_script(None, raw="{broken") + ld_script(job_posting())
    assert extract_jsonld_description(page("", head=head)) is not None

    html = page(f'<div id="content">{LONG_HTML}</div>', head=ld_script(None, raw="{broken"))
    text = enrich_description(URL, client=client_for(html))
    assert text is not None and "Paragraph 5." in text


def test_jsonld_tolerates_literal_newlines_and_odd_type_attributes():
    raw = '{"@type": "JobPosting", "description": "line one\nline two"}'
    html = f'<script type="application/ld+json; charset=utf-8">{raw}</script>'
    assert extract_jsonld_description(html) == "line one\nline two"


def test_first_jobposting_with_a_description_wins():
    head = ld_script({"@type": "JobPosting", "title": "no body"}) + ld_script(
        [job_posting("First."), job_posting("Second.")]
    )
    assert extract_jsonld_description(page("", head=head)) == "First."


# --------------------------------------------------------------------- CSS --


PLATFORM_SAMPLES = {
    "greenhouse": f'<div id="content">{LONG_HTML}</div>',
    "lever": (
        '<div class="posting-page"><div class="section-wrapper">Acme</div>'
        f'<div class="section-wrapper posting">{LONG_HTML}</div></div>'
    ),
    "data-qa": f'<section data-qa="job-description">{LONG_HTML}</section>',
    "ashby-class": f'<div class="ashby-job-posting-content">{LONG_HTML}</div>',
    "ashby-hashed-class": f'<div class="_JobPostingContent_1abc2_3">{LONG_HTML}</div>',
    "indeed": f'<div id="jobDescriptionText">{LONG_HTML}</div>',
    "linkedin": f'<div class="jobs-description__content">{LONG_HTML}</div>',
    "linkedin-public": f'<div class="description__text">{LONG_HTML}</div>',
    "itemprop": f'<div itemprop="description">{LONG_HTML}</div>',
    "article": f"<article>{LONG_HTML}</article>",
    "main": f"<main>{LONG_HTML}</main>",
}


@pytest.mark.parametrize("platform", list(PLATFORM_SAMPLES))
def test_css_selector_per_platform(platform):
    completer = FakeCompleter(ExtractedPosting(description="from the model", found=True))
    html = page(PLATFORM_SAMPLES[platform])

    text = enrich_description(URL, client=client_for(html), completer=completer)

    assert text is not None
    assert lines(text) == LONG_LINES
    assert "Careers" not in text and "Copyright" not in text, "only the container's text"
    assert completer.calls == []


def test_css_extract_returns_the_container_as_plain_text():
    html = page(f'<div id="content"><h2>About</h2>{LONG_HTML}<script>track()</script></div>')
    text = extract_css_description(html, 400)
    assert text is not None
    assert text.startswith("About\n")
    assert "track()" not in text and "<" not in text


def test_css_earlier_selector_wins_over_later_ones():
    html = page(
        f'<main><div id="jobDescriptionText">{LONG_HTML}</div>'
        f"<article>{LONG_HTML.replace('Python', 'Go')}</article></main>"
    )
    text = extract_css_description(html, 400)
    assert text is not None
    assert "Python" in text and "Go" not in text, "#jobDescriptionText is tried before article/main"


def test_css_skips_short_matches_of_the_same_selector():
    html = page(
        '<div class="posting-page"><div class="section-wrapper">Acme · Remote</div>'
        f'<div class="section-wrapper">{LONG_HTML}</div></div>'
    )
    text = extract_css_description(html, 400)
    assert text is not None
    assert text.startswith(SENTENCE)


def test_css_falls_back_to_main_when_nothing_specific_matches():
    html = page(f"<main><h1>Engineer</h1>{LONG_HTML}</main>")
    text = extract_css_description(html, 400)
    assert text is not None and text.startswith("Engineer\n")


@pytest.mark.parametrize(
    "html",
    [
        pytest.param("", id="empty"),
        pytest.param(page(f"<div>{LONG_HTML}</div>"), id="no-known-container"),
        pytest.param(page(f'<div id="content">{SHORT_HTML}</div>'), id="container-too-short"),
        pytest.param(page('<div id="content"></div><main></main>'), id="containers-empty"),
    ],
)
def test_css_returns_none_when_no_container_qualifies(html):
    assert extract_css_description(html, 400) is None


# --------------------------------------------------------------- min_chars --


def test_short_jsonld_falls_through_to_css():
    html = page(f'<div id="content">{LONG_HTML}</div>', head=ld_script(job_posting(SHORT_HTML)))
    text = enrich_description(URL, client=client_for(html))
    assert text is not None and "Paragraph 5." in text


def test_short_everything_returns_none_without_a_completer():
    html = page(f'<div id="content">{SHORT_HTML}</div>', head=ld_script(job_posting(SHORT_HTML)))
    assert enrich_description(URL, client=client_for(html)) is None


def test_min_chars_is_configurable():
    html = page(f'<div id="content">{SHORT_HTML}</div>', head=ld_script(job_posting(SHORT_HTML)))
    assert enrich_description(URL, client=client_for(html), min_chars=20) == SENTENCE
    assert extract_css_description(html, 20) == SENTENCE
    assert enrich_description(URL, client=client_for(html), min_chars=len(SENTENCE)) == SENTENCE
    assert enrich_description(URL, client=client_for(html), min_chars=len(SENTENCE) + 1) is None


# ------------------------------------------------------------------- model --


def model_page() -> str:
    """A page none of the selectors match, so only the model can read it."""
    return page(f"<div class='job'><h1>Engineer</h1>{LONG_HTML}<script>init()</script></div>")


def test_model_is_only_used_when_a_completer_is_given():
    assert enrich_description(URL, client=client_for(model_page())) is None

    completer = FakeCompleter(ExtractedPosting(description=" ".join([SENTENCE] * 4), found=True))
    text = enrich_description(URL, client=client_for(model_page()), completer=completer)

    assert text == " ".join([SENTENCE] * 4)
    assert len(completer.calls) == 1
    call = completer.calls[0]
    assert call["output"] is ExtractedPosting
    assert call["effort"] == "low"
    system = call["system"].lower()
    assert "as it appears" in system
    assert "do not summarise" in system
    assert "do not add anything" in system
    assert "found=false" in system
    assert "not a job posting" in system
    assert "not present" in system
    prompt = call["prompt"]
    assert URL in prompt
    assert "Paragraph 5." in prompt
    assert "Careers" in prompt, "the whole body goes to the model, boilerplate included"
    assert "<div" not in prompt and "init()" not in prompt and "color:red" not in prompt


def test_model_prompt_is_truncated_to_the_page_text_budget():
    filler = "lorem ipsum " * (MAX_MODEL_CHARS // 12 + 200)
    html = page(f"<div>START-MARK {filler} END-MARK</div>")
    completer = FakeCompleter(ExtractedPosting(description=" ".join([SENTENCE] * 4), found=True))

    enrich_description(URL, client=client_for(html), completer=completer)

    prompt = completer.calls[0]["prompt"]
    assert "START-MARK" in prompt and "END-MARK" not in prompt
    inner = prompt.split("<page>\n", 1)[1].rsplit("\n</page>", 1)[0]
    assert len(inner) == MAX_MODEL_CHARS


def test_model_found_false_returns_none():
    completer = FakeCompleter(ExtractedPosting(description=" ".join([SENTENCE] * 4), found=False))
    assert enrich_description(URL, client=client_for(model_page()), completer=completer) is None
    assert len(completer.calls) == 1


def test_model_description_must_clear_half_the_gate():
    floor = 400 // 2
    too_short = FakeCompleter(ExtractedPosting(description="x" * (floor - 1), found=True))
    assert enrich_description(URL, client=client_for(model_page()), completer=too_short) is None

    exact = FakeCompleter(ExtractedPosting(description="  " + "x" * floor + "\n", found=True))
    assert enrich_description(URL, client=client_for(model_page()), completer=exact) == "x" * floor


def test_model_is_not_called_when_the_page_has_no_text():
    completer = FakeCompleter(ExtractedPosting(description=" ".join([SENTENCE] * 4), found=True))
    for html in ["", "<html><body></body></html>", page("<div>hi</div>")]:
        assert enrich_description(URL, client=client_for(html), completer=completer) is None
    assert completer.calls == []


def test_model_reads_the_whole_document_when_there_is_no_body():
    fragment = f"<div>{LONG_HTML}</div>"
    completer = FakeCompleter(ExtractedPosting(description=" ".join([SENTENCE] * 4), found=True))
    assert enrich_description(URL, client=client_for(fragment), completer=completer) is not None
    assert "Paragraph 5." in completer.calls[0]["prompt"]


def test_model_error_returns_none_and_logs(caplog):
    completer = FakeCompleter(error=LLMError("rate limited"))
    with caplog.at_level(logging.WARNING, logger="jobagent.discovery.enrich"):
        result = enrich_description(URL, client=client_for(model_page()), completer=completer)
    assert result is None
    assert any("rate limited" in record.getMessage() for record in caplog.records)


def test_model_result_of_the_wrong_type_is_ignored():
    class Other(BaseModel):
        description: str = "x" * 400
        found: bool = True

    completer = FakeCompleter(Other())
    assert enrich_description(URL, client=client_for(model_page()), completer=completer) is None


# -------------------------------------------------------------------- HTTP --


@pytest.mark.parametrize("status", [404, 410, 429, 500, 503])
def test_http_error_status_returns_none(caplog, status):
    completer = FakeCompleter(ExtractedPosting(description=" ".join([SENTENCE] * 4), found=True))
    html = page(f'<div id="content">{LONG_HTML}</div>')

    with caplog.at_level(logging.WARNING, logger="jobagent.discovery.enrich"):
        result = enrich_description(URL, client=client_for(html, status), completer=completer)

    assert result is None
    assert completer.calls == [], "a failed fetch never reaches the model"
    assert any(URL in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.TooManyRedirects],
)
def test_transport_errors_return_none(error):
    def handler(request: httpx.Request) -> httpx.Response:
        raise error("boom", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert enrich_description(URL, client=client) is None


def test_the_page_is_fetched_once_with_the_given_client():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=page(f'<div id="content">{LONG_HTML}</div>'))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert enrich_description(URL, client=client) is not None
    assert [str(r.url) for r in seen] == [URL]
    assert not client.is_closed, "a caller's client is left open for the next posting"
