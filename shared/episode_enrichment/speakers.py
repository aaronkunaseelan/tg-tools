"""Who speaks: the one sentence a prompt carries, and when episode metadata names a person at all.

The pilot's roster leaked into prose as a list ("Guests: A, B, C") and the model echoed it, so the
speaker reaches a prompt as one sentence and never as a list (X-104). An ordinary episode has no
roster — only its title and its source metadata — so this module also decides, conservatively, when
those name a person: a wrong speaker in the prompt is worse than no speaker, because the model would
name it.
"""
from __future__ import annotations

import re

# A name that is really a show, a channel or a part of one. Conservative on purpose.
_SHOW_WORDS = frozenset("""
    a an and at channel company corp daily episode episodes foundation group hour in inc institute
    journal ltd live magazine media monthly morning network news of on podcast podcasts post radio
    report review show shows studio studios television the this times today tonight tv university
    weekly with
""".split())
# One word of a person's name: capitalised, then lowercase letters (an apostrophe or a hyphen allowed), or
# a single initial. "Weiss:", "AI", "TBPN" and "2026" all fail.
_NAME_WORD = re.compile(r"^[A-Z](?:[a-z]+(?:[-'\u2019][A-Za-z]+)*)?$")
# Punctuation that belongs to a show's title, never to a person's name.
_TITLE_PUNCTUATION = re.compile(r"[:&,;()0-9\"/]")


def person_name(text: object) -> str | None:
    """The person's name when ``text`` is exactly one, else None.

    Two or three words, each a capitalised word or a single initial, with none of a title's punctuation
    and no show word. "Jane Doe" and "Travis K" pass; "Hard Fork", "Palmer Weiss: America", "The Daily"
    and "TBPN" do not.
    """
    candidate = " ".join(str(text or "").split())
    if not candidate or _TITLE_PUNCTUATION.search(candidate):
        return None
    words = candidate.split()
    if not 2 <= len(words) <= 3:
        return None
    for word in words:
        if not _NAME_WORD.match(word) or word.lower() in _SHOW_WORDS:
            return None
    return candidate


def speaker_entry(speaker: object) -> tuple[list[str], dict[str, str]]:
    """(names, firms) of one speaker entry: ``"Jane Doe"``, ``{"name", "firm"}`` or
    ``{"names": [...], "firms": {...}}`` (a panel, as Surveillance passes it)."""
    if isinstance(speaker, str):
        names = [speaker.strip()] if speaker.strip() else []
        return names, {}
    if not isinstance(speaker, dict):
        return [], {}
    if speaker.get("names"):
        names = [str(name).strip() for name in speaker["names"] if str(name).strip()]
    else:
        name = str(speaker.get("name") or "").strip()
        names = [name] if name else []
    firms = {str(key).strip(): str(value).strip()
             for key, value in dict(speaker.get("firms") or {}).items() if str(value).strip()}
    for name in names:
        firm = str(speaker.get("firm") or "").strip()
        if firm and name not in firms:
            firms[name] = firm
    return names, firms


def speaker_fact(names: list | tuple, firms: dict | None = None, *, empty: str = "") -> str:
    """The speakers as one fact: "The speaker is X of Y." Never a list, never a heading.

    ``empty`` is the caller's sentence for an entry whose metadata names nobody (a broadcast part knows
    its guests or knows it does not; an ordinary episode falls back to the transcript).
    """
    names = [str(name).strip() for name in names or [] if str(name).strip()]
    if not names:
        return empty
    firms = firms or {}
    parts = [f"{name} of {firms[name]}" if str(firms.get(name) or "").strip() else name for name in names]
    if len(parts) == 1:
        return f"The speaker is {parts[0]}."
    return f"The speakers are {', '.join(parts[:-1])} and {parts[-1]}."


def surname(name: object) -> str:
    """The last word of a guest's name, as the section prompt writes it."""
    words = str(name or "").split()
    return words[-1] if words else ""


def speaker_intervals(speaker: object, first_id: str, last_id: str) -> list[dict]:
    """One interval over the whole episode, in the shape ``render`` reads: [{"first","last","names","firms"}].

    An ordinary episode has one speaker set for the whole transcript; Surveillance has one interval per
    block and builds its own.
    """
    names, firms = speaker_entry(speaker)
    if not names:
        return []
    return [{"first": str(first_id), "last": str(last_id), "names": names, "firms": firms}]
