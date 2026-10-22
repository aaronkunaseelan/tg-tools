"""Input sources for the shared episode-enrichment pipeline."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .paragraphs import ParagraphStream, build_stream
from .stt import DEFAULT_STT_MODEL, transcribe_audio


@dataclass(frozen=True)
class LayoutSpan:
    """A byte span in the original script."""

    kind: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class ScriptLayout:
    """Lossless byte layout for a reader script."""

    script_sha256: str
    spans: tuple[LayoutSpan, ...]
    heading_spans: tuple[LayoutSpan, ...]
    paragraph_spans: tuple[LayoutSpan, ...]
    separator_spans: tuple[LayoutSpan, ...]

    @property
    def headings(self) -> tuple[LayoutSpan, ...]:
        return self.heading_spans

    @property
    def paragraphs(self) -> tuple[LayoutSpan, ...]:
        return self.paragraph_spans

    @property
    def separators(self) -> tuple[LayoutSpan, ...]:
        return self.separator_spans

    def reconstruct(self) -> bytes:
        return b"".join(span.text.encode("utf-8") for span in self.spans)

    def validate(self, script_markdown: str) -> None:
        raw = script_markdown.encode("utf-8")
        if hashlib.sha256(raw).hexdigest() != self.script_sha256:
            raise ValueError("script SHA-256 does not match its layout map")
        if not self.spans or self.spans[0].start != 0:
            raise ValueError("script layout does not start at byte zero")
        cursor = 0
        for span in self.spans:
            encoded = span.text.encode("utf-8")
            if span.start != cursor or span.end != span.start + len(encoded):
                raise ValueError("script layout contains a gap or invalid span")
            cursor = span.end
        if cursor != len(raw) or self.reconstruct() != raw:
            raise ValueError("script layout does not reconstruct the source bytes")

        def check_category(items, kind):
            if any(item.kind != kind for item in items):
                raise ValueError("script layout category has an invalid span")
            for item in items:
                if raw[item.start:item.end] != item.text.encode("utf-8"):
                    raise ValueError("script layout span text does not match source")

        check_category(self.heading_spans, "heading")
        check_category(self.separator_spans, "separator")
        check_category(self.paragraph_spans, "paragraph")
        expected_headings = tuple(item for item in self.spans if item.kind == "heading")
        expected_separators = tuple(item for item in self.spans if item.kind == "separator")
        if self.heading_spans != expected_headings or self.separator_spans != expected_separators:
            raise ValueError("script layout heading or separator data was lost")

        expected_paragraphs = []
        group = []
        for item in self.spans + (LayoutSpan("separator", "", len(raw), len(raw)),):
            if item.kind == "body":
                group.append(item)
            elif group:
                start, end = group[0].start, group[-1].end
                expected_paragraphs.append((start, end))
                group = []
        actual_paragraphs = [(item.start, item.end) for item in self.paragraph_spans]
        if actual_paragraphs != expected_paragraphs:
            raise ValueError("script layout paragraph data was lost or reordered")


@runtime_checkable
class TranscriptSource(Protocol):
    kind: str
    content_origin: str
    topic_origin: str
    summary_origin: str

    def transcript(self) -> str:
        """Return source text. Only an audio source may perform STT here."""


@dataclass(frozen=True)
class AudioSttSource:
    audio_path: str | Path
    stt_model: str = DEFAULT_STT_MODEL
    jobs: int = 4

    kind: str = "audio"
    content_origin: str = "stt_transcript"
    topic_origin: str = "luna_topics"
    summary_origin: str = "luna_summary"
    has_preknown_topics: bool = False

    def transcript(self) -> str:
        return transcribe_audio(self.audio_path, model=self.stt_model, jobs=self.jobs)


@dataclass(frozen=True)
class PlainScriptSource:
    """Canonical script text without an approved topic map.

    Ordinary TTS already has the exact text sent to speech synthesis, so it
    must never pay for STT. Topic and summary analysis remain model-derived.
    """

    script_markdown: str
    content_origin: str = "speech_script"
    kind: str = "script"
    topic_origin: str = "luna_topics"
    summary_origin: str = "luna_summary"
    has_preknown_topics: bool = False

    def transcript(self) -> str:
        if not isinstance(self.script_markdown, str) or not self.script_markdown.strip():
            raise ValueError("script must contain text")
        return self.script_markdown


_HEADING_RE = re.compile(r"^## ([^\r\n]+)$")


def _line_records(text: str):
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        start = offset
        offset += len(line.encode("utf-8"))
        yield content, line, start, offset
def _span(kind: str, text: str, start: int, end: int) -> LayoutSpan:
    return LayoutSpan(kind=kind, start=start, end=end, text=text)


class ScriptSource:
    """A contract-valid final reader script with a lossless layout map.

    The source uses the exact section grammar: each section starts with one
    ``## `` heading line and contains at least one non-empty body paragraph.
    Headings are plan topics; heading lines are deliberately not body units.
    """

    kind = "script"
    content_origin = "reader_script"
    topic_origin = "approved_reader_plan"
    summary_origin = "approved_reader_plan"
    has_preknown_topics = True

    def __init__(self, script_markdown: str, *, topics=None, plan=None,
                 source_summary: str | None = None, plan_source_summary: str | None = None,
                 speech_markdown: str | None = None):
        if not isinstance(script_markdown, str):
            raise TypeError("script_markdown must be text")
        self.script_markdown = script_markdown
        self.script_sha256 = hashlib.sha256(script_markdown.encode("utf-8")).hexdigest()
        self.layout, body_paragraphs, headings = self._parse(script_markdown)
        self.layout_map = self.layout
        self.layout.validate(script_markdown)
        self._body_paragraphs = tuple(body_paragraphs)
        self._headings = tuple(headings)
        self._speech_markdown = speech_markdown
        if speech_markdown is not None:
            speech_stream = build_stream(speech_markdown)
            if len(speech_stream) != len(self._body_paragraphs):
                raise ValueError("rendered speech paragraph count does not match reader sections")
        if plan is not None:
            if topics is None:
                topics = (plan.get("topics") if isinstance(plan, dict)
                          else list(getattr(plan, "sections", ()) or ()))
            if source_summary is None and plan_source_summary is None:
                source_summary = (plan.get("source_summary") if isinstance(plan, dict)
                                  else getattr(plan, "source_summary", None))
        self.topics = topics
        self.source_summary = (plan_source_summary if plan_source_summary is not None
                               else source_summary)
        if self.source_summary is None:
            self.source_summary = ""

    @staticmethod
    def _parse(text: str):
        records = list(_line_records(text))
        if not records:
            raise ValueError("script must contain at least one section")

        all_spans: list[LayoutSpan] = []
        headings: list[LayoutSpan] = []
        paragraphs: list[LayoutSpan] = []
        separators: list[LayoutSpan] = []
        sections: list[tuple[LayoutSpan, list[tuple[str, str, int, int]]]] = []
        current = None
        for content, raw_line, start, end in records:
            if content == "" or content.isspace():
                if current is None:
                    raise ValueError("script must start with a ## heading")
                span = _span("separator", raw_line, start, end)
                all_spans.append(span)
                separators.append(span)
                current[1].append(("", raw_line, start, end))
                continue
            match = _HEADING_RE.fullmatch(content)
            if match:
                if match.group(1).strip() != match.group(1):
                    raise ValueError("script heading has surrounding whitespace")
                heading = _span("heading", raw_line, start, end)
                all_spans.append(heading)
                headings.append(heading)
                current = [heading, []]
                sections.append((heading, current[1]))
                continue
            if current is None:
                raise ValueError("script must start with a ## heading")
            body_span = _span("body", raw_line, start, end)
            all_spans.append(body_span)
            current[1].append((content, raw_line, start, end))

        if not sections:
            raise ValueError("script must contain at least one ## heading")
        if any(not any(item[0].strip() for item in body) for _, body in sections):
            raise ValueError("each script heading must have non-empty body prose")

        # Convert consecutive non-blank body lines into canonical paragraph
        # spans. The span retains source newlines; the stream retains the
        # existing paragraph canonicalizer's strip semantics.
        for _, body in sections:
            group: list[tuple[str, str, int, int]] = []
            for item in body + [("", "", -1, -1)]:
                if item[0].strip():
                    group.append(item)
                    continue
                if group:
                    start, end = group[0][2], group[-1][3]
                    raw = text.encode("utf-8")[start:end].decode("utf-8")
                    paragraphs.append(_span("paragraph", raw, start, end))
                    group = []

        layout = ScriptLayout(
            script_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            spans=tuple(all_spans), heading_spans=tuple(headings),
            paragraph_spans=tuple(paragraphs), separator_spans=tuple(separators),
        )
        body = [span.text.strip() for span in paragraphs if span.text.strip()]
        return layout, body, [h.text.rstrip("\r\n")[3:] for h in headings]

    @property
    def body_paragraphs(self) -> tuple[str, ...]:
        return self._body_paragraphs

    def transcript(self) -> str:
        self.layout.validate(self.script_markdown)
        return (self._speech_markdown if self._speech_markdown is not None
                else "\n\n".join(self._body_paragraphs))

    def stream(self) -> ParagraphStream:
        # Re-validate immediately before deriving units, not just at init.
        self.layout.validate(self.script_markdown)
        return build_stream(self.transcript())

    def analysis(self, stream: ParagraphStream, topics=None) -> dict:
        plan = self.topics if topics is None else topics
        if plan is None:
            plan = [{"heading": heading} for heading in self._headings]
        if not isinstance(plan, list) or len(plan) != len(self._headings):
            raise ValueError("approved plan topics must match script sections")
        result = []
        cursor = 0
        for index, item in enumerate(plan):
            if isinstance(item, str):
                heading = item
                start = cursor
                end = cursor
            elif isinstance(item, dict):
                heading = str(item.get("heading") or "").strip()
                start_index = item.get("first_body_paragraph_index")
                end_index = item.get("last_body_paragraph_index")
                if start_index is not None or end_index is not None:
                    if (isinstance(start_index, bool) or isinstance(end_index, bool)
                            or not isinstance(start_index, int) or not isinstance(end_index, int)):
                        raise ValueError("approved plan paragraph indexes must be integers")
                    if not (0 <= start_index <= end_index < len(stream)):
                        raise ValueError("approved plan paragraph range is invalid")
                    start, end = stream.ids[start_index], stream.ids[end_index]
                else:
                    start = item.get("startParagraphId")
                    end = item.get("endParagraphId")
            else:
                heading = str(getattr(item, "heading", "") or "").strip()
                start = getattr(item, "startParagraphId", None)
                end = getattr(item, "endParagraphId", None)
                if not heading:
                    raise ValueError("approved plan topic is not an object")
            if not heading:
                raise ValueError("approved plan topic has an empty heading")
            if heading != self._headings[index]:
                raise ValueError("approved plan heading does not match script section")
            section = self._section_paragraph_ranges()[index]
            expected_start = stream.ids[section[0]]
            expected_end = stream.ids[section[1]]
            if start is None or end is None:
                start, end = expected_start, expected_end
            elif (str(start), str(end)) != (expected_start, expected_end):
                raise ValueError("approved plan range does not match script section")
            result.append({"heading": heading, "startParagraphId": str(start),
                           "endParagraphId": str(end)})
            pos = stream.position(str(end))
            if pos is None:
                raise ValueError("approved plan topic references an unknown paragraph")
            cursor = pos + 1
        return {"summary": self.source_summary.strip(), "topics": result}

    def _section_paragraph_ranges(self) -> list[tuple[int, int]]:
        ranges = []
        cursor = 0
        for heading in self.layout.heading_spans:
            # Paragraph spans are ordered and section membership is determined
            # by byte position, so no alternate parser can silently drop one.
            owned = [p for p in self.layout.paragraph_spans if p.start >= heading.end and
                     (heading is self.layout.heading_spans[-1] or
                      p.start < self.layout.heading_spans[self.layout.heading_spans.index(heading) + 1].start)]
            if not owned:
                raise ValueError("script section has no body paragraph")
            ranges.append((cursor, cursor + len(owned) - 1))
            cursor += len(owned)
        return ranges

    def provenance(self) -> dict:
        return {"contentOrigin": self.content_origin,
                "topicOrigin": self.topic_origin,
                "summaryOrigin": self.summary_origin,
                "scriptSha256": self.script_sha256}
