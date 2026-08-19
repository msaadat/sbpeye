"""Short mnemonic handles that stand in for document IDs in the model's context.

A citation reaches the UI as ``[[circular:<uuid>|label]]``, and for a while the model was
asked to reproduce that uuid verbatim. Measured on the 2026-08-19 benchmark round, it does
not: 15 of 58 emitted tokens pointed at nothing. The failures were not random — every one
was a drifted copy of an id the same answer had already cited correctly
(``da13ed0e-96c5-5945-adb2-…`` came back as ``-ut``, ``-adj;``, ``-advb2-*``), or a blend of
two real ids sharing a tail. Past the second or third citation the model stops copying from
its context and starts copying from itself, so the error rate tracks how many citations an
answer makes rather than how hard the question was.

So the model never sees an id. It is given ``[[c:BPRD-CL-01-2021]]`` — short enough to
survive re-copying, and derived from the reference it is already writing into the prose,
which makes a handle pointing at the wrong document visible rather than silent. The map back
to real ids lives here, for one turn, server-side.

Two properties matter more than the shortening:

* An unknown handle can be *detected*. Before, a mangled uuid was indistinguishable from a
  real one and shipped as a dead link on an otherwise correct regulatory answer.
* With no ids in the input, any uuid in the output is fabricated by construction, so it can
  be dropped without judgement.

The frontend is unchanged: it still receives real tokens. Handles exist only between
:meth:`CitationHandles.to_handles` and :meth:`CitationHandles.expand`.
"""

import re

# Real citation tokens — what producers build and what the UI resolves. The id segment
# accepts anything but a delimiter because attachments are sometimes cited by filename, and
# the label is optional so that the shapes a model actually emits when it loses the thread
# — `[[circular:1dcf1c84]]`, `[[circular:1dc2c et al]]` — are matched and stopped here
# rather than reaching the reader as raw markup.
TOKEN_PATTERN = re.compile(
    r"\[\[(circular|attachment|law):([^\]|\n]*)(?:\|([^\]\n]*))?\]\]"
)

# Handles, as the model is asked to write them. Tolerant on purpose: stray spaces, wrong
# case, or a label the model appended anyway all still resolve, because the point is to
# recover the citation rather than to grade the model's formatting.
HANDLE_PATTERN = re.compile(
    r"\[\[\s*([cal])\s*:\s*([^\]|\n]*?)\s*(?:\|[^\]\n]*)?\]\]",
    re.IGNORECASE,
)

UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# An id the model invented takes its own punctuation with it: bracketed, the brackets go
# too; otherwise the space in front of it does, so the sentence closes up cleanly.
_BARE_UUID_PATTERN = re.compile(
    rf"[ \t]*(?:"
    rf"\(\s*{UUID_PATTERN.pattern}\s*\)"
    rf"|\[\s*{UUID_PATTERN.pattern}\s*\]"
    rf"|{UUID_PATTERN.pattern}"
    rf")"
)

KIND_PREFIX = {"circular": "c", "attachment": "a", "law": "l"}

MAX_SLUG_CHARS = 56

# SBP names nearly every circular "<DEPT> Circular Letter No. N of YYYY". Spelling that out
# leaves a handle longer than the uuid it replaces, so the words that repeat across the whole
# corpus are abbreviated and the parts that identify the document — department, number, year
# — are kept intact. Circulars only: a law's title is prose, where dropping "of" would run
# two different Acts together.
_CIRCULAR_ABBREVIATIONS = (
    (re.compile(r"\bcircular\s+letter\b", re.IGNORECASE), "CL"),
    (re.compile(r"\bcircular\b", re.IGNORECASE), "C"),
    (re.compile(r"\bno\.?\b", re.IGNORECASE), " "),
    (re.compile(r"\bof\b", re.IGNORECASE), " "),
)

_FILE_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def slugify(label: str, *, kind: str = "circular") -> str:
    """Turn a document's human label into a handle slug.

    Only ``[A-Za-z0-9-]`` survives, which is what lets a slug sit inside a JSON tool result
    without escaping and inside a handle without ambiguity.
    """
    text = (label or "").strip()
    if kind == "attachment":
        text = _FILE_EXTENSION.sub("", text)
    elif kind == "circular":
        for pattern, replacement in _CIRCULAR_ABBREVIATIONS:
            text = pattern.sub(replacement, text)
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", text.replace("&", "")) if part]
    if not parts:
        return "doc"
    slug = "-".join(parts)
    if len(slug) <= MAX_SLUG_CHARS:
        return slug
    # Over the cap, keep whole words from the front plus the final one: a long title's tail
    # is usually what tells it from its neighbours ("… Chapter 13: IMPORTS"). Two titles can
    # still land on the same slug, and _unique_slug settles that.
    last = parts[-1]
    head: list[str] = []
    budget = MAX_SLUG_CHARS - len(last) - 1
    for part in parts[:-1]:
        if len("-".join([*head, part])) > budget:
            break
        head.append(part)
    return "-".join([*head, last]) if head else last[:MAX_SLUG_CHARS]


class CitationHandles:
    """One turn's map between citation handles and real document ids.

    Turn-scoped by construction: nothing here is persisted, and a handle minted for one turn
    means nothing in the next. Prior assistant messages are re-minted on replay (see
    :meth:`for_history`) so a conversation never re-introduces raw ids to the model.
    """

    def __init__(self) -> None:
        self._slug_by_key: dict[tuple[str, str], str] = {}
        self._entry_by_slug: dict[str, tuple[str, str, str]] = {}
        self.dropped: list[dict[str, str]] = []
        self._reported = 0

    def take_dropped(self) -> list[dict[str, str]]:
        """Drops not yet reported, so a streamed turn can log them as they occur."""
        pending, self._reported = self.dropped[self._reported :], len(self.dropped)
        return pending

    # -- minting ---------------------------------------------------------------

    def mint(self, kind: str, identifier: str, label: str) -> str:
        """Return the handle for one document, creating it on first sight."""
        key = (kind, identifier)
        slug = self._slug_by_key.get(key)
        if slug is None:
            slug = self._unique_slug(slugify(label, kind=kind), key)
            self._slug_by_key[key] = slug
            self._entry_by_slug[slug.lower()] = (kind, identifier, label)
        return f"[[{KIND_PREFIX[kind]}:{slug}]]"

    def _unique_slug(self, base: str, key: tuple[str, str]) -> str:
        """Disambiguate two documents whose labels slugify the same way."""
        candidate = base
        suffix = 2
        while True:
            held = self._entry_by_slug.get(candidate.lower())
            if held is None or (held[0], held[1]) == key:
                return candidate
            candidate = f"{base}-{suffix}"
            suffix += 1

    def to_handles(self, text: str | None) -> str:
        """Rewrite every real citation token in model-bound text into a handle.

        Applied at the three points where text reaches the model — the selected-circular
        context, each tool result, and replayed history — so producers keep building
        ordinary tokens and only this module knows handles exist.
        """
        if not text:
            return text or ""
        return TOKEN_PATTERN.sub(
            lambda match: self.mint(
                match.group(1), match.group(2).strip(), (match.group(3) or "").strip()
            ),
            text,
        )

    def for_history(self, text: str | None) -> str:
        """Prepare a stored assistant turn for replay.

        Answers are persisted with real tokens, and the routes rebuild the conversation from
        those rows. Without this the model reads a transcript full of uuids on turn two and
        copies from it — the fix would hold for one turn and quietly decay after.
        """
        return strip_bare_uuids(self.to_handles(text))

    # -- expansion -------------------------------------------------------------

    def expand(self, text: str | None) -> str:
        """Turn a finished answer back into real citation tokens, dropping what does not resolve.

        This is the gate that decides what a reader sees, so it is deliberately total:

        * a known handle becomes a real token carrying the *server's* label, so a model that
          mangles the display text cannot mangle what the pill says;
        * an unknown handle degrades to its slug as plain prose — the sentence still reads,
          and no dead link ships;
        * a real token is kept only if its id was actually offered this turn;
        * a bare uuid outside a token is removed, which is safe precisely because the model
          was never shown one.
        """
        if not text:
            return text or ""
        pieces: list[str] = []
        cursor = 0
        for match in _COMBINED_PATTERN.finditer(text):
            pieces.append(strip_bare_uuids(text[cursor : match.start()]))
            cursor = match.end()
            if match.group(1):  # a real token the model wrote out in full
                replacement = self._keep_or_drop_token(
                    match.group(1), match.group(2).strip(), match.group(3) or ""
                )
            else:
                replacement = self._resolve_handle(match.group(4), match.group(5))
            if not replacement:
                cursor = _close_gap(pieces, text, cursor)
            pieces.append(replacement)
        pieces.append(strip_bare_uuids(text[cursor:]))
        return "".join(pieces)

    def _resolve_handle(self, prefix: str, slug: str) -> str:
        entry = self._entry_by_slug.get(slug.lower())
        if entry is None:
            self.dropped.append({"reason": "unknown_handle", "handle": f"{prefix}:{slug}"})
            return slug
        # A wrong kind marker on a slug that resolves is a typo, not a different citation:
        # the slug is the identifying half, so it wins and the reader still gets the link.
        # Nothing was lost, so nothing is recorded — `dropped` counts what the reader does
        # not get, and padding it with recoveries would make that number mean less.
        kind, identifier, label = entry
        return f"[[{kind}:{identifier}|{label}]]"

    def _keep_or_drop_token(self, kind: str, identifier: str, label: str) -> str:
        if (kind, identifier) in self._slug_by_key:
            return f"[[{kind}:{identifier}|{label}]]"
        self.dropped.append({"reason": "unknown_id", "handle": f"{kind}:{identifier}"})
        return label.strip()


_COMBINED_PATTERN = re.compile(
    f"{TOKEN_PATTERN.pattern}|{HANDLE_PATTERN.pattern}", re.IGNORECASE
)


def strip_bare_uuids(text: str) -> str:
    """Remove invented ids along with the space each one occupied.

    Each removal closes its own gap. An answer-wide tidy pass would be simpler and wrong:
    it reflows prose that merely looks like markup — a rate table written ``[[ see annexure
    ]]`` came back respaced because an unrelated id was dropped three paragraphs earlier.
    """
    return _BARE_UUID_PATTERN.sub("", text)


def _close_gap(pieces: list[str], text: str, cursor: int) -> int:
    """Absorb the whitespace, and any brackets, left around a citation that was dropped.

    Returns the cursor to resume from, which advances past a closing bracket when the
    dropped citation was the only thing inside it.
    """
    if pieces and pieces[-1].endswith(("(", "[")) and text[cursor : cursor + 1] in (")", "]"):
        pieces[-1] = pieces[-1][:-1].rstrip(" \t")
        return cursor + 1
    if pieces:
        pieces[-1] = pieces[-1].rstrip(" \t")
    return cursor


class StreamExpander:
    """Apply :meth:`CitationHandles.expand` to a token stream.

    The streaming route yields provider deltas straight to the client, so a handle arrives
    split across chunks. Text is held back only from the point where a citation could still
    be forming — an unclosed ``[[``, or a trailing run that could grow into a uuid — and is
    released as soon as that possibility is settled.
    """

    #: Past this, an unclosed bracket is prose, not a citation being written.
    MAX_HOLD = 200

    def __init__(self, handles: CitationHandles) -> None:
        self._handles = handles
        self._buffer = ""

    def feed(self, chunk: str) -> str:
        self._buffer += chunk
        cut = self._safe_cut()
        emitted, self._buffer = self._buffer[:cut], self._buffer[cut:]
        return self._handles.expand(emitted)

    def close(self) -> str:
        emitted, self._buffer = self._buffer, ""
        return self._handles.expand(emitted)

    def _safe_cut(self) -> int:
        """Index up to which the buffer can be expanded and released."""
        candidates = [len(self._buffer)]

        open_bracket = self._buffer.rfind("[[")
        if open_bracket >= 0 and "]]" not in self._buffer[open_bracket:]:
            candidates.append(open_bracket)
        elif self._buffer.endswith("["):
            candidates.append(len(self._buffer) - 1)

        # A uuid the model invented must not be released half-written, or the tail lands in
        # the answer as loose hex once the head is stripped. The run is held from its first
        # character, not from the eighth: by the time eight have arrived the first seven are
        # already gone. Ordinary words made of hex letters ("added", "cafe") are held for
        # the few characters until a non-hex character settles it.
        partial = re.search(r"\b[0-9a-fA-F]{1,8}(?:-[0-9a-fA-F]{1,12}){0,4}-?$", self._buffer)
        if partial:
            candidates.append(partial.start())

        trailing_space = re.search(r"\s+$", self._buffer)
        if trailing_space:
            candidates.append(trailing_space.start())

        # Whatever is held, the whitespace in front of it is held too. A citation that
        # turns out to be droppable takes the space before it with it, and it can only do
        # that if both are in the same release.
        cut = min(candidates)
        while cut > 0 and self._buffer[cut - 1].isspace():
            cut -= 1

        if len(self._buffer) - cut > self.MAX_HOLD:
            return len(self._buffer)
        return cut
