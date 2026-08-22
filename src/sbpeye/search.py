import re
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from sqlalchemy import text
from sqlalchemy.orm import Session, joinedload

from .models import Circular, RegDocument, RegDocumentVersion
from .database import collection, embedding_backend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stopwords — English common words + SBP boilerplate terms
# ---------------------------------------------------------------------------
STOPWORDS: set[str] = {
    # English function words
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "shall", "should", "may", "might", "can", "could", "not", "no", "nor",
    "if", "then", "than", "that", "this", "these", "those", "it", "its",
    "as", "so", "up", "out", "about", "into", "over", "after", "before",
    "between", "under", "above", "below", "through", "during", "each",
    "all", "any", "both", "few", "more", "most", "other", "some", "such",
    "only", "own", "same", "too", "very", "just", "also", "which", "who",
    "whom", "what", "when", "where", "how", "here", "there", "their",
    "them", "they", "we", "us", "our", "you", "your", "he", "she", "his",
    "her", "my", "me",
    # SBP boilerplate
    "state", "bank", "pakistan", 
    "dear", "sir", "madam", "ref", "subject", "please", "kindly",
    "enclosed", "attached", "herewith", "mentioned", "undersigned",
    "regards", "sincerely", "obedient", "servant",
}

# ---------------------------------------------------------------------------
# Comprehensive SBP regulatory synonym / acronym dictionary
# ---------------------------------------------------------------------------
SYNONYMS: dict[str, list[str]] = {
    # Anti-Money Laundering & Counter-Terrorism
    "aml": ["anti money laundering", "money laundering"],
    "cft": ["combating financing terrorism", "terror financing"],
    "cpf": ["countering proliferation financing", "proliferation financing"],
    "fatf": ["financial action task force"],
    "tfs": ["targeted financial sanctions", "financial sanctions"],
    "str": ["suspicious transaction report"],
    "ctr": ["currency transaction report"],
    "pep": ["politically exposed person"],
    "ml": ["money laundering"],
    "tf": ["terror financing", "terrorism financing"],

    # KYC & Customer Due Diligence
    "kyc": ["know your customer", "customer due diligence", "cdd"],
    "cdd": ["customer due diligence", "kyc", "know your customer"],
    "edd": ["enhanced due diligence"],
    "sdd": ["simplified due diligence"],
    "cip": ["customer identification program"],
    "ekyc": ["electronic know your customer", "digital kyc", "e kyc"],

    # Foreign Exchange
    "fx": ["foreign exchange", "forex"],
    "forex": ["foreign exchange", "fx"],
    "epd": ["exchange policy department"],
    "fca": ["foreign currency account"],
    "rda": ["roshan digital account"],
    "nrp": ["non resident pakistani"],
    "nrfc": ["non resident foreign currency"],
    "rfc": ["resident foreign currency"],
    "feam": ["foreign exchange adjudication manual"],
    "fema": ["foreign exchange manual"],
    "nostro": ["foreign correspondent account"],
    "vostro": ["domestic correspondent account"],
    "kerb": ["open market", "interbank"],
    "swap": ["currency swap", "fx swap"],
    "forward": ["forward contract", "forward cover"],
    "remittance": ["money transfer", "home remittance", "inward remittance"],
    "tt": ["telegraphic transfer", "wire transfer"],

    # SBP Departments
    "bprd": ["banking policy regulations department", "banking policy"],
    "acfid": ["agriculture credit financial inclusion"],
    "acd": ["agriculture credit department"],
    "dmmd": ["domestic markets monetary management"],
    "bsd": ["banking supervision department"],
    "psd": ["payment systems department", "payment systems oversight"],
    "ifpd": ["islamic finance policy department"],
    "ifdd": ["islamic finance development department"],
    "cpd": ["consumer protection department"],
    "bcpd": ["banking conduct policy department"],
    "fsd": ["financial stability department"],
    "cmd": ["currency management department", "currency accounts department"],
    "cad": ["currency accounts department"],
    "crmd": ["cyber risk management department"],
    "disd": ["digital innovation settlements department"],
    "fird": ["financial institutions resolution department"],
    "mfd": ["microfinance department"],
    "tod": ["treasury operations department"],
    "smefd": ["sme finance department"],
    "bsrvd": ["banking surveillance department"],

    # Prudential / Capital
    "car": ["capital adequacy ratio", "capital adequacy"],
    "crwa": ["credit risk weighted assets"],
    "npl": ["non performing loan", "bad loan", "classified loan"],
    "oaem": ["other assets especially mentioned"],
    "irac": ["income recognition asset classification"],
    "ecl": ["expected credit loss", "exchange company license"],
    "pcr": ["provision coverage ratio"],
    "leverage": ["leverage ratio"],
    "lcr": ["liquidity coverage ratio"],
    "nsfr": ["net stable funding ratio"],
    "hqla": ["high quality liquid assets"],
    "ccb": ["capital conservation buffer"],
    "d-sib": ["domestic systemically important bank"],
    "dsib": ["domestic systemically important bank"],
    "icaap": ["internal capital adequacy assessment process"],
    "orr": ["obligor risk rating"],
    "tier1": ["tier one capital", "core capital"],
    "tier2": ["tier two capital", "supplementary capital"],
    "cet1": ["common equity tier one"],
    "slr": ["statutory liquidity requirement"],
    "crr": ["cash reserve requirement"],
    "mcr": ["minimum capital requirement"],
    "paid up capital": ["minimum paid up capital"],

    # Monetary Policy & Rates
    "kibor": ["karachi interbank offered rate", "interbank rate"],
    "repo": ["repurchase agreement"],
    "omo": ["open market operation"],
    "orf": ["overnight reverse repo facility"],
    "sdf": ["standing deposit facility"],
    "slf": ["standing lending facility"],
    "discount": ["discount rate", "policy rate"],
    "policy rate": ["discount rate", "key policy rate"],
    "monetary policy": ["interest rate", "policy rate"],
    "mpc": ["monetary policy committee"],

    # Payment Systems
    "rtgs": ["real time gross settlement", "prism"],
    "prism": ["pakistan real time interbank settlement", "rtgs"],
    "iban": ["international bank account number"],
    "swift": ["society worldwide interbank financial telecommunication"],
    "raast": ["instant payment system", "faster payment"],
    "1link": ["interbank switch", "atm switch"],
    "psp": ["payment service provider"],
    "tpsp": ["third party service provider"],
    "emi": ["electronic money institution", "e money"],
    "emoney": ["electronic money", "e money", "emi"],
    "pos": ["point of sale"],
    "atm": ["automated teller machine"],
    "nift": ["national institutional facilitation technologies"],
    "dpc": ["digital payment certification"],

    # Banking Types & Institutions
    "dfi": ["development finance institution"],
    "mfb": ["microfinance bank"],
    "mfi": ["microfinance institution"],
    "nbfi": ["non banking financial institution"],
    "nbfc": ["non banking finance company"],
    "modaraba": ["islamic fund management"],
    "leasing": ["lease finance", "ijarah"],
    "branchless banking": ["digital banking", "mobile banking", "agent banking"],
    "digital banking": ["branchless banking", "mobile banking"],
    "mobile banking": ["branchless banking", "digital banking"],

    # Islamic Finance
    "sukuk": ["islamic bond", "shariah compliant bond"],
    "musharakah": ["partnership financing", "islamic partnership", "diminishing musharakah"],
    "murabaha": ["cost plus financing", "islamic trade finance"],
    "ijarah": ["islamic leasing", "shariah leasing"],
    "mudarabah": ["profit sharing", "islamic investment"],
    "salam": ["advance purchase", "forward sale"],
    "istisna": ["manufacturing contract", "construction finance"],
    "wakalah": ["agency contract", "islamic agency"],
    "takaful": ["islamic insurance", "shariah insurance"],
    "shariah": ["islamic law", "sharia"],
    "ssb": ["shariah supervisory board", "shariah board"],

    # SME & Agriculture
    "sme": ["small medium enterprise", "small business"],
    "msme": ["micro small medium enterprise"],
    "agri": ["agriculture", "agricultural", "farm"],
    "zarai": ["agriculture", "agricultural"],
    "crop loan": ["agricultural loan", "crop financing"],
    "clis": ["crop loan insurance scheme"],
    "markup": ["interest", "profit rate"],

    # Consumer / Conduct
    "bcp": ["banking conduct prudential", "business continuity plan"],
    "adr": ["alternative dispute resolution"],
    "grievance": ["complaint", "dispute"],
    "disclosure": ["transparency", "fair dealing"],
    "pricing": ["charges", "fees", "schedule of charges"],
    "soc": ["schedule of charges"],

    # Risk Management
    "orm": ["operational risk management"],
    "crm": ["credit risk management"],
    "mrm": ["market risk management"],
    "alm": ["asset liability management"],
    "alco": ["asset liability committee"],
    "stress test": ["scenario analysis", "stress testing"],
    "bcm": ["business continuity management"],
    "drp": ["disaster recovery plan"],
    "outsourcing": ["third party", "service provider"],
    "cybersecurity": ["cyber security", "information security", "it security"],
    "ransomware": ["cyber attack", "malware"],

    # Credit Bureau & Data
    "ecib": ["electronic credit information bureau", "credit bureau", "credit information"],
    "cib": ["credit information bureau"],

    # General Regulatory
    "gazette": ["official gazette", "government notification"],
    "sro": ["statutory regulatory order"],
    "prudential": ["prudential regulations", "prs"],
    "prs": ["prudential regulations"],
    "bpd": ["banking policy division"],
    "exposure": ["credit exposure", "concentration"],
    "provisioning": ["provision", "loan loss provision"],
    "write off": ["write-off", "loan write off"],
    "restructuring": ["loan restructuring", "rescheduling"],
    "covid": ["covid 19", "pandemic", "coronavirus"],
    "msb": ["minimum savings balance", "minimum balance"],
    "dormant": ["inactive account", "unclaimed deposit"],
    "escheat": ["unclaimed deposit"],
    "whitelist": ["approved list", "permitted list"],
    "blacklist": ["sanctioned", "debarred"],
    "fit proper": ["fit and proper", "eligibility criteria"],
    "moratorium": ["payment deferral", "grace period"],
    "green banking": ["sustainable finance", "climate finance", "esg"],
    "esg": ["environmental social governance", "green banking"],
    "climate": ["climate finance", "green banking", "climate risk"],
    "housing": ["housing finance", "mortgage", "home loan"],
    "mortgage": ["housing finance", "home loan"],
}

# ---------------------------------------------------------------------------
# Reference pattern — matches queries like "BPRD Circular No. 05 of 2024"
# ---------------------------------------------------------------------------
REFERENCE_PATTERN = re.compile(
    r"(?:^|\b)"
    r"([A-Za-z&]{2,}(?:\d)?)"        # dept code: BPRD, AC&MFD, BSD1, etc.
    r"\s*"
    r"(circular\s+letter|circular\s+let\.?|cir\.?\s+let\.?|letter|circular|cir\.?)?"  # doc type
    r"\s*"
    r"(?:no\.?|number|#)?"
    r"\s*"
    r"(\d{1,3})"                      # circular number (group 3)
    r"(?:"                            # optional year group
        r"\s*(?:of\s*)?"
        r"(\d{4})"                    # year (optional, group 4)
    r")?",
    re.IGNORECASE,
)


def tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase words, removing stopwords."""
    text_lower = text.lower()
    pattern = r"(?<=\b[a-z])\.(?=[a-z]|\s|$)"
    normalized = re.sub(pattern, "", text_lower)
    return [w for w in re.findall(r"\w+", normalized) if len(w) > 1 and w not in STOPWORDS]


def _build_multiword_synonyms() -> dict[str, list[str]]:
    """Index multi-word SYNONYMS keys by their tokenized (stopword-stripped)
    form, so a key like "paid up capital" is looked up as "paid capital" —
    matching what query tokens look like after tokenize() has already
    dropped stopwords like "up" and split on hyphens.
    """
    index: dict[str, list[str]] = {}
    for key, values in SYNONYMS.items():
        key_tokens = tokenize(key)
        if len(key_tokens) < 2:
            continue
        index.setdefault(" ".join(key_tokens), []).extend(values)
    return index


# Multi-word keys indexed by tokenized form, e.g. "paid up capital" -> "paid capital".
MULTIWORD_SYNONYMS: dict[str, list[str]] = _build_multiword_synonyms()


def expand_query_tokens(tokens: list[str]) -> list[str]:
    """Expand query tokens with domain synonyms/acronyms.

    Also handles multi-word synonym keys by checking bigrams/trigrams.
    """
    expanded = list(tokens)
    seen: set[str] = set(tokens)

    # Single-token synonyms
    for token in tokens:
        for synonym_phrase in SYNONYMS.get(token, []):
            for w in tokenize(synonym_phrase):
                if w not in seen:
                    expanded.append(w)
                    seen.add(w)

    # Multi-word keys (bigrams/trigrams) — e.g. "policy rate", "paid up capital"
    for window in (2, 3):
        for i in range(len(tokens) - window + 1):
            phrase = " ".join(tokens[i:i + window])
            for synonym_phrase in MULTIWORD_SYNONYMS.get(phrase, []):
                for w in tokenize(synonym_phrase):
                    if w not in seen:
                        expanded.append(w)
                        seen.add(w)

    return expanded


# ---------------------------------------------------------------------------
# Metric resolution — acronym/expansion-aware matching for regulatory values
# ---------------------------------------------------------------------------

_SYNONYM_GROUPS: list[set[str]] | None = None


def _synonym_groups() -> list[set[str]]:
    """Group every SYNONYMS key with its expansion phrases (lowercased).

    e.g. {"crr", "cash reserve requirement"} so a query for either surface form
    resolves to the whole group. Built once and cached.
    """
    global _SYNONYM_GROUPS
    if _SYNONYM_GROUPS is None:
        groups: list[set[str]] = []
        for key, phrases in SYNONYMS.items():
            group = {key.lower()}
            group.update(p.lower() for p in phrases)
            groups.append(group)
        _SYNONYM_GROUPS = groups
    return _SYNONYM_GROUPS


def _expansion_set(term: str) -> set[str]:
    """All surface forms a term should also match: the term plus any synonym
    group(s) any of its forms belongs to."""
    term_l = term.strip().lower()
    forms = {term_l}
    for group in _synonym_groups():
        if forms & group or any(g in term_l or term_l in g for g in group):
            forms |= group
    return {f for f in forms if f}


def resolve_metric_terms(term: str, distinct_metrics: Iterable[str]) -> list[str]:
    """Return the stored metric strings that match ``term``, acronym/expansion-aware.

    Matching is precision-first and never returns *fewer* hits than a plain
    substring filter:
      1. Expand ``term`` to its synonym group(s) (e.g. CRR ↔ cash reserve requirement).
      2. A metric matches if any expanded form is a substring of it (or vice versa),
         or the metric's own expansion set intersects the term's.
      3. Fallback: synonym-expanded token overlap, so word-order/partial phrasings hit.
    """
    term_l = (term or "").strip().lower()
    if not term_l:
        return []

    metrics = [m for m in distinct_metrics if m]
    term_forms = _expansion_set(term_l)

    matched: list[str] = []
    leftovers: list[str] = []
    for metric in metrics:
        metric_l = metric.lower()
        # 1. Plain substring (preserve current behavior as a floor).
        # 2. Synonym-expanded substring, either direction.
        if any(form in metric_l or metric_l in form for form in term_forms):
            matched.append(metric)
            continue
        # 3. Group-intersection: metric expands into the same synonym group.
        if _expansion_set(metric_l) & term_forms:
            matched.append(metric)
            continue
        leftovers.append(metric)

    if matched:
        return matched

    # Fallback: synonym-expanded token overlap for word-order / partial phrasings.
    query_tokens = set(expand_query_tokens(tokenize(term_l)))
    if not query_tokens:
        return []
    for metric in leftovers:
        metric_tokens = set(expand_query_tokens(tokenize(metric)))
        if query_tokens & metric_tokens:
            matched.append(metric)
    return matched


# ---------------------------------------------------------------------------
# Document chunking utilities (used by scraper + reindex)
# ---------------------------------------------------------------------------

_BOILERPLATE_RE = re.compile(
    r"^State Bank of Pakistan\s+Circulars?/?Notifications?\s*/?",
    re.IGNORECASE,
)


def strip_boilerplate(text: str) -> str:
    """Remove common SBP boilerplate from the start of circular text."""
    return _BOILERPLATE_RE.sub("", text).strip()


def prepare_chunks(
    title: str,
    content: str,
    max_words: int = 350,
    overlap_words: int = 75,
) -> list[str]:
    """Split content into overlapping chunks, each prefixed with the title.

    Designed for BAAI/bge-base-en-v1.5 with a 512-token context window.
    350 words ≈ 420 tokens, leaving room for title + special tokens.
    """
    content = strip_boilerplate(content or "")
    words = content.split()

    if not words:
        return [title] if title else []

    if len(words) <= max_words:
        return [f"{title}. {content}"]

    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(start + max_words, len(words))
        chunk_body = " ".join(words[start:end])
        chunks.append(f"{title}. {chunk_body}")
        if end >= len(words):
            break
        start += max_words - overlap_words

    return chunks


# ---------------------------------------------------------------------------
# Passage primitives and match evidence
# ---------------------------------------------------------------------------

# Words in the preview window cut out of a passage.
SNIPPET_WINDOW = 25


def best_window(text: str, query_tokens: set[str], window: int = SNIPPET_WINDOW) -> str:
    """The densest `window`-word run of `text`, by count of query-matching words.

    Only ever call this on a *passage* — one retrieved chunk, or a short circular
    body (the corpus median is 164 words). Term density picks a readable line out
    of a passage; run over a whole document it reliably prefers prose *about* a
    subject to the table that states the value, because prose repeats the subject's
    words and a table states them once. Which passage matched is retrieval's answer
    to give (see `MatchEvidence`), not something to re-derive here.
    """
    if not text or not query_tokens:
        return ""
    words = text.split()
    if not words:
        return ""
    if len(words) <= window:
        return text

    # Each word is tested against the query once, then the window score *rolls*: add the
    # word entering on the right, subtract the one leaving on the left. Scoring each
    # window from scratch re-tested all 25 words at every one of the N positions, so a
    # word was examined 25 times over — O(N·window·tokens) to compute something that is
    # O(N·tokens) plus a running total.
    #
    # It read as a detail because the docstring above promises this only ever sees a
    # passage. `_scan_documents` does not keep that promise: on the lexical-only fallback
    # it previews *whole attachments*, and the largest in the corpus is 99,321 words. That
    # made this single function 87% of a search — 17.3 million inner comparisons for three
    # queries, half a second for one document. Same snippet, verified byte-for-byte
    # against the previous implementation across the attachment corpus.
    strip_punctuation = re.compile(r"[^\w]").sub
    hits = [
        1 if any(qt in w for qt in query_tokens) else 0
        for w in (strip_punctuation("", word).lower() for word in words)
    ]

    score = sum(hits[:window])
    best_score = score
    best_pos = 0
    for i in range(1, len(words) - window + 1):
        score += hits[i + window - 1] - hits[i - 1]
        if score > best_score:
            best_score = score
            best_pos = i

    end = min(len(words), best_pos + window)
    snippet = " ".join(words[best_pos:end])
    if best_pos > 0:
        snippet = "…" + snippet
    if end < len(words):
        snippet += "…"
    return snippet


def highlight_terms(text: str, query_tokens: set[str]) -> str:
    """Wrap query matches in ``<mark>`` for the search UI. Presentation only."""
    if not text or not query_tokens:
        return text or ""
    for token in query_tokens:
        if token.isalpha():
            # Matches dotted acronyms like T.T. or T.T as well as plain prefixes.
            dotted = r'\.?'.join(re.escape(c) for c in token) + r'\.?(?!\w)'
            pattern = rf"(?i)\b({dotted}|{re.escape(token)}\w*)"
        else:
            pattern = rf"(?i)\b({re.escape(token)}\w*)"
        text = re.sub(pattern, r"<mark>\1</mark>", text)
    return text


# Characters of document kept around the densest run of query terms, before the
# word-level window is cut out of it. Roughly 300 words — an order of magnitude more
# context than the 25-word window needs, so the fine pass still has room to choose.
PREVIEW_REGION_CHARS = 2000


@lru_cache(maxsize=128)
def _token_scan_pattern(tokens: frozenset[str]):
    """One compiled alternation matching any query token as a substring.

    Mirrors `best_window`'s test (`qt in word`, case-folded) rather than
    `highlight_terms`'s word-boundary one: this decides *where to look*, so being stricter
    than the scorer that follows would hide a region the scorer would have picked.

    It is still very slightly stricter in one case, and deliberately so. `best_window`
    strips punctuation *inside* a word before testing, so it matches "sme" in "S.M.E";
    this pattern does not. Allowing `[\\W_]*` between characters closes that gap and opens
    a worse one — the loose form matches letters scattered across unrelated words, which
    sent the preview to a region containing no whole query term at all. Measured over the
    corpus, the loose pattern turned 3 such misses into 192. The strict form's 3 misses
    fall back to the opening of the document, which is a reasonable preview; the loose
    form's 192 land on noise.
    """
    terms = sorted((t for t in tokens if t), key=len, reverse=True)
    if not terms:
        return None
    return re.compile("|".join(re.escape(term) for term in terms), re.IGNORECASE)


def _preview_region(
    text: str, query_tokens: set[str], span: int = PREVIEW_REGION_CHARS
) -> tuple[str, bool, bool]:
    """The `span` characters around the first query match, and whether text was cut off.

    A coarse pass in front of the fine one, and the reason previews stopped being the most
    expensive thing a search does.

    `best_window` scores every word position in Python. That is linear, but the constant is
    a Python-level loop and the input was a whole document: law editions average 45 KB and
    the largest attachment in the corpus is 99,321 words. Running it over the full text
    made it 87% of a law search. `best_window`'s own docstring says never to do this — not
    for speed, but because density across a whole document prefers prose *about* a subject
    to the table that states it — so no quality argument was holding the old behaviour up.

    **First match rather than densest**, which is a deliberate retreat from an earlier
    version of this function. Locating *every* match to pick the densest region costs 3×
    against 12×, and the reason is the opposite of the intuition: `finditer` is cheap when
    a document is full of matches and expensive when it is nearly empty of them, because it
    has to reach the end to prove there are no more. `search` stops at the first hit. The
    documents that dominate the bill are the weak matches, and those are exactly the ones
    where the densest window was never meaningful anyway.

    Measured over the corpus (five token sets, every law version and attachment):

        strategy              laws          attachments   snippet unchanged
        densest, all hits     3.1x          2.5x          44% / 79%
        first hit  (this)     12.5x         4.8x          36% / 76%

    Returns the region and whether text was dropped before and after it, so the caller can
    keep the ellipses honest.
    """
    if len(text) <= span:
        return text, False, False

    pattern = _token_scan_pattern(frozenset(query_tokens))
    match = pattern.search(text) if pattern else None
    if match is None:
        # Nothing matches, so every window scores zero and `best_window` takes the first.
        # Handing it the opening of the document reaches the same answer without the scan.
        return text[:span], False, True

    # A little lead-in, so the window is not forced to begin mid-sentence on the match.
    start = max(0, match.start() - 40)
    end = min(len(text), start + span)
    return text[start:end], start > 0, end < len(text)


def make_preview(
    text: str, query_tokens: set[str], window: int = SNIPPET_WINDOW
) -> str:
    """A short highlighted blurb for the search UI: locate a window, then mark it up.

    Only ever called on whole documents — the two fallback paths that have no retrieved
    chunk to quote (`_law_result`, `_scan_documents`). Callers holding a real passage go
    straight to `best_window`, which is what it is documented for.
    """
    region, cut_before, cut_after = _preview_region(text, query_tokens)
    snippet = best_window(region, query_tokens, window)
    # `best_window` marks its own edges, but it only sees the region — so a window sitting
    # flush against a cut looks like the start or end of the document unless said here.
    if snippet:
        if cut_before and not snippet.startswith("…"):
            snippet = "…" + snippet
        if cut_after and not snippet.endswith("…"):
            snippet = snippet + "…"
    return highlight_terms(snippet, query_tokens)


def _window_density(window_text: str, query_tokens: set[str]) -> int:
    """How many words of a preview window match the query. Used only to choose
    between passages retrieval already ranked as comparable."""
    return sum(
        1
        for word in window_text.split()
        if any(qt in re.sub(r"[^\w]", "", word).lower() for qt in query_tokens)
    )


@dataclass(frozen=True)
class MatchEvidence:
    """Where a hit actually came from, carried out of the retriever.

    The vector arm knows which chunk matched. Collapsing that to a document rank for
    RRF is right for *ranking* and lossy for everything else, so the chunk rides along
    here instead of being reconstructed downstream by guesswork. `doc_type` and
    `source_id` mirror the chunk metadata written at index time: an attachment chunk
    carries its attachment id, a circular-body chunk carries none, a law chunk carries
    the version whose text is in force.
    """

    text: str
    doc_type: str                    # "circular" | "attachment" | "law"
    source_id: str | None = None     # attachment id, or law version id
    source_label: str | None = None  # attachment filename
    page: int | None = None
    source_ref: str | None = None
    distance: float | None = None
    chunk_index: int | None = None


def _evidence_from_chunk(
    meta: dict, text: str, distance: float | None
) -> MatchEvidence:
    """Build evidence from one Chroma chunk's metadata, for either corpus."""
    doc_type = meta.get("doc_type") or "circular"
    if doc_type == "law":
        source_id = meta.get("version_id")
        source_label = meta.get("part_label") or meta.get("title") or None
    else:
        source_id = meta.get("attachment_id")
        source_label = meta.get("filename") if source_id else None
    return MatchEvidence(
        text=text or "",
        doc_type=doc_type,
        source_id=source_id,
        source_label=source_label,
        page=meta.get("page_start"),
        source_ref=meta.get("ref"),
        distance=distance,
        chunk_index=meta.get("chunk_index"),
    )


def choose_evidence(
    evidence: list[MatchEvidence], query_tokens: set[str]
) -> tuple[MatchEvidence, str] | None:
    """Pick which matched passage to show, and its preview.

    Retrieval decides *which passages are candidates*; this only decides which of
    those to put in front of the reader. Nearest-by-embedding is not automatically
    the most informative: the top two chunks of a document are routinely separated
    by a rounding error in distance while one holds a heading and the other holds
    the table the question is about. Among passages retrieval already scored as
    comparable, term density is a fair way to break the tie — the failure mode it
    has over a whole document (preferring prose about a subject to the figure
    itself) needs a large field of unmatched text to bite, and there isn't one here.

    Ties keep the retrieval order, so the nearest chunk wins when nothing separates
    them on density.
    """
    scored = [
        (item, best_window(item.text, query_tokens))
        for item in evidence
        if item.text
    ]
    if not scored:
        return None
    item, window = max(
        scored, key=lambda pair: _window_density(pair[1], query_tokens)
    )
    return item, highlight_terms(window, query_tokens)


def _collect_evidence(
    results: dict, owner_key: str, keep: int
) -> tuple[dict[str, int], dict[str, list[MatchEvidence]]]:
    """Split one Chroma response into per-owner ranks and per-owner evidence.

    `owner_key` is the metadata field that names the thing being ranked —
    ``circular_id`` for circulars, ``document_id`` for laws. Ranks are unchanged
    from the old de-duplicating loop, so RRF fusion behaves exactly as before; the
    evidence is the part that used to be dropped on the floor.
    """
    ranks: dict[str, int] = {}
    evidence: dict[str, list[MatchEvidence]] = {}
    ids = results["ids"][0] if results.get("ids") else []
    metas = results["metadatas"][0] if results.get("metadatas") else []
    documents = results["documents"][0] if results.get("documents") else []
    distances = results["distances"][0] if results.get("distances") else []

    for index, chunk_id in enumerate(ids):
        meta = metas[index] if index < len(metas) else {}
        owner_id = meta.get(owner_key, chunk_id)
        if owner_id not in ranks:
            ranks[owner_id] = len(ranks) + 1
        chunk_text = documents[index] if index < len(documents) else ""
        # Ranking never depends on the stored text, but quoting does. A chunk whose
        # text did not come back is still a legitimate rank and a useless quote, so
        # it is left out of the evidence and the caller falls back to scanning.
        if not chunk_text:
            continue
        bucket = evidence.setdefault(owner_id, [])
        if len(bucket) < keep:
            bucket.append(
                _evidence_from_chunk(
                    meta,
                    chunk_text,
                    distances[index] if index < len(distances) else None,
                )
            )
    return ranks, evidence


# ---------------------------------------------------------------------------
# FTS5 lexical index (persistent, incremental — replaces in-memory rank-bm25)
# ---------------------------------------------------------------------------

# bm25() column weights, applied at query time. Reference outranks title
# outranks body, mirroring the old title×3 / reference×5 token duplication.
FTS_WEIGHTS: tuple[float, float, float] = (3.0, 5.0, 1.0)  # title, reference, body

_FTS_CREATE_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS circulars_fts USING fts5("
    "circular_id UNINDEXED, title, reference, body, tokenize='unicode61')"
)


def _fts_reference_tokens(reference: str | None) -> list[str]:
    """Reference tokens plus padded/unpadded digit forms so a query for "8"
    matches a stored "08" and vice-versa (preserves the old BM25 behavior)."""
    ref_tokens = tokenize(reference or "")
    extra: list[str] = []
    for tok in ref_tokens:
        if tok.isdigit():
            extra.append(tok.lstrip("0") or "0")
            extra.append(tok.zfill(2))
    return ref_tokens + extra


def _fts_row(circular: Circular) -> tuple[str, str, str]:
    """Build the (title, reference, body) token strings stored in one FTS row.

    Cells hold the space-joined output of ``tokenize()`` — the semantic
    tokenization (SBP stopwords, dotted-acronym handling, 1-char filtering)
    happens here in Python; FTS5's unicode61 tokenizer then just splits on
    whitespace. Body aggregates the circular's own text and every attachment's,
    exactly like the old per-circular BM25 document.
    """
    title = " ".join(tokenize(circular.title or ""))
    reference = " ".join(_fts_reference_tokens(circular.reference))
    body_tokens = tokenize(circular.content_text or "")
    for attachment in circular.attachments:
        body_tokens = body_tokens + tokenize(attachment.content_text or "")
    return title, reference, " ".join(body_tokens)


def _fts_ensure_table(conn) -> None:
    conn.execute(text(_FTS_CREATE_SQL))


def index_circular_fts(db: Session, circular: Circular) -> None:
    """Upsert one circular's FTS row (delete-then-insert). Idempotent.

    Call wherever a circular's or its attachments' text changes — co-located
    with the Chroma writes. Commits so the change is durable and visible to
    other processes (e.g. the web server reading a CLI sync's writes).
    """
    conn = db.connection()
    _fts_ensure_table(conn)
    title, reference, body = _fts_row(circular)
    conn.execute(
        text("DELETE FROM circulars_fts WHERE circular_id = :cid"),
        {"cid": circular.id},
    )
    conn.execute(
        text(
            "INSERT INTO circulars_fts (circular_id, title, reference, body) "
            "VALUES (:cid, :title, :reference, :body)"
        ),
        {"cid": circular.id, "title": title, "reference": reference, "body": body},
    )
    db.commit()


def delete_circular_fts(db: Session, circular_id: str) -> None:
    """Remove a circular's FTS row (for deletions)."""
    conn = db.connection()
    _fts_ensure_table(conn)
    conn.execute(
        text("DELETE FROM circulars_fts WHERE circular_id = :cid"),
        {"cid": circular_id},
    )
    db.commit()


def backfill_fts(db: Session, force: bool = False) -> None:
    """Build the FTS index from all circulars if it is empty (or ``force``).

    Replaces the old per-startup full BM25 rebuild: once populated this is a
    cheap no-op, and thereafter the index is maintained incrementally. Pass
    ``force=True`` to fully rebuild (e.g. the ``reindex`` CLI command).
    """
    conn = db.connection()
    _fts_ensure_table(conn)
    if force:
        conn.execute(text("DELETE FROM circulars_fts"))
    elif conn.execute(text("SELECT count(*) FROM circulars_fts")).scalar():
        return
    circulars = db.query(Circular).options(joinedload(Circular.attachments)).all()
    for circular in circulars:
        title, reference, body = _fts_row(circular)
        conn.execute(
            text(
                "INSERT INTO circulars_fts (circular_id, title, reference, body) "
                "VALUES (:cid, :title, :reference, :body)"
            ),
            {"cid": circular.id, "title": title, "reference": reference, "body": body},
        )
    db.commit()


# ---------------------------------------------------------------------------
# FTS5 lexical index for laws & regulations
# ---------------------------------------------------------------------------

# A separate table rather than a doc_kind column on circulars_fts: the two corpora have
# different column shapes (a law has no reference; it has a part label), and keeping them
# apart means nothing about circular search can regress.
LAW_FTS_WEIGHTS: tuple[float, float, float] = (3.0, 4.0, 1.0)  # title, part_label, body

_LAW_FTS_CREATE_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS laws_fts USING fts5("
    "document_id UNINDEXED, title, part_label, body, tokenize='unicode61')"
)

# Manifest versions hold JSON bookkeeping, not readable text (see scraper/laws.py).
NON_TEXT_LAW_FILE_TYPES = {"manifest"}


def _law_fts_ensure_table(conn) -> None:
    conn.execute(text(_LAW_FTS_CREATE_SQL))


def _searchable_law_version(document: RegDocument) -> RegDocumentVersion | None:
    """The version of a document that search should see: the one in force, if it's text.

    Superseded versions stay in SQLite and in the archive but are not searchable — a
    search hit on text SBP no longer publishes would be actively misleading.
    """
    version = document.current_version
    if version is None or version.file_type in NON_TEXT_LAW_FILE_TYPES:
        return None
    return version


def _law_fts_row(document: RegDocument) -> tuple[str, str, str]:
    """The (title, part_label, body) token strings for one document's FTS row."""
    version = _searchable_law_version(document)
    title = " ".join(tokenize(document.title or ""))
    part_label = " ".join(tokenize(document.part_label or ""))
    body = " ".join(tokenize(version.content_text or "" if version else ""))
    return title, part_label, body


def index_law_fts(db: Session, document: RegDocument) -> None:
    """Upsert one law/regulation's FTS row (delete-then-insert). Idempotent.

    Call from every path that changes which text is in force for a document — paired
    with the Chroma write, the same rule circulars follow.
    """
    conn = db.connection()
    _law_fts_ensure_table(conn)
    title, part_label, body = _law_fts_row(document)
    conn.execute(
        text("DELETE FROM laws_fts WHERE document_id = :did"), {"did": document.id}
    )
    if title or body:
        conn.execute(
            text(
                "INSERT INTO laws_fts (document_id, title, part_label, body) "
                "VALUES (:did, :title, :part_label, :body)"
            ),
            {"did": document.id, "title": title, "part_label": part_label, "body": body},
        )
    db.commit()


def delete_law_fts(db: Session, document_id: str) -> None:
    conn = db.connection()
    _law_fts_ensure_table(conn)
    conn.execute(
        text("DELETE FROM laws_fts WHERE document_id = :did"), {"did": document_id}
    )
    db.commit()


def backfill_laws_fts(db: Session, force: bool = False) -> int:
    """Build the laws FTS index if it is empty (or ``force``). Returns rows written."""
    conn = db.connection()
    _law_fts_ensure_table(conn)
    if force:
        conn.execute(text("DELETE FROM laws_fts"))
    elif conn.execute(text("SELECT count(*) FROM laws_fts")).scalar():
        return 0

    written = 0
    for document in db.query(RegDocument).all():
        title, part_label, body = _law_fts_row(document)
        if not (title or body):
            continue
        conn.execute(
            text(
                "INSERT INTO laws_fts (document_id, title, part_label, body) "
                "VALUES (:did, :title, :part_label, :body)"
            ),
            {"did": document.id, "title": title, "part_label": part_label, "body": body},
        )
        written += 1
    db.commit()
    return written


def _result_sort_date(item) -> float:
    """Sort key for date-ordering a mixed result list.

    A circular has a publication date; a law has only the moment we first captured its
    current text, since SBP's listing dates are unreliable placeholders.
    """
    if isinstance(item, Circular):
        return item.date.timestamp() if item.date else 0.0
    if isinstance(item, RegDocument):
        version = _searchable_law_version(item)
        stamp = (version.first_seen_at if version else None) or item.first_seen_at
        return stamp.timestamp() if stamp else 0.0
    return 0.0


# ---------------------------------------------------------------------------
# Search Engine
# ---------------------------------------------------------------------------


class SearchEngine:
    CANDIDATE_COUNT = 50           # candidates per retrieval method
    VECTOR_OVERFETCH = 5           # neighbours fetched per candidate — see _query_chunks
    RRF_K = 60                     # RRF damping constant
    TITLE_MATCH_BONUS = 0.05       # per-word title overlap bonus
    DEPT_MATCH_BONUS = 0.02        # per-word department overlap bonus
    RECENCY_WEIGHT = 0.008         # recency decay weight
    REFERENCE_BONUS = 0.5          # bonus for exact reference matches
    EVIDENCE_K = 3                 # matched chunks retained per result

    def _fts_ranks(self, db: Session, expanded_tokens: list[str]) -> dict[str, int]:
        """Rank circulars via the persistent FTS5 index for the expanded query.

        Returns ``{circular_id: rank}`` (rank 1 = best), the same shape the old
        in-memory BM25 arm produced, so RRF fusion downstream is unchanged.
        """
        ranks: dict[str, int] = {}
        terms = [t for t in expanded_tokens if t]
        if not terms:
            return ranks

        # Ensure the virtual table exists so a never-backfilled DB yields an empty
        # lexical arm rather than crashing the query (and poisoning the session).
        _fts_ensure_table(db.connection())

        # Quote every term so FTS5 treats it as a literal (never as a bare
        # operator), doubling any embedded quote; OR them across all columns.
        match_query = " OR ".join('"%s"' % t.replace('"', '""') for t in terms)
        order_by = "bm25(circulars_fts, %g, %g, %g)" % FTS_WEIGHTS
        try:
            rows = db.execute(
                text(
                    "SELECT circular_id FROM circulars_fts "
                    "WHERE circulars_fts MATCH :mq "
                    f"ORDER BY {order_by} "
                    "LIMIT :lim"
                ),
                {"mq": match_query, "lim": self.CANDIDATE_COUNT},
            ).fetchall()
        except Exception:
            logger.exception(
                "FTS5 lexical search failed — falling back to vector/reference only"
            )
            return ranks

        for rank, row in enumerate(rows):
            ranks[row[0]] = rank + 1
        return ranks

    @staticmethod
    def _apply_circular_filters(
        q_obj,
        *,
        start_year: int | None = None,
        end_year: int | None = None,
        department: str | None = None,
        tag: str | None = None,
    ):
        """Apply the circular-corpus filters to a query. Shared by every search path."""
        from sqlalchemy import extract, or_

        if start_year:
            q_obj = q_obj.filter(extract('year', Circular.date) >= start_year)
        if end_year:
            q_obj = q_obj.filter(extract('year', Circular.date) <= end_year)
        if department and department.strip():
            dept = department.strip()
            q_obj = q_obj.filter(
                or_(
                    Circular.department == dept,
                    Circular.department.ilike(f"%{dept}%"),
                )
            )
        if tag and tag.strip():
            q_obj = q_obj.filter(
                or_(
                    Circular.tags.like(f'%"{tag}"%'),
                    Circular.tags.like(f'%{tag}%'),
                )
            )
        return q_obj

    def _query_chunks(self, query: str, keep_chunk, where: dict) -> dict:
        """Nearest chunks matching `keep_chunk`, filtered in Python rather than by Chroma.

        Circulars and laws share one collection, so each arm has to see only its own
        chunks. Expressing that as a `where=` on the query is the obvious way and is
        startlingly expensive: a metadata pre-filter makes Chroma walk all 44,395 chunks
        instead of descending the HNSW index. Measured, same query, `n_results=50`:

            where={"doc_type": {"$in": ["circular", "attachment"]}}   60.1 ms
            where={"doc_type": {"$ne": "law"}}                        49.7 ms
            no filter                                                  1.9 ms
            no filter, n_results=150                                   5.0 ms

        So: ask for `VECTOR_OVERFETCH` times as many neighbours with no filter, drop the
        foreign ones here, and keep the nearest `CANDIDATE_COUNT` of what is left.

        **This only works because circular chunks are the bulk of the store** — 36,226 of
        44,395 against the law corpus's 8,169. A circular query's 150 nearest neighbours
        are overwhelmingly circular chunks (worst case measured across 14 queries: 99, so
        twice what the arm needs). The mirror image is not true and `_law_vector_ranks`
        deliberately does not call this; the note there has the numbers.

        A majority is not a guarantee, so `where` is kept as a fallback rather than
        deleted: if the over-fetch does come up short the query is simply re-run the old
        way, and the arm is never weaker than it was before this optimisation — only
        sometimes slower, on queries that do not arise in practice. `VECTOR_OVERFETCH` is
        sized to keep that path cold. Measured worst case over ten queries chosen to be
        hostile (law-flavoured phrasing, single stopwords, junk):

            n_results=150   5.2 ms   worst yield  59   1.2x margin
            n_results=250   8.6 ms   worst yield 128   2.6x margin   (this)
            n_results=400  14.2 ms   worst yield 220   4.4x margin

        The version with no heuristic at all is one collection per corpus (the 1.9 ms
        row), which costs a re-index of a 330 MB store; see `docs/PERFORMANCE_PLAN.md` P3.
        """
        embeddings = embedding_backend.embed_queries([query])
        results = collection.query(
            query_embeddings=embeddings,
            n_results=self.CANDIDATE_COUNT * self.VECTOR_OVERFETCH,
            include=["metadatas", "documents", "distances"],
        )

        metas = results["metadatas"][0] if results.get("metadatas") else []
        keep = [i for i, meta in enumerate(metas) if keep_chunk(meta)]

        if len(keep) < self.CANDIDATE_COUNT and len(metas) >= (
            self.CANDIDATE_COUNT * self.VECTOR_OVERFETCH
        ):
            # Short, and not merely because the store holds fewer chunks than we asked
            # for. Pay the pre-filter rather than hand back a thinner arm.
            logger.debug(
                "Vector over-fetch yielded %d of %d candidates for %r — "
                "falling back to the metadata filter",
                len(keep), self.CANDIDATE_COUNT, query,
            )
            return collection.query(
                query_embeddings=embeddings,
                n_results=self.CANDIDATE_COUNT,
                where=where,
                include=["metadatas", "documents", "distances"],
            )

        keep = keep[: self.CANDIDATE_COUNT]
        # Rebuilt in Chroma's own shape so `_collect_evidence` cannot tell the difference.
        return {
            field: [[results[field][0][i] for i in keep]]
            for field in ("ids", "metadatas", "documents", "distances")
            if results.get(field)
        }

    def _vector_ranks(
        self, query: str
    ) -> tuple[dict[str, int], dict[str, list[MatchEvidence]]]:
        """Rank circulars via Chroma. ``({circular_id: rank}, {circular_id: evidence})``.

        Chunks are collapsed to their parent circular for ranking; the chunks
        themselves come back as evidence, so a result can quote the passage that
        actually matched instead of hunting for one afterwards. A vector-store
        failure degrades to an empty arm rather than taking the whole search down.
        """
        try:
            # Laws share the collection; without this filter their chunks would enter the
            # circular candidate set as ids that resolve to no circular, silently
            # displacing real hits. Every circular/attachment chunk carries doc_type.
            results = self._query_chunks(
                query,
                lambda meta: meta.get("doc_type") in ("circular", "attachment"),
                where={"doc_type": {"$in": ["circular", "attachment"]}},
            )
        except Exception:
            logger.exception(
                "ChromaDB vector search failed — falling back to BM25-only"
            )
            return {}, {}

        return _collect_evidence(results, "circular_id", self.EVIDENCE_K)

    def _law_fts_ranks(self, db: Session, expanded_tokens: list[str]) -> dict[str, int]:
        """Rank laws/regulations via the `laws_fts` index. ``{document_id: rank}``."""
        ranks: dict[str, int] = {}
        terms = [t for t in expanded_tokens if t]
        if not terms:
            return ranks

        _law_fts_ensure_table(db.connection())
        match_query = " OR ".join('"%s"' % t.replace('"', '""') for t in terms)
        order_by = "bm25(laws_fts, %g, %g, %g)" % LAW_FTS_WEIGHTS
        try:
            rows = db.execute(
                text(
                    "SELECT document_id FROM laws_fts "
                    "WHERE laws_fts MATCH :mq "
                    f"ORDER BY {order_by} "
                    "LIMIT :lim"
                ),
                {"mq": match_query, "lim": self.CANDIDATE_COUNT},
            ).fetchall()
        except Exception:
            logger.exception("laws_fts lexical search failed — vector arm only")
            return ranks

        for rank, row in enumerate(rows):
            ranks[row[0]] = rank + 1
        return ranks

    def _law_vector_ranks(
        self, query: str
    ) -> tuple[dict[str, int], dict[str, list[MatchEvidence]]]:
        """Rank laws via Chroma, restricted to law chunks.

        Returns ``({document_id: rank}, {document_id: [MatchEvidence, …]})``.

        Keeps the metadata pre-filter that `_vector_ranks` was able to drop, because here
        the trade runs the other way. Laws are 8,169 of 44,395 chunks, so they are sparse
        in any unfiltered neighbourhood and the over-fetch starves: across five law
        queries the top 150 held as few as 4 law chunks against the 50 this arm needs.
        Buying the margin back costs more than the filter does — measured, worst of five:

            where={"kind": "law"}, n=50   20.5 ms   yields 50   (this)
            no filter, n=150              4.6 ms    yields 4    starved
            no filter, n=500              16.7 ms   yields 11   starved
            no filter, n=1000             33.4 ms   yields 56   barely enough, slower

        Cheaper than the circular arm's old filter (60.1 ms) because `$eq` over a small
        subset is not the same query as `$in` over a large one. Separate collections would
        make this 1.9 ms and delete the whole trade-off.
        """
        try:
            results = collection.query(
                query_embeddings=embedding_backend.embed_queries([query]),
                n_results=self.CANDIDATE_COUNT,
                where={"kind": "law"},
                include=["metadatas", "documents", "distances"],
            )
        except Exception:
            logger.exception("ChromaDB law vector search failed — lexical arm only")
            return {}, {}

        return _collect_evidence(results, "document_id", self.EVIDENCE_K)

    def _law_scores(
        self, query: str, db: Session, query_tokens: list[str], expanded_tokens: list[str],
        doc_type: str | None = None,
    ) -> tuple[dict[str, float], dict[str, RegDocument], dict[str, list[MatchEvidence]]]:
        """RRF scores for the law corpus, fused exactly like the circular arms.

        Laws get no recency bonus: their listing dates are display metadata SBP fills with
        placeholders (see the plan's §1.1), so ranking on them would be noise.

        Returns ``(scores, documents, evidence)`` — the evidence being the matched
        chunks the vector arm found, carried through for the caller to render.
        """
        fts_ranks = self._law_fts_ranks(db, expanded_tokens)
        vector_ranks, evidence = self._law_vector_ranks(query)
        candidate_ids = set(fts_ranks) | set(vector_ranks)
        if not candidate_ids:
            return {}, {}, {}

        documents_query = db.query(RegDocument).filter(RegDocument.id.in_(candidate_ids))
        if doc_type and doc_type.strip():
            documents_query = documents_query.filter(RegDocument.doc_type == doc_type.strip())
        documents = {document.id: document for document in documents_query.all()}

        query_words = set(query_tokens)
        scores: dict[str, float] = {}
        for document_id, document in documents.items():
            score = 0.0
            if document_id in fts_ranks:
                score += 1.0 / (self.RRF_K + fts_ranks[document_id])
            if document_id in vector_ranks:
                score += 1.0 / (self.RRF_K + vector_ranks[document_id])
            title_words = set(tokenize(document.title or ""))
            score += len(query_words & title_words) * self.TITLE_MATCH_BONUS
            scores[document_id] = score
        return scores, documents, evidence

    def _evidence_filename(
        self, circular: Circular, evidence: MatchEvidence
    ) -> str | None:
        """The attachment filename a piece of evidence came from, if any."""
        if evidence.source_label:
            return evidence.source_label
        if not evidence.source_id:
            return None
        return next(
            (
                item.filename
                for item in circular.attachments
                if item.id == evidence.source_id
            ),
            None,
        )

    def _circular_result(
        self,
        circular: Circular,
        snippet_tokens: set[str],
        evidence_by_id: dict[str, list[MatchEvidence]],
    ) -> dict:
        """Build one circular search result (snippet + provenance of the match).

        The preview is cut from the chunk the vector arm matched, so the snippet, the
        `match_source` badge and the page citation all describe one passage. The old
        code re-derived the passage independently and then had to check whether the
        two agreed before it dared cite a page; they now agree by construction.

        `passages` carries *every* retained chunk whole, in retrieval order, for readers
        that can take more than one preview. Windowing is safe on prose and unsafe on a
        table: PDF extraction interleaves a table's columns, so a window centred on the
        query's words lands mid-row and can pair one row's label with the next row's
        figure. Whole chunks are the only form of a table that cannot mislead, so the
        preview and the passage list deliberately carry different things — a snippet for
        a human scanning results, and the passage itself for a reader that will quote it.
        """
        evidence_list = evidence_by_id.get(circular.id) or []
        passages = [
            {
                "text": item.text,
                "match_source": item.doc_type,
                "attachment_id": item.source_id,
                "attachment_filename": self._evidence_filename(circular, item),
                "source_page": item.page,
                "source_ref": item.source_ref,
            }
            for item in evidence_list
            if item.text
        ]
        chosen = choose_evidence(evidence_list, snippet_tokens)
        if chosen is not None:
            evidence, snippet = chosen
            return {
                "result_kind": "circular",
                "circular": circular,
                "snippet": snippet,
                "match_source": evidence.doc_type,
                "attachment_id": evidence.source_id,
                "attachment_filename": self._evidence_filename(circular, evidence),
                "source_ref": evidence.source_ref,
                "source_page": evidence.page,
                "passages": passages,
            }

        # No vector evidence: a lexical-only hit, or Chroma is unavailable. Fall back
        # to scanning the documents themselves. This path cannot cite a page — nothing
        # located the passage, so there is no page to name.
        snippet, source, attachment_id, filename = self._scan_documents(
            circular, snippet_tokens
        )
        return {
            "result_kind": "circular",
            "circular": circular,
            "snippet": snippet,
            "match_source": source,
            "attachment_id": attachment_id,
            "attachment_filename": filename,
            # Nothing located a passage, so there is none to hand over whole.
            "passages": [],
        }

    def _latest_laws(
        self, db: Session, limit: int, offset: int, doc_type: str | None = None
    ) -> tuple[list[dict], int]:
        """Empty-query browse for the law corpus: most recently captured first."""
        q_obj = db.query(RegDocument).filter(RegDocument.delisted_at.is_(None))
        if doc_type and doc_type.strip():
            q_obj = q_obj.filter(RegDocument.doc_type == doc_type.strip())
        total = q_obj.count()
        documents = (
            q_obj.order_by(RegDocument.first_seen_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return [self._law_result(document, set(), {}) for document in documents], total

    def _law_result(
        self,
        document: RegDocument,
        snippet_tokens: set[str],
        evidence_by_id: dict[str, list[MatchEvidence]],
    ) -> dict:
        """Build one law result. Same evidence rule as circulars.

        Laws need this more than circulars do, not less: a captured edition is a whole
        instrument — an FE Manual chapter runs to tens of thousands of words — so a
        preview cut by term density across the full text is picking from an enormous
        field. The matched chunk narrows that to the passage retrieval actually scored.
        """
        version = _searchable_law_version(document)
        chosen = choose_evidence(
            evidence_by_id.get(document.id) or [], snippet_tokens
        )
        if chosen is not None:
            evidence, snippet = chosen
            return {
                "result_kind": "law",
                "law": document,
                "version": version,
                "snippet": snippet,
                "source_ref": evidence.source_ref,
                "source_page": evidence.page,
            }
        return {
            "result_kind": "law",
            "law": document,
            "version": version,
            "snippet": make_preview(
                (version.content_text if version else "") or "", snippet_tokens
            ),
        }

    # ------------------------------------------------------------------
    # Reference-pattern search
    # ------------------------------------------------------------------

    @staticmethod
    def _search_by_reference(query: str, db: Session, limit: int) -> list[Circular]:
        """Return circulars whose reference field matches a reference pattern."""
        match = REFERENCE_PATTERN.search(query)
        if not match:
            return []

        dept_code  = match.group(1).upper()
        doc_type   = (match.group(2) or "").lower().strip()
        num_raw    = match.group(3).lstrip("0") or "0"  # "08" -> "8"
        year       = match.group(4)                      # None when omitted
        if not year:
            # Users and model tools often cite references as
            # "Circular No. 04 dated March 08, 2018" rather than "of 2018".
            year_match = re.search(r"\b(19\d{2}|20\d{2})\b", query)
            year = year_match.group(1) if year_match else None

        # Determine whether the query pins a specific type.
        # "circular letter" / "cir let" → must contain "letter"
        # plain "circular" / "cir"      → must NOT contain "letter"
        # no type token at all          → no type constraint
        is_letter  = bool(re.search(r"let", doc_type))
        is_plain   = bool(doc_type) and not is_letter   # explicitly said "circular" but not "letter"


        from sqlalchemy import or_, extract

        # Search broadly in SQL, then parse candidate references in Python.
        # A raw LIKE for "04" also matches "14" and "24".
        q_obj = db.query(Circular).filter(
            or_(
                Circular.reference.ilike(f"%{dept_code}%"),
                Circular.title.ilike(f"%{dept_code}%"),
            )
        )

        # Enforce document-type constraint so "Circular No. 08" ≠ "Circular Letter No. 08"
        if is_letter:
            q_obj = q_obj.filter(
                or_(
                    Circular.reference.ilike("%letter%"),
                    Circular.title.ilike("%letter%"),
                )
            )
        elif is_plain:
            q_obj = q_obj.filter(
                ~Circular.reference.ilike("%letter%"),
                ~Circular.title.ilike("%letter%"),
            )

        # The year the user cites is the one in the *reference number*, which is not
        # always the year SBP published in: EDMD's 2002-2004 circulars all carry a
        # backfilled 2001-03-31 date, and a circular numbered "of 2011" can be dated
        # January 2012. Admit a candidate on either signal here and let
        # reference_matches() settle which one actually governs.
        if year:
            q_obj = q_obj.filter(
                or_(
                    Circular.reference.ilike(f"%{year}%"),
                    extract("year", Circular.date) == int(year),
                )
            )

        candidates = q_obj.order_by(Circular.date.desc()).limit(max(limit * 20, 100)).all()

        def reference_matches(text: str | None) -> tuple[bool, bool]:
            """Return ``(dated, undated)`` over the occurrences in `text` that match
            the query's department, number and document type.

            `dated` — an occurrence spelled its own year and it is the year asked for.
            `undated` — an occurrence carried no year, so only the circular's date can
            settle it. An occurrence whose year *disagrees* names a different circular
            and sets neither flag.

            The year has to come from the same occurrence that matched the number;
            reading it from anywhere in the string would let an unrelated year in the
            title decide, and titles like "Constitution Petition No.57 Of 2016" carry
            years of their own.
            """
            dated = undated = False
            for candidate in REFERENCE_PATTERN.finditer(text or ""):
                candidate_dept = candidate.group(1).upper()
                candidate_type = (candidate.group(2) or "").lower().strip()
                candidate_num = candidate.group(3).lstrip("0") or "0"
                candidate_year = candidate.group(4)
                candidate_is_letter = bool(re.search(r"let", candidate_type))
                candidate_is_plain = bool(candidate_type) and not candidate_is_letter
                if candidate_dept != dept_code or candidate_num != num_raw:
                    continue
                if is_letter and not candidate_is_letter:
                    continue
                if is_plain and candidate_is_letter:
                    continue
                if candidate_year is None:
                    undated = True
                elif year is None or candidate_year == year:
                    dated = True
            return dated, undated

        def is_match(circular: Circular) -> bool:
            reference_dated, reference_undated = reference_matches(circular.reference)
            title_dated, title_undated = reference_matches(circular.title)
            if reference_dated or title_dated:
                return True
            if not (reference_undated or title_undated):
                return False
            # The reference number gave no year of its own — fall back to the date,
            # which is what references like "FD Circular Letter No. 08 / 2018" need.
            if year is None:
                return True
            return circular.date is not None and circular.date.year == int(year)

        return [circular for circular in candidates if is_match(circular)][:limit]

    # ------------------------------------------------------------------
    # Snippet fallback (no vector evidence)
    # ------------------------------------------------------------------

    def _scan_documents(
        self, circular: Circular, query_tokens: set[str],
    ) -> tuple[str, str, str | None, str | None]:
        """Pick a document by token overlap and preview it. Fallback only.

        Used when the vector arm returned nothing for this circular — a lexical-only
        hit, or Chroma being down. It carries the known weakness of scanning whole
        documents (a long attachment's densest window is often prose near the answer
        rather than the answer), which is why it is no longer the primary path.
        """
        candidates: list[tuple[str, str, str | None, str | None]] = [
            (circular.content_text or "", "circular", None, None)
        ]
        candidates.extend(
            (item.content_text or "", "attachment", item.id, item.filename)
            for item in circular.attachments
            if item.content_text
        )

        def score(candidate: tuple[str, str, str | None, str | None]) -> int:
            candidate_tokens = tokenize(candidate[0])
            return sum(token in query_tokens for token in candidate_tokens)

        text, source, attachment_id, filename = max(candidates, key=score)
        return (
            make_preview(text, query_tokens),
            source,
            attachment_id,
            filename,
        )

    # ------------------------------------------------------------------
    # Unfused retrieval (chat)
    # ------------------------------------------------------------------
    def dual_arm_search(
        self,
        query: str,
        db: Session,
        *,
        limit: int = 10,
        department: str | None = None,
        tag: str | None = None,
        start_year: int | None = None,
        end_year: int | None = None,
    ) -> dict[str, list[dict]]:
        """Return the lexical and semantic arms separately, unfused — for chat.

        ``search()`` fuses the two retrievers with RRF and then adds title/recency
        bonuses. That is right for the search UI, where a human scans titles and dates
        and does their own judging. It is wrong for a chat tool, because the bonuses are
        an order of magnitude larger than the entire RRF range: a two-word title match
        is +0.10 against a best-case +0.033 from both arms at rank 1. A circular that
        names the topic only in its body or an annexure therefore cannot outrank one
        that names it in the title, however much better the retrieval judged it to be.

        Handing the model both ranked lists keeps that signal intact and lets it decide,
        which is the one thing an LLM is better at than a scoring formula. Every entry
        carries both `lexical_rank` and `semantic_rank`, so agreement between the arms
        stays visible. Reference-pattern hits come back as their own list rather than as
        a score bonus, for the same reason.
        """
        empty: dict[str, list[dict]] = {
            "reference_matches": [], "lexical_results": [], "semantic_results": []
        }
        query_tokens = tokenize(query)
        if not query.strip() or not query_tokens:
            return empty

        expanded_tokens = expand_query_tokens(query_tokens)
        snippet_tokens = set(query_tokens) | set(expanded_tokens)

        ref_results = self._search_by_reference(query, db, limit)
        fts_ranks = self._fts_ranks(db, expanded_tokens)
        vector_ranks, vector_evidence = self._vector_ranks(query)

        candidate_ids = (
            set(fts_ranks) | set(vector_ranks) | {c.id for c in ref_results}
        )
        if not candidate_ids:
            return empty

        if start_year or end_year or department or tag:
            filtered = self._apply_circular_filters(
                db.query(Circular.id).filter(Circular.id.in_(candidate_ids)),
                start_year=start_year, end_year=end_year,
                department=department, tag=tag,
            )
            candidate_ids &= {row[0] for row in filtered.all()}
            if not candidate_ids:
                return empty

        id_to_circular = {
            c.id: c
            for c in db.query(Circular)
            .options(joinedload(Circular.attachments))
            .filter(Circular.id.in_(candidate_ids))
            .all()
        }

        def build(circular_id: str) -> dict:
            # Both lists share `vector_evidence`, so a circular appearing in each gets
            # the same passage quoted rather than two different ones.
            result = self._circular_result(
                id_to_circular[circular_id], snippet_tokens, vector_evidence,
            )
            result["lexical_rank"] = fts_ranks.get(circular_id)
            result["semantic_rank"] = vector_ranks.get(circular_id)
            return result

        def arm(ranks: dict[str, int]) -> list[dict]:
            ordered = sorted(
                (cid for cid in ranks if cid in id_to_circular),
                key=ranks.__getitem__,
            )
            return [build(cid) for cid in ordered[:limit]]

        return {
            "reference_matches": [
                build(c.id) for c in ref_results if c.id in id_to_circular
            ],
            "lexical_results": arm(fts_ranks),
            "semantic_results": arm(vector_ranks),
        }

    # ------------------------------------------------------------------
    # Main search (fused — search UI, browse, laws)
    # ------------------------------------------------------------------
    def search(
        self, query: str, db: Session, limit: int = 20,
        offset: int = 0,
        start_year: int | None = None,
        end_year: int | None = None,
        department: str | None = None,
        sort_by: str = "relevance",
        tag: str | None = None,
        source: str = "circulars",
        doc_type: str | None = None,
    ) -> tuple[list[dict], int]:
        """Hybrid search returning ``([{dict}, …], total_count)``.

        `source` selects the corpus: ``circulars`` (default — unchanged behavior),
        ``laws``, or ``all``. Every result carries `result_kind` so a caller can tell
        which shape it got: circular results keep their `circular` key, law results carry
        `law` plus the `version` whose text matched.

        The default stays `circulars` because rendering law results is frontend work this
        phase deliberately leaves out; flipping it is a one-line change once the SPA can
        badge them. `department`, `tag` and the year bounds are circular-only concepts and
        are simply not applied to the law arm; `doc_type` is the law-side filter.
        """
        include_circulars = source in ("circulars", "all")
        include_laws = source in ("laws", "all")
        if not include_circulars and not include_laws:
            raise ValueError(f"Unknown search source: {source!r}")

        def apply_filters(q_obj):
            return self._apply_circular_filters(
                q_obj,
                start_year=start_year,
                end_year=end_year,
                department=department,
                tag=tag,
            )

        query_tokens = tokenize(query)

        # Empty query → return the latest of whichever corpus was asked for
        if not query.strip() or not query_tokens:
            if not include_circulars:
                return self._latest_laws(db, limit, offset, doc_type)
            q_obj = db.query(Circular)
            q_obj = apply_filters(q_obj)
            total = q_obj.count()
            circulars = q_obj.order_by(Circular.date.desc()).offset(offset).limit(limit).all()
            return (
                [{"result_kind": "circular", "circular": c, "snippet": ""} for c in circulars],
                total,
            )

        if not include_circulars:
            expanded_tokens = expand_query_tokens(query_tokens)
            scores, documents, law_evidence = self._law_scores(
                query, db, query_tokens, expanded_tokens, doc_type
            )
            ordered_ids = sorted(scores, key=scores.__getitem__, reverse=True)
            total = len(ordered_ids)
            snippet_tokens = set(query_tokens) | set(expanded_tokens)
            return (
                [
                    self._law_result(
                        documents[document_id], snippet_tokens, law_evidence
                    )
                    for document_id in ordered_ids[offset:offset + limit]
                ],
                total,
            )

        # 1. Reference-pattern search (exact match)
        ref_results = self._search_by_reference(query, db, limit * 2)
        ref_ids: set[str] = {c.id for c in ref_results}

        # 2. FTS5 lexical arm with synonym-expanded query
        expanded_tokens = expand_query_tokens(query_tokens)
        bm25_ranks = self._fts_ranks(db, expanded_tokens)

        # 3. Vector search (use original query — embeddings handle semantics)
        vector_ranks, vector_evidence = self._vector_ranks(query)

        # 4. Reciprocal Rank Fusion + bonuses
        all_candidate_ids = (
            set(bm25_ranks.keys()) | set(vector_ranks.keys()) | ref_ids
        )

        # Apply filters to candidates before sorting
        if start_year or end_year or department or tag:
            q_obj = db.query(Circular.id).filter(Circular.id.in_(all_candidate_ids))
            q_obj = apply_filters(q_obj)
            valid_ids = {r[0] for r in q_obj.all()}
            all_candidate_ids &= valid_ids

        # Fetch candidate circulars once — used for bonuses, sorting, and snippets.
        # (The lexical arm no longer keeps title/department/date in memory.)
        circulars = (
            db.query(Circular)
            .options(joinedload(Circular.attachments))
            .filter(Circular.id.in_(all_candidate_ids))
            .all()
        )
        id_to_circular = {c.id: c for c in circulars}

        rrf_scores: dict[str, float] = {}
        query_words = set(query_tokens)
        now = datetime.now()

        for cid in all_candidate_ids:
            score = 0.0

            # RRF from the FTS5 lexical arm
            if cid in bm25_ranks:
                score += 1.0 / (self.RRF_K + bm25_ranks[cid])

            # RRF from vector search
            if cid in vector_ranks:
                score += 1.0 / (self.RRF_K + vector_ranks[cid])

            # Reference exact-match bonus
            if cid in ref_ids:
                score += self.REFERENCE_BONUS

            # Title / department word-overlap + recency bonuses
            c = id_to_circular.get(cid)
            if c is not None:
                title_words = set(tokenize(c.title or ""))
                dept_words = set(tokenize(c.department or ""))
                score += len(query_words & title_words) * self.TITLE_MATCH_BONUS
                score += len(query_words & dept_words) * self.DEPT_MATCH_BONUS

                if c.date:
                    age_years = max((now - c.date).days / 365.25, 0)
                    score += self.RECENCY_WEIGHT / (1 + age_years)

            rrf_scores[cid] = score

        # 5. Sort — with laws merged in when asked for. Both corpora produce RRF scores
        # on the same scale and constant, so one sorted list is meaningful.
        if include_laws:
            law_scores, law_documents, law_evidence = self._law_scores(
                query, db, query_tokens, expanded_tokens, doc_type
            )
            merged = [("circular", cid, score) for cid, score in rrf_scores.items()]
            merged += [("law", did, score) for did, score in law_scores.items()]
            if sort_by == "date":
                merged.sort(
                    key=lambda item: _result_sort_date(
                        id_to_circular.get(item[1]) if item[0] == "circular"
                        else law_documents.get(item[1])
                    ),
                    reverse=True,
                )
            else:
                merged.sort(key=lambda item: item[2], reverse=True)
            total = len(merged)
            snippet_tokens = set(query_tokens) | set(expanded_tokens)
            ordered = []
            for kind, item_id, _score in merged[offset:offset + limit]:
                if kind == "law":
                    ordered.append(
                        self._law_result(
                            law_documents[item_id], snippet_tokens, law_evidence
                        )
                    )
                    continue
                circular = id_to_circular.get(item_id)
                if circular is not None:
                    ordered.append(
                        self._circular_result(circular, snippet_tokens, vector_evidence)
                    )
            return ordered, total

        if sort_by == "date":
            # Sort valid candidates by date
            sorted_circulars = sorted(
                circulars,
                key=lambda c: c.date.timestamp() if c.date else 0,
                reverse=True
            )
            total = len(sorted_circulars)
            sorted_ids = [c.id for c in sorted_circulars[offset:offset + limit]]
        else:
            sorted_ids = sorted(
                rrf_scores, key=rrf_scores.__getitem__, reverse=True,
            )
            total = len(sorted_ids)
            sorted_ids = sorted_ids[offset:offset + limit]

        if not sorted_ids:
            return [], total

        # 6. Generate snippets
        snippet_tokens = set(query_tokens) | set(expanded_tokens)
        ordered: list[dict] = []
        for cid in sorted_ids:
            c = id_to_circular.get(cid)
            if c:
                ordered.append(
                    self._circular_result(c, snippet_tokens, vector_evidence)
                )

        return ordered, total


search_engine = SearchEngine()
