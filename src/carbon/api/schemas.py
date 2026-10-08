"""Public API contract: check requests, job status and check results.

Field names are snake_case on the wire. Percentages are integers 0-100, as the UI renders them.
Character offsets are half-open ``[start, end)`` indexes into the side's ``text``.
"""

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, model_validator

Percent = Annotated[int, Field(ge=0, le=100)]
Offset = Annotated[int, Field(ge=0)]


class Schema(BaseModel):
    # Fields with defaults are always present in responses, so mark them required there.
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


# --- Request ---------------------------------------------------------------------------------


class Mode(StrEnum):
    COMPARE = "compare"  # side A against side B
    DISCOVER = "discover"  # side A against sources found on the open web (plus side B, if given)


class Strictness(StrEnum):
    LENIENT = "lenient"
    BALANCED = "balanced"
    STRICT = "strict"


class TextInput(Schema):
    type: Literal["text"] = "text"
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def _not_blank(self) -> Self:
        if not self.text.strip():
            raise ValueError("text must not be blank")
        return self


class UrlInput(Schema):
    type: Literal["url"] = "url"
    url: HttpUrl


SideInput = Annotated[TextInput | UrlInput, Field(discriminator="type")]


class CheckRequest(Schema):
    side_a: SideInput
    side_b: SideInput | None = None
    mode: Mode = Mode.COMPARE
    include_paraphrase: bool = True
    include_layout: bool = False
    strictness: Strictness = Strictness.BALANCED

    @model_validator(mode="after")
    def _side_b_for_compare(self) -> Self:
        if self.mode is Mode.COMPARE and self.side_b is None:
            raise ValueError("side_b is required in compare mode")
        return self


# --- Job -------------------------------------------------------------------------------------


class JobStatus(StrEnum):
    QUEUED = "queued"
    FETCHING = "fetching"
    COMPARING = "comparing"
    DONE = "done"
    FAILED = "failed"


class Job(Schema):
    id: str = Field(min_length=1)
    status: JobStatus
    progress: Percent = 0
    error: str | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _error_iff_failed(self) -> Self:
        if (self.status is JobStatus.FAILED) != (self.error is not None):
            raise ValueError("error must be set exactly when status is 'failed'")
        if self.status is JobStatus.DONE and self.progress != 100:
            raise ValueError("progress must be 100 when status is 'done'")
        return self


# --- Result ----------------------------------------------------------------------------------


class MatchKind(StrEnum):
    NEAR_IDENTICAL = "near_identical"
    PARAPHRASED = "paraphrased"


class HighlightSpan(Schema):
    start: Offset
    end: Offset
    kind: MatchKind
    passage_id: str

    @model_validator(mode="after")
    def _non_empty(self) -> Self:
        if self.end <= self.start:
            raise ValueError("span end must be greater than start")
        return self


class SideResult(Schema):
    """One side as it was compared: the extracted text the spans index into."""

    text: str
    url: HttpUrl | None = Field(None, description="Set when the side was submitted as a URL.")
    word_count: Annotated[int, Field(ge=0)]
    spans: list[HighlightSpan] = []

    @model_validator(mode="after")
    def _spans_fit_text(self) -> Self:
        prev_end = 0
        for span in sorted(self.spans, key=lambda s: s.start):
            if span.start < prev_end:
                raise ValueError("highlight spans must not overlap")
            if span.end > len(self.text):
                raise ValueError("highlight span runs past the end of the text")
            prev_end = span.end
        return self


class MatchedPassage(Schema):
    id: str = Field(min_length=1)
    kind: MatchKind
    similarity: Percent
    text: str = Field(min_length=1, description="The passage as it appears in side A.")
    source_id: str
    source_url: HttpUrl
    published_date: date | None = Field(
        None, description="When the source page was first published, if known."
    )
    edit_summary: str | None = Field(
        None, examples=["Four of nine words substituted, order preserved"]
    )


class Source(Schema):
    id: str = Field(min_length=1)
    name: str
    domain: str
    pct_of_a: Percent = Field(
        description="Share of side A assigned to this source; spans are never double counted."
    )


class LayoutScores(Schema):
    overall: Percent
    grid: Percent
    type_scale: Percent
    components: Percent
    palette: Percent


class CheckResult(Schema):
    overall_similarity: Percent = Field(description="Share of side A covered by any match.")
    side_a: SideResult
    side_b: SideResult | None = None
    passages: list[MatchedPassage]
    sources: list[Source]
    layout: LayoutScores | None = Field(
        None, description="Null when layout comparison was off or not possible."
    )

    @model_validator(mode="after")
    def _references_resolve(self) -> Self:
        passages = {p.id: p for p in self.passages}
        if len(passages) != len(self.passages):
            raise ValueError("passage ids must be unique")
        source_ids = {s.id for s in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("source ids must be unique")
        for passage in self.passages:
            if passage.source_id not in source_ids:
                raise ValueError(f"passage {passage.id!r} references unknown source")
        for side in (self.side_a, self.side_b):
            for span in side.spans if side else ():
                passage = passages.get(span.passage_id)
                if passage is None:
                    raise ValueError(f"span references unknown passage {span.passage_id!r}")
                if span.kind is not passage.kind:
                    raise ValueError(f"span kind differs from passage {passage.id!r}")
        if sum(s.pct_of_a for s in self.sources) > self.overall_similarity + len(self.sources):
            # Each pct is rounded, so allow up to one point of slack per source.
            raise ValueError("source shares exceed overall similarity")
        return self


REQUEST_MODELS: tuple[type[BaseModel], ...] = (CheckRequest,)
RESPONSE_MODELS: tuple[type[BaseModel], ...] = (Job, CheckResult)
