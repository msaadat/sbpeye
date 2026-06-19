import re
import logging
import threading
from datetime import datetime
from rank_bm25 import BM25Okapi
from sqlalchemy.orm import Session

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
    "ecl": ["expected credit loss"],
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
    "ecl": ["exchange company license"],
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
    "bcp": ["banking conduct prudential"],
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
    "bcp2": ["business continuity plan"],
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

    # Multi-word keys (bigrams) — e.g. "policy rate", "branchless banking"
    for i in range(len(tokens) - 1):
        bigram = f"{tokens[i]} {tokens[i+1]}"
        for synonym_phrase in SYNONYMS.get(bigram, []):
            for w in tokenize(synonym_phrase):
                if w not in seen:
                    expanded.append(w)
                    seen.add(w)

    return expanded


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

    def __init__(self):
        self._lock = threading.Lock()
        self._dirty = True
        self._ids: list[str] = []
        self._id_to_idx: dict[str, int] = {}
        self._bm25: BM25Okapi | None = None
        self._corpus_tokens: list[list[str]] = []
        self._titles: list[str] = []
        self._departments: list[str] = []
        self._dates: list[datetime | None] = []

    def mark_dirty(self):
        with self._lock:
            self._dirty = True

    def _ensure_index(self, db: Session):
        with self._lock:
            if not self._dirty and self._bm25 is not None:
                return

            circulars = db.query(Circular).all()
            self._ids = []
            self._id_to_idx = {}
            self._corpus_tokens = []
            self._titles = []
            self._departments = []
            self._dates = []

            for i, c in enumerate(circulars):
                title_tokens = tokenize(c.title or "")
                body_tokens = tokenize(c.content_text or "")
                ref_tokens = tokenize(c.reference or "")

                # Preserve both padded and unpadded number forms from the reference
                # so BM25 matches "08" and "8" interchangeably
                extra_ref_tokens: list[str] = []
                for tok in ref_tokens:
                    if tok.isdigit():
                        extra_ref_tokens.append(tok.lstrip("0") or "0")
                        extra_ref_tokens.append(tok.zfill(2))

                # Boost title 3×, reference 5× (was 2×) so reference queries rank first
                boosted = title_tokens * 3 + (ref_tokens + extra_ref_tokens) * 5 + body_tokens

                self._ids.append(c.id)
                self._id_to_idx[c.id] = i
                self._corpus_tokens.append(boosted)
                self._titles.append(c.title or "")
                self._departments.append(c.department or "")
                self._dates.append(c.date)

            if self._corpus_tokens:
                self._bm25 = BM25Okapi(self._corpus_tokens)
            else:
                self._bm25 = None

            self._dirty = False

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

        # Build both padded and unpadded variants so "8" matches "08" and vice-versa
        num_padded = num_raw.zfill(2)

        # Determine whether the query pins a specific type.
        # "circular letter" / "cir let" → must contain "letter"
        # plain "circular" / "cir"      → must NOT contain "letter"
        # no type token at all          → no type constraint
        is_letter  = bool(re.search(r"let", doc_type))
        is_plain   = bool(doc_type) and not is_letter   # explicitly said "circular" but not "letter"


        from sqlalchemy import or_, extract

        # Search both `reference` and `title` columns to catch circulars that
        # store the reference only in the title.
        def _col_conditions(col):
            return [
                col.ilike(f"%{dept_code}%{num_raw}%"),
                col.ilike(f"%{dept_code}%{num_padded}%"),
            ]

        conditions = _col_conditions(Circular.reference) + _col_conditions(Circular.title)

        q_obj = db.query(Circular).filter(or_(*conditions))

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

        return q_obj.order_by(Circular.date.desc()).limit(limit).all()

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
        self._ensure_index(db)
        
        from sqlalchemy import extract, or_

        def apply_filters(q_obj):
            if start_year:
                q_obj = q_obj.filter(extract('year', Circular.date) >= start_year)
            if end_year:
                q_obj = q_obj.filter(extract('year', Circular.date) <= end_year)
            if department and department.strip():
                q_obj = q_obj.filter(Circular.department == department)
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

        # 2. BM25 with synonym-expanded query
        expanded_tokens = expand_query_tokens(query_tokens)

        bm25_ranks: dict[str, int] = {}
        if self._bm25 is not None:
            bm25_scores = self._bm25.get_scores(expanded_tokens)
            bm25_ranked = sorted(
                range(len(bm25_scores)),
                key=lambda i: bm25_scores[i],
                reverse=True,
            )[: self.CANDIDATE_COUNT]

            for rank, idx in enumerate(bm25_ranked):
                bm25_ranks[self._ids[idx]] = rank + 1

        # 3. Vector search (use original query — embeddings handle semantics)
        vector_ranks: dict[str, int] = {}
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

        rrf_scores: dict[str, float] = {}
        query_words = set(query_tokens)
        now = datetime.now()

        for cid in all_candidate_ids:
            score = 0.0

            # RRF from BM25
            if cid in bm25_ranks:
                score += 1.0 / (self.RRF_K + bm25_ranks[cid])

            # RRF from vector search
            if cid in vector_ranks:
                score += 1.0 / (self.RRF_K + vector_ranks[cid])

            # Reference exact-match bonus
            if cid in ref_ids:
                score += self.REFERENCE_BONUS

            # Title / department word-overlap bonuses
            idx = self._id_to_idx.get(cid)
            if idx is not None:
                title_words = set(tokenize(self._titles[idx]))
                dept_words = set(tokenize(self._departments[idx]))
                score += len(query_words & title_words) * self.TITLE_MATCH_BONUS
                score += len(query_words & dept_words) * self.DEPT_MATCH_BONUS

                # Recency boost — recent docs score slightly higher
                doc_date = self._dates[idx]
                if doc_date:
                    age_years = max((now - doc_date).days / 365.25, 0)
                    score += self.RECENCY_WEIGHT / (1 + age_years)

            rrf_scores[cid] = score

        # 5. Sort and retrieve
        circulars = (
            db.query(Circular).filter(Circular.id.in_(all_candidate_ids)).all()
        )
        id_to_circular = {c.id: c for c in circulars}

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
                snippet = self._generate_snippet(
                    c.content_text or "", snippet_tokens,
                )
                ordered.append({"circular": c, "snippet": snippet})

        return ordered, total


search_engine = SearchEngine()
