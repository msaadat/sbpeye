import re
import logging
from collections.abc import Iterable
from datetime import datetime
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
    RRF_K = 60                     # RRF damping constant
    TITLE_MATCH_BONUS = 0.05       # per-word title overlap bonus
    DEPT_MATCH_BONUS = 0.02        # per-word department overlap bonus
    RECENCY_WEIGHT = 0.008         # recency decay weight
    REFERENCE_BONUS = 0.5          # bonus for exact reference matches
    SNIPPET_WINDOW = 25            # words in snippet window

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

    def _vector_ranks(
        self, query: str
    ) -> tuple[dict[str, int], dict[str, str], dict[str, dict]]:
        """Rank circulars via Chroma. ``({circular_id: rank}, sources, references)``.

        Chunks are collapsed to their parent circular, keeping each circular's best
        chunk: `sources` records which attachment that chunk came from (so snippets
        quote the passage that actually matched) and `references` its page/ref
        provenance. A vector-store failure degrades to an empty arm rather than
        taking the whole search down.
        """
        vector_ranks: dict[str, int] = {}
        vector_sources: dict[str, str] = {}
        vector_references: dict[str, dict] = {}
        try:
            query_embeddings = embedding_backend.embed_queries([query])
            results = collection.query(
                query_embeddings=query_embeddings,
                n_results=self.CANDIDATE_COUNT,
                # Laws share the collection; without this their chunks would enter the
                # circular candidate set as ids that resolve to no circular, silently
                # displacing real hits. Every circular/attachment chunk carries doc_type.
                where={"doc_type": {"$in": ["circular", "attachment"]}},
            )
            raw_ids = results["ids"][0] if results["ids"] else []
            raw_metas = (
                results["metadatas"][0] if results.get("metadatas") else []
            )

            # De-duplicate chunked results by circular_id
            rank_counter = 1
            for i, vid in enumerate(raw_ids):
                meta = raw_metas[i] if i < len(raw_metas) else {}
                circular_id = meta.get("circular_id", vid)
                if circular_id not in vector_ranks:
                    vector_ranks[circular_id] = rank_counter
                    if meta.get("ref"):
                        vector_references[circular_id] = {
                            "source_ref": meta.get("ref"),
                            "source_page": meta.get("page_start"),
                            "doc_type": meta.get("doc_type"),
                            "attachment_id": meta.get("attachment_id"),
                        }
                    if meta.get("doc_type") == "attachment" and meta.get("attachment_id"):
                        vector_sources[circular_id] = meta["attachment_id"]
                    rank_counter += 1
        except Exception:
            logger.exception(
                "ChromaDB vector search failed — falling back to BM25-only"
            )
        return vector_ranks, vector_sources, vector_references

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

    def _law_vector_ranks(self, query: str) -> dict[str, int]:
        """Rank laws via Chroma, restricted to law chunks. ``{document_id: rank}``."""
        ranks: dict[str, int] = {}
        try:
            results = collection.query(
                query_embeddings=embedding_backend.embed_queries([query]),
                n_results=self.CANDIDATE_COUNT,
                where={"kind": "law"},
            )
        except Exception:
            logger.exception("ChromaDB law vector search failed — lexical arm only")
            return ranks

        raw_ids = results["ids"][0] if results["ids"] else []
        raw_metas = results["metadatas"][0] if results.get("metadatas") else []
        rank_counter = 1
        for index, chunk_id in enumerate(raw_ids):
            meta = raw_metas[index] if index < len(raw_metas) else {}
            document_id = meta.get("document_id", chunk_id)
            if document_id not in ranks:
                ranks[document_id] = rank_counter
                rank_counter += 1
        return ranks

    def _law_scores(
        self, query: str, db: Session, query_tokens: list[str], expanded_tokens: list[str],
        doc_type: str | None = None,
    ) -> tuple[dict[str, float], dict[str, RegDocument]]:
        """RRF scores for the law corpus, fused exactly like the circular arms.

        Laws get no recency bonus: their listing dates are display metadata SBP fills with
        placeholders (see the plan's §1.1), so ranking on them would be noise.
        """
        fts_ranks = self._law_fts_ranks(db, expanded_tokens)
        vector_ranks = self._law_vector_ranks(query)
        candidate_ids = set(fts_ranks) | set(vector_ranks)
        if not candidate_ids:
            return {}, {}

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
        return scores, documents

    def _circular_result(
        self,
        circular: Circular,
        snippet_tokens: set[str],
        vector_sources: dict[str, str],
        vector_references: dict[str, dict],
    ) -> dict:
        """Build one circular search result (snippet + provenance of the match)."""
        snippet, source, attachment_id, filename = self._best_snippet_source(
            circular,
            snippet_tokens,
            preferred_attachment_id=vector_sources.get(circular.id),
        )
        reference = vector_references.get(circular.id, {})
        reference_matches_source = reference.get("doc_type") == source and (
            source != "attachment" or reference.get("attachment_id") == attachment_id
        )
        return {
            "result_kind": "circular",
            "circular": circular,
            "snippet": snippet,
            "match_source": source,
            "attachment_id": attachment_id,
            "attachment_filename": filename,
            **({
                "source_ref": reference.get("source_ref"),
                "source_page": reference.get("source_page"),
            } if reference_matches_source else {}),
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
        return [self._law_result(document, set()) for document in documents], total

    def _law_result(self, document: RegDocument, snippet_tokens: set[str]) -> dict:
        version = _searchable_law_version(document)
        return {
            "result_kind": "law",
            "law": document,
            "version": version,
            "snippet": self._generate_snippet(
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

        if year:
            q_obj = q_obj.filter(extract("year", Circular.date) == int(year))

        candidates = q_obj.order_by(Circular.date.desc()).limit(max(limit * 20, 100)).all()

        def reference_matches(text: str | None) -> bool:
            for candidate in REFERENCE_PATTERN.finditer(text or ""):
                candidate_dept = candidate.group(1).upper()
                candidate_type = (candidate.group(2) or "").lower().strip()
                candidate_num = candidate.group(3).lstrip("0") or "0"
                candidate_is_letter = bool(re.search(r"let", candidate_type))
                candidate_is_plain = bool(candidate_type) and not candidate_is_letter
                if candidate_dept != dept_code or candidate_num != num_raw:
                    continue
                if is_letter and not candidate_is_letter:
                    continue
                if is_plain and candidate_is_letter:
                    continue
                return True
            return False

        return [
            circular
            for circular in candidates
            if reference_matches(circular.reference) or reference_matches(circular.title)
        ][:limit]

    # ------------------------------------------------------------------
    # Snippet generation
    # ------------------------------------------------------------------

    def _generate_snippet(
        self, content: str, query_tokens: set[str], window: int = 0,
    ) -> str:
        """Find the most relevant passage and highlight matching terms."""
        if not content or not query_tokens:
            return ""

        window = window or self.SNIPPET_WINDOW
        words = content.split()
        if not words:
            return ""

        # Short documents — use entire text
        if len(words) <= window:
            snippet = content
        else:
            # Score each window position by query-term density
            words_lower = [
                re.sub(r"[^\w]", "", w).lower() for w in words
            ]
            best_score = -1
            best_pos = 0

            for i in range(len(words) - window + 1):
                score = sum(
                    1
                    for w in words_lower[i : i + window]
                    if any(qt in w for qt in query_tokens)
                )
                if score > best_score:
                    best_score = score
                    best_pos = i

            start = best_pos
            end = min(len(words), start + window)
            snippet = " ".join(words[start:end])

            if start > 0:
                snippet = "…" + snippet
            if end < len(words):
                snippet += "…"

        # Highlight matching terms (case-insensitive, match prefixes too)
        for token in query_tokens:
            if token.isalpha():
                # Matches dotted acronyms like T.T. or T.T or normal prefix like tt/ttbar
                dotted = r'\.?'.join(re.escape(c) for c in token) + r'\.?(?!\w)'
                pattern = rf"(?i)\b({dotted}|{re.escape(token)}\w*)"
            else:
                pattern = rf"(?i)\b({re.escape(token)}\w*)"
            snippet = re.sub(
                pattern,
                r"<mark>\1</mark>",
                snippet,
            )

        return snippet

    def _best_snippet_source(
        self,
        circular: Circular,
        query_tokens: set[str],
        preferred_attachment_id: str | None = None,
    ) -> tuple[str, str, str | None, str | None]:
        """Return snippet and source metadata for the strongest matching document."""
        if preferred_attachment_id:
            preferred = next(
                (
                    item
                    for item in circular.attachments
                    if item.id == preferred_attachment_id and item.content_text
                ),
                None,
            )
            if preferred:
                return (
                    self._generate_snippet(preferred.content_text, query_tokens),
                    "attachment",
                    preferred.id,
                    preferred.filename,
                )

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
            self._generate_snippet(text, query_tokens),
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
        vector_ranks, vector_sources, vector_references = self._vector_ranks(query)

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
            # Both lists share `vector_sources`, so a circular appearing in each gets
            # the same passage quoted rather than two different ones.
            result = self._circular_result(
                id_to_circular[circular_id], snippet_tokens,
                vector_sources, vector_references,
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
            scores, documents = self._law_scores(
                query, db, query_tokens, expanded_tokens, doc_type
            )
            ordered_ids = sorted(scores, key=scores.__getitem__, reverse=True)
            total = len(ordered_ids)
            snippet_tokens = set(query_tokens) | set(expanded_tokens)
            return (
                [
                    self._law_result(documents[document_id], snippet_tokens)
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
        vector_ranks, vector_sources, vector_references = self._vector_ranks(query)

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
            law_scores, law_documents = self._law_scores(
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
                    ordered.append(self._law_result(law_documents[item_id], snippet_tokens))
                    continue
                circular = id_to_circular.get(item_id)
                if circular is not None:
                    ordered.append(
                        self._circular_result(circular, snippet_tokens, vector_sources, vector_references)
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
                    self._circular_result(c, snippet_tokens, vector_sources, vector_references)
                )

        return ordered, total


search_engine = SearchEngine()
