import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from carbon.api.schemas import (
    CheckRequest,
    CheckResult,
    Job,
    JobStatus,
    MatchKind,
    Mode,
    Strictness,
    TextInput,
    UrlInput,
)

EXAMPLE_RESULT = Path(__file__).parent / "fixtures" / "example_result.json"


def example() -> dict:
    return json.loads(EXAMPLE_RESULT.read_text())


# --- CheckRequest ----------------------------------------------------------------------------


def test_request_text_vs_url():
    req = CheckRequest.model_validate(
        {
            "side_a": {"type": "text", "text": "Every brand deserves a system."},
            "side_b": {"type": "url", "url": "https://novastack.io/services"},
        }
    )
    assert isinstance(req.side_a, TextInput)
    assert isinstance(req.side_b, UrlInput)
    assert req.mode is Mode.COMPARE
    assert req.include_paraphrase is True
    assert req.include_layout is False
    assert req.strictness is Strictness.BALANCED


def test_request_discover_without_side_b():
    req = CheckRequest.model_validate(
        {"side_a": {"type": "url", "url": "https://my.site/page"}, "mode": "discover"}
    )
    assert req.side_b is None


@pytest.mark.parametrize(
    "payload",
    [
        {"side_a": {"type": "text", "text": "hello"}},  # compare needs side_b
        {"side_a": {"type": "text", "text": "   "}, "mode": "discover"},
        {"side_a": {"type": "text", "text": ""}, "mode": "discover"},
        {"side_a": {"type": "url", "url": "not a url"}, "mode": "discover"},
        {"side_a": {"type": "url", "url": "ftp://x.com/a"}, "mode": "discover"},
        {"side_a": {"type": "text", "url": "https://x.com"}, "mode": "discover"},
        {"side_a": {"type": "file", "text": "x"}, "mode": "discover"},
        {"side_a": {"type": "text", "text": "x"}, "mode": "search"},
        {"side_a": {"type": "text", "text": "x"}, "mode": "discover", "strictness": "harsh"},
        {"side_a": {"type": "text", "text": "x"}, "mode": "discover", "extra": 1},
    ],
)
def test_request_rejects_invalid(payload):
    with pytest.raises(ValidationError):
        CheckRequest.model_validate(payload)


# --- Job -------------------------------------------------------------------------------------

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def test_job_queued_defaults():
    job = Job(id="j1", status=JobStatus.QUEUED, created_at=NOW)
    assert job.progress == 0
    assert job.error is None


def test_job_round_trips_json():
    job = Job(id="j1", status=JobStatus.FAILED, progress=40, error="fetch failed", created_at=NOW)
    assert Job.model_validate_json(job.model_dump_json()) == job


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "failed"},  # failed needs an error
        {"status": "comparing", "error": "boom"},  # error only when failed
        {"status": "done", "progress": 90},
        {"status": "comparing", "progress": 101},
        {"status": "paused"},
        {"status": "queued", "created_at": datetime(2026, 10, 8)},  # naive datetime
    ],
)
def test_job_rejects_invalid(fields):
    with pytest.raises(ValidationError):
        Job.model_validate({"id": "j1", "created_at": NOW} | fields)


# --- CheckResult -----------------------------------------------------------------------------


def test_example_result_validates():
    result = CheckResult.model_validate_json(EXAMPLE_RESULT.read_text())
    assert result.overall_similarity == 63
    assert [s.pct_of_a for s in result.sources] == [41, 14, 8]
    assert result.layout is not None
    assert result.side_b is not None
    assert result.side_b.url is not None


def test_example_spans_cover_their_passages():
    result = CheckResult.model_validate(example())
    passages = {p.id: p for p in result.passages}
    for span in result.side_a.spans:
        assert result.side_a.text[span.start : span.end] == passages[span.passage_id].text
    assert {s.kind for s in result.side_a.spans} == set(MatchKind)


def test_result_round_trips_json():
    result = CheckResult.model_validate(example())
    assert CheckResult.model_validate_json(result.model_dump_json()) == result


def test_result_discover_has_no_side_b_or_layout():
    data = example() | {"side_b": None, "layout": None}
    result = CheckResult.model_validate(data)
    assert result.side_b is None
    assert result.layout is None


def break_span_end(d):
    d["side_a"]["spans"][0]["end"] = len(d["side_a"]["text"]) + 1


def overlap_spans(d):
    d["side_a"]["spans"][1]["start"] = d["side_a"]["spans"][0]["end"] - 1


def empty_span(d):
    d["side_a"]["spans"][0]["end"] = d["side_a"]["spans"][0]["start"]


def unknown_passage(d):
    d["side_b"]["spans"][0]["passage_id"] = "nope"


def mismatched_kind(d):
    d["side_a"]["spans"][0]["kind"] = "paraphrased"


def unknown_source(d):
    d["passages"][0]["source_id"] = "nope"


def duplicate_passage(d):
    d["passages"][1]["id"] = d["passages"][0]["id"]


def oversized_share(d):
    d["sources"][0]["pct_of_a"] = 90


def bad_similarity(d):
    d["passages"][0]["similarity"] = 120


def missing_layout_field(d):
    del d["layout"]["palette"]


@pytest.mark.parametrize(
    "mutate",
    [
        break_span_end,
        overlap_spans,
        empty_span,
        unknown_passage,
        mismatched_kind,
        unknown_source,
        duplicate_passage,
        oversized_share,
        bad_similarity,
        missing_layout_field,
    ],
)
def test_result_rejects_inconsistent(mutate):
    data = example()
    mutate(data)
    with pytest.raises(ValidationError):
        CheckResult.model_validate(data)
