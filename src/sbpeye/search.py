import re
import logging
from collections.abc import Iterable
from datetime import datetime
from sqlalchemy import text
from sqlalchemy.orm import Session, joinedload

from .models import Circular
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
    # Main search
    # ------------------------------------------------------------------
    def search(
        self, query: str, db: Session, limit: int = 20,
        offset: int = 0,
        start_year: int | None = None,
        end_year: int | None = None,
        department: str | None = None,
        sort_by: str = "relevance",
        tag: str | None = None,
    ) -> tuple[list[dict], int]:
        """Hybrid search returning ``([{dict}, …], total_count)``."""
        from sqlalchemy import extract, or_

        def apply_filters(q_obj):
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

        query_tokens = tokenize(query)

        # Empty query → return latest circulars with filters
        if not query.strip() or not query_tokens:
            q_obj = db.query(Circular)
            q_obj = apply_filters(q_obj)
            total = q_obj.count()
            circulars = q_obj.order_by(Circular.date.desc()).offset(offset).limit(limit).all()
            return [{"circular": c, "snippet": ""} for c in circulars], total

        # 1. Reference-pattern search (exact match)
        ref_results = self._search_by_reference(query, db, limit * 2)
        ref_ids: set[str] = {c.id for c in ref_results}

        # 2. FTS5 lexical arm with synonym-expanded query
        expanded_tokens = expand_query_tokens(query_tokens)
        bm25_ranks = self._fts_ranks(db, expanded_tokens)

        # 3. Vector search (use original query — embeddings handle semantics)
        vector_ranks: dict[str, int] = {}
        vector_sources: dict[str, str] = {}
        vector_references: dict[str, dict] = {}
        try:
            query_embeddings = embedding_backend.embed_queries([query])
            results = collection.query(
                query_embeddings=query_embeddings,
                n_results=self.CANDIDATE_COUNT,
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

        # 5. Sort

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
                snippet, source, attachment_id, filename = self._best_snippet_source(
                    c,
                    snippet_tokens,
                    preferred_attachment_id=vector_sources.get(cid),
                )
                reference = vector_references.get(cid, {})
                reference_matches_source = reference.get("doc_type") == source and (
                    source != "attachment"
                    or reference.get("attachment_id") == attachment_id
                )
                ordered.append({
                    "circular": c,
                    "snippet": snippet,
                    "match_source": source,
                    "attachment_id": attachment_id,
                    "attachment_filename": filename,
                    **({
                        "source_ref": reference.get("source_ref"),
                        "source_page": reference.get("source_page"),
                    } if reference_matches_source else {}),
                })

        return ordered, total


search_engine = SearchEngine()
