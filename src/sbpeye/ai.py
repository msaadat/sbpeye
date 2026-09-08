import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import zip_longest
from types import SimpleNamespace
from typing import Any

import requests
from openai import APIError, OpenAI

from sqlalchemy.orm import Session

from .chat_steps import build_step, failed_step
from .checklist import compact_required_checklist
from .citation_handles import CitationHandles, StreamExpander
from .database import AppSessionLocal
from .env import load_app_env, resolve_env_value
from .llm_debug import (
    bind_context,
    close_implicit_trace,
    emit_event,
    ensure_implicit_trace,
    exception_payload,
    finish_trace,
    to_jsonable,
    trace_operation,
)


load_app_env()


TAG_TAXONOMY = [
    "AML",
    "CFT",
    "KYC",
    "CDD",
    "EDD",
    "Sanctions",
    "Compliance",
    "Forex",
    "Remittance",
    "Exchange Rate",
    "Export",
    "Import",
    "Trade Finance",
    "LC",
    "Guarantees",
    "Prudential",
    "Capital Adequacy",
    "Liquidity",
    "Risk Management",
    "Corporate Governance",
    "Payment Systems",
    "Digital Banking",
    "RAAST",
    "RTGS",
    "Card Operations",
    "Consumer Protection",
    "Microfinance",
    "Islamic Banking",
    "Sukuk",
    "Reporting",
    "IT",
    "Cybersecurity",
    "Branch Licensing",
    "Penalty",
    "Interest Rate",
    "Monetary Policy",
    "Tax",
    "Housing Finance",
    "SME Finance",
    "Agriculture Credit",
    "Sustainable Finance",
    "Deposit Insurance",
    "Anti-Fraud",
    "Data Privacy",
    "Outsourcing",
    "Internal Audit",
    "Credit Risk",
    "Market Risk",
    "Operational Risk",
    "Treasury",
]


# Human-readable activity labels for the chat status stream. Keys must match the
# tool function names declared in ``TOOLS`` below.
TOOL_LABELS = {
    "search_selected_documents": "Searching selected documents",
    "search_corpus": "Searching circulars and laws",
    "get_latest_circulars": "Fetching latest circulars",
    "get_circular_details": "Reading circular details",
    "get_law_details": "Reading the law text",
    "read_attachment": "Reading the annexure",
    "query_regulatory_values": "Looking up regulatory values",
    "get_circulars_by_tag": "Browsing circulars by tag",
    "search_regulatory_inventory": "Taking inventory across every document",
}


# An inventory answer is a list, so per-row cost decides how complete it can be. 240
# characters is enough passage to reject a false match (the measured worst case is a
# street address matching "Centre") without spending the window on prose.
_INVENTORY_PASSAGE_CHARS = 240
# Only a backstop against a pathological corpus; the context budget is the real bound.
_INVENTORY_MAX_ROWS = 1000


def _inventory_locator(evidence) -> str:
    """Human-readable pointer into the source, per plan section 8.2.

    A page number only exists where the chunker found page markers, which is the PDF
    attachment path; circular HTML bodies carry character offsets instead.
    """
    if evidence is None:
        return ""
    if evidence.locator_kind == "page" and evidence.page_start is not None:
        return f"p.{evidence.page_start}"
    if evidence.locator_kind == "offset" and evidence.source_start is not None:
        return f"@{evidence.source_start}"
    return evidence.source_ref or ""


def tool_activity_label(name: str) -> str:
    """Friendly label for a tool call, falling back to a humanized name."""
    return TOOL_LABELS.get(name, name.replace("_", " ").strip().capitalize())


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_selected_documents",
            "description": "Search passages within the circulars currently selected for this chat, including their attachments. Use this to inspect full text, find exact requirements, or retrieve additional passages. The server enforces the selected-document scope.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "A focused question or search phrase for the selected documents"},
                    "limit": {"type": "integer", "description": "Number of passages to return (1-10)", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_corpus",
            "description": (
                "Search everything SBPEye holds — circulars AND the laws, Acts, regulations "
                "and guidelines corpus — by keyword, topic, department, or tag. This is the "
                "default search: use it for any question about a subject, rule, or topic, "
                "whether the answer turns out to sit in a circular or in an Act.\n"
                "Returns TWO independently ranked lists of CIRCULARS, deliberately not "
                "merged, plus `reference_matches` for any exact circular reference in the "
                "query, plus `law_results` for the laws corpus:\n"
                "- `lexical_results`: keyword/BM25 ranking. Favours circulars whose title and "
                "text repeat the query's words.\n"
                "- `semantic_results`: meaning-based ranking over passages, including the text "
                "of attached annexures and frameworks. Finds circulars that answer the question "
                "without using its words.\n"
                "Judge both lists yourself; neither is authoritative. Each entry carries "
                "`lexical_rank` and `semantic_rank`, so you can see which circulars both "
                "retrievers agreed on. A circular both arms returned, or one an earlier "
                "search in this conversation already returned, carries its text ONCE: the "
                "later entry is marked `duplicate_of_earlier_entry` and names in "
                "`text_provided_earlier` what the first entry gave you. That is a pointer "
                "upwards, not a missing document — scroll back for the text rather than "
                "fetching it again. A repeat entry that carries `matching_passages` with "
                "`passages_not_provided_earlier` is bringing you passages of that document "
                "no earlier entry did — a sharper query reached a different part of its "
                "annexure — so read them. Pay particular attention to a circular ranked highly by "
                "`semantic_results` whose title looks unrelated — that usually means the answer "
                "sits in an attachment rather than the covering letter, and consolidated "
                "frameworks that revise earlier limits often look like this. Call "
                "get_circular_details on it to read the full document set before concluding.\n"
                "`matching_passages` are the retrieved passages in full — quote from these. "
                "`matching_passage_excerpt`, where it appears instead, is a short window and "
                "is often not the passage that answers the question.\n"
                "`full_circular_text` is the circular's complete covering LETTER, not its "
                "complete content — quote it rather than spending a get_circular_details "
                "call to re-read the same letter. When `attachment_text_chars` is present "
                "the circular has annexures whose text is NOT in this result beyond the "
                "`matching_passages` shown. A letter that announces a change without stating "
                "its terms ('details are at Annexure', 'the Framework has been amended') is a "
                "pointer, not an answer: the operative figures live in the annexure, and "
                "revised limits usually arrive this way. Do not conclude from an older "
                "circular that states a figure outright over a newer one whose figure is in "
                "an annexure you have not read — call get_circular_details on the newer one "
                "first, or read_attachment when a passage or contents listing has already "
                "told you which page or section holds it. Repeating a search does not open "
                "an annexure; read_attachment does.\n"
                "CURRENCY. The ranked lists hold circulars that are still in force. One "
                "that matched but has been superseded or cancelled is moved to "
                "`withdrawn_matches` — citation, title, date and `superseded_by`, with no "
                "text — so you can still see it matched and open it if the question turns "
                "out to be about the old rule. It is demoted, never hidden: a circular you "
                "name outright always comes back in full under `reference_matches`, with "
                "its `replaced_by` naming what took its place.\n"
                "A result carrying `amended_by` is STILL THE RULE — something later "
                "changed part of it, and the entry names those circulars with their "
                "dates. Read the amender before quoting any figure, threshold or "
                "deadline from an amended circular: the base text still shows the old "
                "number, and nothing in it says so. `older_changes_not_shown` counts "
                "amendments beyond the three most recent. Never treat `amended` as a "
                "reason to discard a circular — most amendments add to a circular or "
                "clarify it rather than change what it requires.\n"
                "`law_results` is the statute and regulation corpus, ranked separately "
                "because a rank there is a rank among laws and cannot be compared with a "
                "rank among circulars. Each entry carries a `[[l:...]]` citation and short "
                "`passages`. A statute is far too long to return whole, so these passages "
                "are a pointer, not the provision: when the answer depends on what an Act "
                "actually says — its composition, its timelines, its thresholds — call "
                "get_law_details on it and quote from that. A circular that merely mentions "
                "an Act is not a source for what the Act requires."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms. Examples: 'TT remittance', 'foreign exchange rules', 'AML guidelines', 'KYC requirements'"},
                    "department": {"type": "string", "description": "Optional department name to filter by, e.g. 'BPRD', 'Exchange Policy'"},
                    "tag": {"type": "string", "description": "Optional tag to filter by, e.g. 'Remittance', 'Forex', 'AML'"},
                    "limit": {"type": "integer", "description": "Max results to return (1-50)", "default": 10}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_latest_circulars",
            "description": "Retrieve the most recent circulars from the database, optionally filtered by department or topic. Use this when the user asks for the latest or most recent circulars.",
            "parameters": {
                "type": "object",
                "properties": {
                    "department": {"type": "string", "description": "Optional department name to filter by"},
                    "limit": {"type": "integer", "description": "Number of circulars to return (1-20)", "default": 5}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_circular_details",
            "description": (
                "Fetch a specific circular by reference number or title: its covering "
                "letter in full, an attachment manifest, and passages from its annexures "
                "and attachments matched to `query` (or, without one, to the user's "
                "question). Use this when the user refers to a specific circular (e.g. "
                "'BPRD Circular No. 12 of 2023') or needs its complete document set. "
                "Passages this conversation has already shown you are listed as "
                "'already provided earlier' rather than repeated, so a second call with "
                "a sharper `query` returns what the first did not. To read a known page "
                "or paragraph of an attachment, use read_attachment instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "circular_reference": {"type": "string", "description": "The circular's reference number, e.g. 'BPRD Circular No. 12 of 2023' or title"},
                    "query": {"type": "string", "description": "What you need from inside its attachments, e.g. 'definition of stable retail deposits and their run-off rate'. Defaults to the user's question."}
                },
                "required": ["circular_reference"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_attachment",
            "description": (
                "Read inside one attachment of a circular — an annexure, framework, "
                "instructions or guidelines PDF behind a covering letter — the way "
                "get_law_details reads inside an Act. Use it whenever a search result "
                "shows `attachment_text_chars`, a contents listing, or a `[[a:...]]` "
                "citation and the answer is in the attachment rather than the letter: "
                "the letter announces the rule, the annexure states it.\n"
                "Give `circular_reference`, name the `attachment` when the circular has "
                "more than one (its filename or `[[a:...]]` citation), and ask for one of:\n"
                "- `page`: every chunk of that page, in order. Use when you have seen a "
                "page number — in a contents listing, or on a passage already returned.\n"
                "- `section`: a paragraph or section number as the document numbers it, "
                "e.g. '4.11', '3.4', 'Part 2'. Semantic search cannot find a number by "
                "meaning; this scans for the heading directly.\n"
                "- `query`: what you need, by meaning and keyword. Matches come back with "
                "the chunk either side, so a paragraph split across a chunk boundary "
                "arrives whole.\n"
                "Passages are whole index chunks with `chunk_index` and `page`; consecutive "
                "indexes are consecutive text. `pages` gives the attachment's page range."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "circular_reference": {"type": "string", "description": "The circular the attachment belongs to, e.g. 'BPRD Circular No. 08 of 2016'"},
                    "attachment": {"type": "string", "description": "Which attachment, by filename ('C8-Annex.pdf') or citation ('[[a:C8-Annex]]'). Optional when the circular has one attachment."},
                    "page": {"type": "integer", "description": "A page number of the attachment to return whole"},
                    "section": {"type": "string", "description": "A paragraph or section number to locate, e.g. '4.11', 'Part 2', 'B'"},
                    "query": {"type": "string", "description": "What you need from the attachment, e.g. 'stable retail deposits definition run-off rate'"},
                    "limit": {"type": "integer", "description": "Number of matched passages for `query` before neighbour expansion (1-10)", "default": 5}
                },
                "required": ["circular_reference"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_law_details",
            "description": (
                "Read inside one law, Act, regulation or guideline — the equivalent of "
                "get_circular_details for the statute corpus. Use it whenever an answer "
                "depends on what an Act or a set of Regulations actually says: statutory "
                "timelines, the composition of a body, definitions, thresholds set in "
                "primary legislation. get_circular_details CANNOT retrieve an Act — it "
                "searches circulars only, and asking it for one returns an unrelated "
                "circular that happens to mention the Act by name.\n"
                "Give `law_title` and a `query` describing what you need from it; the "
                "matched passages come back with the passages either side of them, so a "
                "provision split across a page boundary arrives whole. Give `section` "
                "instead when you already know the provision number — semantic search "
                "cannot find 'section 9D' by meaning.\n"
                "The result names the document it actually resolved to in "
                "`resolved_title`. Check it against what you asked for: a mismatch means "
                "the corpus does not hold the instrument you wanted, and you should say "
                "so rather than answer from whatever came back."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "law_title": {"type": "string", "description": "The law's title, e.g. 'State Bank of Pakistan Act, 1956', 'Payment Systems and Electronic Fund Transfers Act, 2007', 'Prudential Regulations for SME Financing'"},
                    "query": {"type": "string", "description": "What you need from inside it, e.g. 'composition and quorum of the Monetary Policy Committee'"},
                    "section": {"type": "string", "description": "Optional provision number to fetch directly, e.g. '9D', '36', 'R-6'. Use when you know it; semantic search cannot find a section by its number."},
                    "limit": {"type": "integer", "description": "Number of matched passages before neighbour expansion (1-10)", "default": 5}
                },
                "required": ["law_title"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_regulatory_values",
            "description": "Query the structured database of regulatory VALUES extracted from circulars — ratios (CAR, LCR, NSFR, Leverage Ratio), monetary thresholds (minimum paid-up capital, MCR, exposure limits), percentage limits, numeric limits, and deadlines. Use this for any quantitative question, e.g. 'what is the current minimum capital requirement for MFBs?', 'which circulars set a threshold above 10%?', 'what is the required CAR?'. Returns each value with its metric, normalized number, unit, comparator (min/max/exactly), subject it applies to, effective date, and a citation to the source circular.",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "description": "Metric name to match, e.g. 'CAR', 'LCR', 'Paid-up Capital', 'MCR' (substring match)"},
                    "subject": {"type": "string", "description": "Who the value applies to, e.g. 'MFB', 'locally incorporated banks' (substring match)"},
                    "entity_type": {"type": "string", "description": "Optional: ratio | monetary_threshold | percentage_limit | numeric_limit | deadline | effective_date"},
                    "unit": {"type": "string", "description": "Optional unit filter: '%', 'PKR', 'USD', 'times', 'days', 'months'"},
                    "comparator": {"type": "string", "description": "Optional: min, max, exactly, or range"},
                    "min_value": {"type": "number", "description": "Only return values whose normalized number is >= this (e.g. 10 with unit '%' for 'above 10%')"},
                    "max_value": {"type": "number", "description": "Only return values whose normalized number is <= this"},
                    "current_only": {"type": "boolean", "description": "If true, exclude superseded/cancelled circulars and keep only the latest value per metric+subject. Use for 'current' value questions."},
                    "limit": {"type": "integer", "description": "Max results (1-50)", "default": 20}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_circulars_by_tag",
            "description": "Retrieve all circulars that have a specific AI-generated tag. Use this when the user asks for circulars categorized under a specific topic like 'AML', 'Remittance', 'Forex', etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "The tag name, e.g. 'AML', 'Remittance', 'Forex', 'Trade Finance'"},
                    "limit": {"type": "integer", "description": "Number of circulars to return (1-50)", "default": 10}
                },
                "required": ["tag"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_regulatory_inventory",
            "description": (
                "Sweep EVERY circular and regulation for every document that "
                "mentions a subject, and return them as an inventory. Use this only for "
                "exhaustive 'list all / find every / which documents' questions, e.g. 'list all "
                "circulars that talk about AML' or 'which regulations mention contact centres'. "
                "It expands the subject into a full vocabulary (acronyms, spellings, plurals) "
                "and searches with no result cutoff, so it finds documents that mention the "
                "subject in passing, which ordinary search misses. It is much slower than "
                "search_corpus — prefer that tool for ordinary questions about a topic. "
                "Each row is a pointer, never a reading: the passage is one short excerpt, so "
                "to learn what a document says, call get_circular_details or get_law_details "
                "on it. "
                "IMPORTANT: results are UNREVIEWED candidates. Every returned document contains "
                "one of the search terms, but nothing has judged whether it genuinely discusses "
                "the subject, so some will be false matches (a term appearing in an address or "
                "a passing reference). Say so when presenting them, and use each item's passage "
                "to judge relevance yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The subject to take inventory of, e.g. 'anti-money laundering', 'responsibilities of internal audit', 'call centres'"},
                    "sources": {"type": "string", "description": "Which corpus to sweep: 'all', 'circulars', or 'laws'", "default": "all"},
                    "department": {"type": "string", "description": "Optional department filter, e.g. 'BPRD'"},
                    "start_year": {"type": "integer", "description": "Optional earliest circular year"},
                    "end_year": {"type": "integer", "description": "Optional latest circular year"},
                    "limit": {"type": "integer", "description": "Optional. Omit to receive every matching document that fits the context budget, which is what an inventory question needs. Set it only to deliberately sample."}
                },
                "required": ["query"]
            }
        }
    }
]

# Tools whose server-side implementation can only fail without pre-selected circulars.
# `search_selected_documents` returns {"error": "No circulars are selected for this chat"}
# and nothing else when the selection is empty — see `_execute_tool`.
_SELECTION_ONLY_TOOLS = frozenset({"search_selected_documents"})


def tools_for_turn(selected_circular_ids: list[str] | None) -> list[dict]:
    """The tool schema for one chat turn, minus what this turn cannot serve.

    `_chat_system_prompt` already refuses to name `search_selected_documents` outside the
    selected branch, and `test_no_prompt_advertises_a_tool_its_path_cannot_call` pins that:
    "each prompt offers only what that path can actually do". The schema was never held to
    the same rule. It was the module constant on both loop paths, so a turn with no
    selection described the tool to the model ("The server enforces the selected-document
    scope" — which reads as a capability), and the guard in `_execute_tool` then answered
    every call with an error.

    Measured on chat session `48655b06` (benchmark P14): the model called it at iteration 3
    of 5, got `{"error": "No circulars are selected for this chat"}`, and spent a fifth of
    the turn's tool budget on a call that could not have succeeded. It was still fetching
    useful provisions when the ceiling cut it off two iterations later. Withdrawing the
    schema is worth more than raising that ceiling: it returns a turn at no token cost,
    where another iteration re-sends the whole accumulated record to buy one.

    The guard in `_execute_tool` stays. A model can name a tool it was never given, and the
    scope check is what makes the selected-document scope a fact rather than a request.
    """
    if selected_circular_ids:
        return TOOLS
    return [
        tool for tool in TOOLS
        if (tool.get("function") or {}).get("name") not in _SELECTION_ONLY_TOOLS
    ]


@dataclass(frozen=True)
class ProviderDefinition:
    value: str
    label: str
    default_base_url: str
    api_key_env_vars: tuple[str, ...]
    default_model: str = "local-model"
    default_api_key: str = ""


PROVIDER_DEFINITIONS = {
    "lmstudio": ProviderDefinition(
        value="lmstudio",
        label="LM Studio (Local)",
        default_base_url="http://localhost:1234/v1",
        api_key_env_vars=("AI_API_KEY",),
        default_model="local-model",
        default_api_key="lm-studio",
    ),
    "openai": ProviderDefinition(
        value="openai",
        label="OpenAI",
        default_base_url="https://api.openai.com/v1",
        api_key_env_vars=("OPENAI_API_KEY", "AI_API_KEY"),
        default_model="gpt-4o-mini",
    ),
    "google": ProviderDefinition(
        value="google",
        label="Google Gemini",
        default_base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY", "AI_API_KEY"),
        default_model="gemini-2.0-flash",
    ),
    "ollama": ProviderDefinition(
        value="ollama",
        label="Ollama Cloud",
        default_base_url="https://ollama.com/api",
        api_key_env_vars=("OLLAMA_API_KEY", "AI_API_KEY"),
        default_model="gpt-oss:120b",
    ),
    "mistral": ProviderDefinition(
        value="mistral",
        label="Mistral AI",
        default_base_url="https://api.mistral.ai/v1",
        api_key_env_vars=("MISTRAL_API_KEY", "AI_API_KEY"),
        default_model="mistral-small-latest",
    ),
    "groq": ProviderDefinition(
        value="groq",
        label="Groq",
        default_base_url="https://api.groq.com/openai/v1",
        api_key_env_vars=("GROQ_API_KEY", "AI_API_KEY"),
        default_model="llama-3.1-8b-instant",
    ),
    "openrouter": ProviderDefinition(
        value="openrouter",
        label="OpenRouter",
        default_base_url="https://openrouter.ai/api/v1",
        api_key_env_vars=("OPENROUTER_API_KEY", "AI_API_KEY"),
        default_model="openai/gpt-4o-mini",
    ),
    "custom": ProviderDefinition(
        value="custom",
        label="Custom OpenAI-Compatible",
        default_base_url="http://localhost:1234/v1",
        api_key_env_vars=("AI_API_KEY",),
    ),
}


def normalize_provider(provider: str | None) -> str:
    value = (provider or "mistral").strip().lower()
    aliases = {
        "gemini": "google",
        "lm_studio": "lmstudio",
        "mistralai": "mistral",
        "mistral_ai": "mistral",
        "ollama_cloud": "ollama",
    }
    return aliases.get(value, value if value in PROVIDER_DEFINITIONS else "custom")


def get_provider_definition(provider: str | None) -> ProviderDefinition:
    return PROVIDER_DEFINITIONS[normalize_provider(provider)]


GENERIC_CHAT_ERROR = (
    "Sorry, something went wrong while generating a response. Please try again."
)

# The rules every chat path shares, held once because they used to be held three times.
#
# `_chat_system_prompt`'s two branches and `_tool_result_synthesis_messages` each carried
# their own copy in their own wording, and they had already drifted: the synthesis copy
# compressed all three citation rules into a single sentence and dropped the clause that
# says what to do with a source you have no handle for. That path is reached only when the
# tool loop hits its iteration ceiling — when retrieval is struggling — so the weakest copy
# of the contract governed the answers that could least afford it.
_CITATION_RULES = """CITATIONS
- Cite a source only with the exact short handle printed beside it in the context or in a
tool result: [[c:...]] for a circular, [[a:...]] for an attachment, [[l:...]] for a law.
Copy the handle character for character.
- A handle renders as a link showing the document's own name, so write "as required by
[[c:BPRD-CL-01-2021]]" rather than repeating the reference immediately beside it.
- Never write a document ID, never invent or adjust a handle, and never use a handle you
were not given. A source you have no handle for is named in prose and cited with nothing."""

# What the answer itself owes the reader. Every line is a defect from the 2026-08-23
# benchmark round — see docs/ANSWER_QUALITY_DEFECTS.md, where they are D9 (preamble),
# D7 (denial ordering), D13 (length), D10 (closing offers) and D8 (retrieval vocabulary).
# None of them were contract violations before this constant existed: there was no output
# contract, only citation and tool-routing rules, so the model filled the gap with its own
# chat-assistant defaults.
#
# Two lines are worded against the obvious phrasing, on purpose:
#
# * The denial rule is about *ordering*, not honesty. P09 corrected a false premise after
#   summarising it, under a heading naming a circular that does not exist. The correction
#   was already right; it was simply below the point where a reader stops.
# * The offer rule does not say "do not offer". P12 offered to fetch Regulation R-6 when
#   R-6 was a required part of the answer, so suppressing the offer on its own would trade
#   a visible gap for an invisible one. Fetch it, or declare it missing.
_ANSWER_CONTRACT = """ANSWERING
- Open with the answer. No preamble about what you are about to do, what you have found, or
how you traced it.
- If the user names an instrument that does not exist, say so in your first sentence —
before any summary, and before any heading. If a real document is probably meant, name it
after the denial, never before. Never put a reference you have not verified in a heading.
- Match the answer to the question asked. A definitional question gets a definition; do not
annex adjacent topics the user did not ask about.
- Do not close by offering to look something up. If you can name a provision worth fetching,
fetch it and include it. If you cannot fetch it, say what is missing and why — an unfetched
provision is a gap to declare, not a service to offer.
- Say plainly when you could not find something; that disclosure must survive. Change only
the vocabulary: write "the provisions available to me do not include X", not "the retrieved
passage does not provide", "the lookup results", or "my database"."""


class MissingUserAIConfig(RuntimeError):
    """This user has not supplied provider credentials of their own."""


def friendly_chat_error(exc: Exception) -> str:
    """Translate a provider/SDK exception into a clear, user-facing message.

    Always returns a non-empty string so callers can surface it directly. Raw
    provider payloads (status codes, JSON dumps, stack traces) are never exposed.
    """
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()

    def has(*needles: str) -> bool:
        return any(needle in text for needle in needles)

    # Not a provider failure at all: this user has no credentials of their own. Handled
    # first so it never falls through to the generic "provider is unreachable" message,
    # which would send someone debugging a network problem they do not have.
    if isinstance(exc, MissingUserAIConfig):
        return str(exc)

    # The provider was reachable and accepted the request but sent back no completion.
    # Measured as intermittent on OpenRouter free tiers, so "try again" is genuinely
    # the right advice here and the generic message would bury it.
    if isinstance(exc, ProviderResponseError):
        return (
            "The AI provider accepted the request but returned an empty response. "
            "This is usually temporary — please try again."
        )

    # Request too large / rate limited / context window exceeded.
    if status in (413, 429) or has(
        "rate_limit_exceeded",
        "request too large",
        "tokens per minute",
        "requests per minute",
        "context_length_exceeded",
        "maximum context length",
        "reduce your message size",
    ):
        return (
            "This request was too large for the selected model. The provider "
            "rejected it because the conversation plus the selected circulars' "
            "context exceeded its token/rate limit. Try selecting fewer "
            "circulars (or ones with smaller attachments), or ask a more "
            "specific question."
        )

    # Authentication / authorization problems.
    if status in (401, 403) or has(
        "invalid api key",
        "incorrect api key",
        "authentication",
        "unauthorized",
        "permission",
    ):
        return (
            "The AI provider rejected the request due to an authentication "
            "problem. Check that a valid API key is configured for the selected "
            "provider in Settings."
        )

    # Model not found / not available.
    if status == 404 or has(
        "model not found",
        "does not exist",
        "no such model",
        "model_not_found",
    ):
        return (
            "The configured chat model could not be found at the AI provider. "
            "Verify the model name in Settings."
        )

    # Network / connection / timeout issues reaching the provider.
    if exc.__class__.__name__ in {"APIConnectionError", "APITimeoutError"} or has(
        "connection error",
        "connection refused",
        "timed out",
        "timeout",
        "failed to establish",
        "name resolution",
        "max retries",
    ):
        return (
            "Could not reach the AI provider. Check your network connection and "
            "that the provider's base URL is correct in Settings, then try again."
        )

    # Provider-side server errors.
    if (isinstance(status, int) and status >= 500) or has(
        "internal server error",
        "service unavailable",
        "bad gateway",
        "overloaded",
    ):
        return (
            "The AI provider is temporarily unavailable or overloaded. Please "
            "wait a moment and try again."
        )

    return GENERIC_CHAT_ERROR


def classify_provider_state(exc: Exception) -> tuple[str, str]:
    """Map a provider/SDK exception to a coarse availability state and short detail.

    States: ``rate_limited``, ``auth_error``, ``not_found``, ``offline``,
    ``server_error``, ``error``. The detail is a short, user-facing phrase.
    """
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()

    def has(*needles: str) -> bool:
        return any(needle in text for needle in needles)

    if isinstance(exc, ProviderResponseError):
        return "server_error", "Provider returned an empty response"

    if status in (413, 429) or has(
        "rate_limit", "rate limit", "too many requests",
        "tokens per minute", "requests per minute", "quota",
    ):
        return "rate_limited", "Rate limited or quota exceeded"

    if status in (401, 403) or has(
        "invalid api key", "incorrect api key", "authentication",
        "unauthorized", "permission",
    ):
        return "auth_error", "Authentication failed — check API key"

    if status == 404 or has("model not found", "model_not_found", "no such model"):
        return "not_found", "Configured model not found"

    if exc.__class__.__name__ in {"APIConnectionError", "APITimeoutError"} or has(
        "connection error", "connection refused", "timed out", "timeout",
        "failed to establish", "name resolution", "max retries",
    ):
        return "offline", "Provider unreachable"

    if (isinstance(status, int) and status >= 500) or has(
        "internal server error", "service unavailable", "bad gateway", "overloaded",
    ):
        return "server_error", "Provider temporarily unavailable"

    return "error", "Provider check failed"


def is_rate_limit_error(exc: Exception) -> bool:
    """True if the exception is a provider 429 / rate-limit / quota rejection.

    The CLI uses this to abort batch LLM operations on the first 429 instead of
    sending the rest of the batch into the same limit. Handles both SDK errors
    (``status_code`` attribute) and ``requests`` HTTP errors (status on
    ``response``).
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return True
    text = str(exc).lower()
    return any(
        needle in text
        for needle in (
            "rate_limit", "rate limit", "too many requests",
            "requests per minute", "tokens per minute", "quota",
        )
    )


def get_provider_api_key(provider: str | None) -> tuple[str, str | None]:
    definition = get_provider_definition(provider)
    return resolve_env_value(
        *definition.api_key_env_vars,
        default=definition.default_api_key,
    )


class OllamaCloudClient:
    """Minimal OpenAI-shaped adapter for Ollama's native cloud API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 120.0,
        max_retries: int = 2,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._chat_completion_create))
        self.models = SimpleNamespace(list=self._models_list)

    def with_options(
        self,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> "OllamaCloudClient":
        return OllamaCloudClient(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout if timeout is None else timeout,
            max_retries=self.max_retries if max_retries is None else max_retries,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=self._headers(),
                    timeout=self.timeout,
                    **kwargs,
                )
                response.raise_for_status()
                return response
            except Exception as exc:
                last_exc = exc
                # Stop immediately on a 429 (rate exceeded) — retrying just walks
                # further into the provider's rate limit.
                if attempt >= self.max_retries or is_rate_limit_error(exc):
                    break
                time.sleep(0.25 * (attempt + 1))
        raise last_exc or RuntimeError("Ollama Cloud request failed")

    @staticmethod
    def _tool_call_to_namespace(tool_call: dict[str, Any], index: int = 0) -> SimpleNamespace:
        function = tool_call.get("function") or {}
        arguments = function.get("arguments", {})
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments)
        return SimpleNamespace(
            id=tool_call.get("id") or f"call_{index}",
            index=index,
            type=tool_call.get("type") or "function",
            function=SimpleNamespace(
                name=function.get("name") or "",
                arguments=arguments,
            ),
        )

    @classmethod
    def _message_to_namespace(cls, message: dict[str, Any]) -> SimpleNamespace:
        tool_calls = message.get("tool_calls") or []
        return SimpleNamespace(
            content=message.get("content") or "",
            tool_calls=[
                cls._tool_call_to_namespace(tool_call, index)
                for index, tool_call in enumerate(tool_calls)
            ],
        )

    @classmethod
    def _chunk_to_namespace(cls, body: dict[str, Any]) -> SimpleNamespace:
        message = body.get("message") or {}
        return SimpleNamespace(
            id=body.get("id"),
            model=body.get("model"),
            usage=body.get("usage"),
            raw_provider_json=body,
            choices=[
                SimpleNamespace(
                    delta=cls._message_to_namespace(message),
                )
            ]
        )

    @staticmethod
    def _normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = []
        for message in messages:
            item = dict(message)
            if isinstance(item.get("tool_calls"), list):
                converted = []
                for tool_call in item["tool_calls"]:
                    converted_call = dict(tool_call)
                    function = dict(converted_call.get("function") or {})
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        try:
                            function["arguments"] = json.loads(arguments)
                        except json.JSONDecodeError:
                            function["arguments"] = {}
                    converted_call["function"] = function
                    converted.append(converted_call)
                item["tool_calls"] = converted
            normalized.append(item)
        return normalized

    @staticmethod
    def _response_format(format_value: dict[str, Any] | None) -> Any:
        if not format_value:
            return None
        if format_value.get("type") == "json_schema":
            return (format_value.get("json_schema") or {}).get("schema")
        if format_value.get("type") == "json_object":
            return "json"
        return None

    def _chat_payload(self, **kwargs: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": kwargs["model"],
            "messages": self._normalize_messages(kwargs.get("messages", [])),
            "stream": bool(kwargs.get("stream", False)),
        }
        options: dict[str, Any] = {}
        if "temperature" in kwargs and kwargs["temperature"] is not None:
            options["temperature"] = kwargs["temperature"]
        if "max_tokens" in kwargs and kwargs["max_tokens"] is not None:
            options["num_predict"] = kwargs["max_tokens"]
        if options:
            payload["options"] = options
        if kwargs.get("tools"):
            payload["tools"] = kwargs["tools"]
        response_format = self._response_format(kwargs.get("response_format"))
        if response_format:
            payload["format"] = response_format
        return payload

    def _chat_completion_create(self, **kwargs: Any) -> Any:
        payload = self._chat_payload(**kwargs)
        response = self._request("POST", "/chat", json=payload, stream=payload["stream"])
        if payload["stream"]:
            return self._stream_chat(response)
        body = response.json()
        message = self._message_to_namespace(body.get("message") or {})
        return SimpleNamespace(
            id=body.get("id"), model=body.get("model") or payload.get("model"),
            usage=body.get("usage"), raw_provider_json=body,
            choices=[SimpleNamespace(
                message=message,
                finish_reason=body.get("done_reason") or ("stop" if body.get("done") else None),
            )],
        )

    def _stream_chat(self, response: requests.Response):
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            body = json.loads(line)
            yield self._chunk_to_namespace(body)

    def _models_list(self) -> SimpleNamespace:
        response = self._request("GET", "/tags")
        body = response.json()
        models = [
            SimpleNamespace(
                id=model.get("model") or model.get("name") or "",
                model=model.get("model") or model.get("name") or "",
                name=model.get("name") or model.get("model") or "",
            )
            for model in body.get("models", [])
        ]
        return SimpleNamespace(data=models)

@dataclass
class AIConfig:
    # Kept in step with PROVIDER_DEFINITIONS["mistral"]. Mistral rather than LM Studio
    # because LM Studio's default points at localhost:1234, which exists on a developer's
    # machine and nowhere else: a deployment falling back to it fails every call with a
    # connection error instead of the configuration error it actually is.
    provider: str = "mistral"
    base_url: str = "https://api.mistral.ai/v1"
    api_key: str = ""
    model: str = "mistral-small-latest"
    chat_model: str = ""
    max_context_tokens: int = 4000

    @property
    def effective_chat_model(self) -> str:
        return self.chat_model or self.model

    @staticmethod
    def for_user(user) -> "AIConfig | None":
        """This user's own provider configuration, or None if they have not set one.

        Returns None rather than falling back to the deployment config. The fallback is
        exactly what per-user keys exist to prevent: a tester who never sets a key would
        otherwise spend the admin's budget silently, which is the situation 7.5
        describes. "Not configured" has to be a state the caller can see and report.
        """
        from .auth import decrypt_secret

        api_key = decrypt_secret(getattr(user, "ai_api_key_encrypted", None))
        # Tested before normalizing: `normalize_provider("")` answers with the default
        # provider, so normalizing first would turn "never configured" into a usable
        # lmstudio config pointed at a localhost that does not exist in the cloud.
        chosen = (getattr(user, "ai_provider", "") or "").strip()
        if not chosen:
            return None
        provider = normalize_provider(chosen)
        definition = get_provider_definition(provider)
        # A local provider ships a placeholder credential (`default_api_key`); a hosted
        # one has none, and is unusable without a real key. Reporting that here beats a
        # 401 from the vendor halfway through a chat turn.
        if not definition.default_api_key and not api_key:
            return None
        return AIConfig(
            provider=provider,
            base_url=(getattr(user, "ai_base_url", "") or definition.default_base_url),
            api_key=api_key or definition.default_api_key,
            model=(getattr(user, "ai_model", "") or definition.default_model),
            chat_model=(getattr(user, "ai_chat_model", "") or ""),
        )

    @staticmethod
    def from_env() -> "AIConfig":
        provider = normalize_provider(os.getenv("AI_PROVIDER", "mistral"))
        definition = get_provider_definition(provider)
        api_key, _ = get_provider_api_key(provider)
        return AIConfig(
            provider=provider,
            base_url=os.getenv("AI_BASE_URL", definition.default_base_url),
            api_key=api_key,
            model=os.getenv("AI_MODEL", definition.default_model),
            chat_model=os.getenv("AI_CHAT_MODEL", ""),
            max_context_tokens=int(os.getenv("AI_MAX_CONTEXT_TOKENS", "4000")),
        )

    @staticmethod
    def from_db(db) -> "AIConfig | None":
        try:
            from .models import Settings
            rows = db.query(Settings).all()
            if not rows:
                return None
            kv = {r.key: r.value for r in rows}
            if "ai_provider" not in kv:
                return None
            provider = normalize_provider(kv.get("ai_provider", "mistral"))
            env_config = AIConfig.from_env()
            api_key, _ = get_provider_api_key(provider)
            return AIConfig(
                provider=provider,
                base_url=kv.get("ai_base_url", get_provider_definition(provider).default_base_url),
                api_key=api_key,
                model=kv.get("ai_model", env_config.model),
                chat_model=kv.get("ai_chat_model", ""),
                max_context_tokens=int(kv.get("ai_max_context_tokens", str(env_config.max_context_tokens))),
            )
        except Exception:
            return None

    def save_to_db(self, db):
        from .models import upsert_settings
        upsert_settings(db, {
            "ai_provider": normalize_provider(self.provider),
            "ai_base_url": self.base_url,
            "ai_model": self.model,
            "ai_chat_model": self.chat_model,
            "ai_max_context_tokens": str(self.max_context_tokens),
        })

    @classmethod
    def secret_state(cls, provider: str | None) -> dict[str, str | bool]:
        normalized = normalize_provider(provider)
        definition = get_provider_definition(normalized)
        api_key, env_var = get_provider_api_key(normalized)
        return {
            "provider": normalized,
            "api_key_configured": bool(env_var and api_key),
            "api_key_env_var": env_var or definition.api_key_env_vars[0],
        }


# Conservative context windows (tokens) used to size checklist extraction batches
# when the provider does not report a window. detect_context_window() overrides
# these for local servers (LM Studio / Ollama) that advertise context_length.
_PROVIDER_CONTEXT_WINDOW: dict[str, int] = {
    "openai": 128_000,
    "google": 1_000_000,
    "ollama": 32_768,
    "groq": 32_768,
    "mistral": 32_768,
    "openrouter": 32_768,
    "lmstudio": 8_192,
    "custom": 8_192,
}
_DEFAULT_CONTEXT_WINDOW = 8_192
# Fraction of the context window reserved for prompt input; the remainder covers
# the system prompt and the JSON response.
_CONTEXT_INPUT_FRACTION = 0.6
# Floor under the synthesis step's evidence budget, matching `resolve_context_budget`'s
# own 1,000-token floor. Keeps a provider that reports an implausibly small window from
# reducing a finished turn's evidence to nothing.
_SYNTHESIS_MIN_EVIDENCE_CHARS = 4_000
# The header the gathered tool results sit under in the synthesis turn. Named because
# `_SYNTHESIS_FRAMING` charges its length to the budget, and a literal in two places is how
# the ceiling quietly stops being the ceiling.
_SYNTHESIS_EVIDENCE_HEADER = "Source material already gathered:"
# Fixed framing around the synthesis evidence, charged to the budget so the ceiling holds.
_SYNTHESIS_FRAMING = (
    f"Selected circular context:\n\n\n\n{_SYNTHESIS_EVIDENCE_HEADER}\n\n"
)
# Structured-output capability tiers, strongest first.
_STRUCTURED_MODES = ("json_schema", "json_object", "text")
# Retries for a provider that answers 200 with no usable choice. Measured on
# OpenRouter's free tier (nvidia/nemotron-3-ultra): the same request succeeds at one
# moment and returns an empty body the next, at every structured-output tier.
_EMPTY_RESPONSE_RETRIES = 2
# Relationship extraction reads the whole cover letter: supersession clauses often sit
# at the very end, past the default max_context_tokens clip. 24k chars covers the
# longest cover letter observed in the DB while still bounding pathological inputs.
RELATIONSHIP_CONTEXT_CHARS = 24_000

# A search hit whose body is this short is handed over whole rather than as a 25-word
# preview. Most SBP circulars are two-page letters — the corpus median body is ~990
# chars and 4k covers 95% of them — so the preview is usually a lossy summary of
# something that would have fit anyway. It costs a follow-up get_circular_details
# round to recover, and the last tool round of a chat turn has no such round left.
SEARCH_INLINE_BODY_MAX_CHARS = 4_000
# Ceiling on the total body text inlined across one search response, so a limit=50
# call cannot turn a result list into a 100-document dump.
SEARCH_INLINE_BODY_BUDGET_CHARS = 40_000
# Ceiling on the law passages handed over across one search response. Smaller than the
# circular budget on purpose: a law hit is a pointer to open with get_law_details, not a
# reading of the instrument, and the two corpora share one context window.
LAW_SEARCH_PASSAGE_BUDGET_CHARS = 8_000

# A turn hands the model each document's text once. A later row for the same document —
# the other retrieval arm, or a subsequent search — keeps only what says *where it ranked*,
# and points at the row that carried the text.
#
# The duplication is not a rounding error. Measured over the traced turns, across 5,096,863
# characters of search output: 458,376 characters were the same circular serialized twice
# inside one response (both arms read the same `_inline_body_texts` / `_passage_sets`
# lookup, so the copies are byte-identical), and 552,709 more were the same circular
# returning in a later call of the same turn. Law passages repeat at 40.2%. Together,
# 19.8%. A duplicated row costs ~2,714 characters and carries nothing new; reduced to the
# keys below it costs ~260.
#
# These are allowlists rather than a list of things to strip, because the failure mode of
# a denylist here is silent: a field added to the payload later would start being
# duplicated again and nothing would say so.
_REPEAT_ROW_KEYS = (
    "title", "reference", "department", "date", "status", "citation",
    "lexical_rank", "semantic_rank",
)
_REPEAT_LAW_ROW_KEYS = (
    "title", "law_type", "part_label", "citation", "lexical_rank", "semantic_rank",
)
# The keys within those payloads that carry substantive document text, named in the
# pointer so the model can tell what it already has rather than only that it has
# something. `matching_passage_excerpt` is deliberately absent: it is a 25-word window on
# text the pointer already names, so a repeat row drops it like everything else but
# saying "you were given an excerpt" adds nothing to "you were given the letter".
_DOCUMENT_TEXT_KEYS = ("full_circular_text", "matching_passages", "passages")


def _passage_ledger_key(circular_id: str, passage: dict) -> str:
    """The turn-ledger identity of one search passage — `chat_retrieval.passage_key`.

    Imported lazily: `chat_retrieval` reaches `search`, which is fine, but this module
    is imported by the checklist and inventory paths that `chat_retrieval` also touches,
    and a module-level import here would close that loop.
    """
    from .chat_retrieval import passage_key

    return passage_key(
        passage.get("attachment_id") or circular_id,
        passage.get("chunk_index"),
        passage.get("text") or "",
    )

# Said once per response, not once per pointer. There can be a dozen withdrawn matches and
# the instruction is the same for all of them; repeating it is the pattern `_dedupe_repeat_row`
# exists to remove. Measured, the pointers themselves are ~190 characters against 2,814 for
# the full entries they replace.
_WITHDRAWN_MATCHES_NOTE = (
    "These matched the query but are no longer in force, so they are listed without their "
    "text. Do not answer from them. Open one with get_circular_details only if the question "
    "is about what a rule used to say; otherwise use `superseded_by` to find what replaced it."
)


def _withdrawn_section(pointers: list[dict]) -> dict:
    """The `withdrawn_matches` block, or nothing at all when there is none."""
    if not pointers:
        return {}
    return {
        "withdrawn_matches": pointers,
        "withdrawn_matches_note": _WITHDRAWN_MATCHES_NOTE,
    }
# Ceiling on the matched-chunk text handed over whole across one search response.
# Chunks average ~1.5k chars, so this covers the head of both arms — which is where
# a wrong passage does its damage, because that is what the model reads first.
SEARCH_PASSAGE_BUDGET_CHARS = 24_000
# One result's share of that. `SearchEngine.ATTACHMENT_EVIDENCE_K` plus neighbour
# expansion lets one annexure arrive with a dozen chunks; without a per-result ceiling
# the first result in an arm could spend the whole response on itself. Seven or eight
# index chunks — a hit, its neighbours, and a second run — is what a reader needs from
# one document in a *search* result; more than that is what `read_attachment` is for.
SEARCH_PASSAGES_PER_RESULT_CHARS = 7_000
# Everything one search response may hand back, across both corpora. Named because the
# turn budget below is expressed as a share of it: these three numbers are what a search
# was tuned to be worth reading when the window has room for all of it.
_SEARCH_RESPONSE_BUDGET_CHARS = (
    SEARCH_INLINE_BODY_BUDGET_CHARS
    + SEARCH_PASSAGE_BUDGET_CHARS
    + LAW_SEARCH_PASSAGE_BUDGET_CHARS
)

# Tool rounds one chat turn may take before it falls back to synthesis.
_MAX_TOOL_ITERATIONS = 5
# What the turn's input budget is divided between: one selected-circular context plus a
# tool result per round, all of which `_chat_impl` keeps for the rest of the turn.
_TURN_CONTRIBUTORS = _MAX_TOOL_ITERATIONS + 1
# A share is capped at what a whole search response was already tuned to be worth, so a
# million-token window does not turn one lookup into a corpus dump.
_TURN_SHARE_MAX_TOKENS = _SEARCH_RESPONSE_BUDGET_CHARS // 4
# The floor exists only for a provider that reports something implausible; it is set low
# enough that it never breaks the ceiling it is guarding. A floor of 1,500 was tried
# first and is what not to do: on an 8,192-token window it produced six shares of 1,500
# against an input budget of 4,915 — the floor alone guaranteeing the overflow this
# whole division exists to prevent, and on the smallest models, which are the ones that
# actually reject oversized requests. At 500 the floor binds only below a ~5,000-token
# window, where a five-round tool loop does not fit under any split.
_TURN_SHARE_MIN_TOKENS = 500

# Provider-reported context windows, cached process-wide. A chat request builds its own
# `AIClient`, so `AIClient._context_budget` never survives a turn and every budget the
# turn derives would otherwise pay a `models.list()` round trip first. Failures are
# cached too, and briefly: an unreachable provider should not be re-probed by every
# request, nor have its fallback window pinned in place for a quarter of an hour.
_WINDOW_CACHE_TTL_SECONDS = 900.0
_WINDOW_CACHE_FAILURE_TTL_SECONDS = 60.0
_WINDOW_CACHE_MISS = object()
_window_cache: dict[tuple[str, str, str, str], tuple[float, int | None]] = {}
_window_cache_lock = threading.Lock()


def _cached_context_window(key: tuple[str, str, str, str]) -> Any:
    """The cached window, ``None`` for a cached failure, ``_WINDOW_CACHE_MISS`` if unknown."""
    with _window_cache_lock:
        entry = _window_cache.get(key)
        if entry is None:
            return _WINDOW_CACHE_MISS
        expires_at, window = entry
        if expires_at < time.monotonic():
            del _window_cache[key]
            return _WINDOW_CACHE_MISS
        return window


def _store_context_window(key: tuple[str, str, str, str], window: int | None) -> None:
    ttl = (
        _WINDOW_CACHE_TTL_SECONDS if window is not None
        else _WINDOW_CACHE_FAILURE_TTL_SECONDS
    )
    with _window_cache_lock:
        _window_cache[key] = (time.monotonic() + ttl, window)


class ProviderResponseError(RuntimeError):
    """The provider answered, but with no completion in it."""


def _first_choice_message(response: Any) -> Any | None:
    """The first choice's message, or ``None`` when the provider returned no choice.

    Indexing ``choices[0]`` directly turns an empty body into
    ``TypeError: 'NoneType' object is not subscriptable``, which reaches the caller as
    an opaque crash rather than as the transient provider fault it actually is.

    Callers that need the whole message — the tool-calling loop reads ``tool_calls`` —
    use this; callers that only want text use :func:`_first_choice_content`.
    """
    choices = getattr(response, "choices", None) or []
    if not choices:
        return None
    return getattr(choices[0], "message", None)


def _first_choice_content(response: Any) -> str | None:
    """The completion text, or ``None`` when the provider returned no usable choice."""
    message = _first_choice_message(response)
    if message is None:
        return None
    # A reasoning model that spends its whole budget thinking returns content=None with
    # the text in `reasoning`. That is not an answer, so it counts as no choice.
    return getattr(message, "content", None)


def _empty_response_error(
    provider: str, model: str, detail: str = ""
) -> ProviderResponseError:
    suffix = f" ({detail})" if detail else ""
    return ProviderResponseError(
        f"{provider} returned no completion for {model}{suffix}"
    )


def _messages_with_handles(
    messages: list[dict[str, str]], handles: CitationHandles
) -> list[dict[str, str]]:
    """Replay a stored conversation with handles in place of ids.

    Only assistant turns carry citations, and only they are rewritten — a user's own words
    are left exactly as typed.
    """
    return [
        {**message, "content": handles.for_history(message.get("content"))}
        if message.get("role") == "assistant"
        else message
        for message in messages
    ]


def _report_dropped_citations(handles: CitationHandles) -> None:
    """Log citations the gate refused to render.

    A drop is a source the reader will not get, so it is worth an event even though the
    answer itself reads normally without it — silently correct output is exactly how the
    old dead-link bug stayed invisible for so long.
    """
    dropped = handles.take_dropped()
    if dropped:
        emit_event(
            "citation_drop",
            {"dropped": dropped, "count": len(dropped)},
            stage="chat.citations",
        )


def _expanded_answer(content: str, handles: CitationHandles) -> str:
    expanded = handles.expand(content)
    _report_dropped_citations(handles)
    return expanded


def _status_code(exc: Exception) -> int | None:
    """Best-effort HTTP status from OpenAI SDK or requests exceptions."""
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _is_response_format_error(exc: Exception) -> bool:
    """Heuristically detect that a model/provider rejected structured output.

    Covers explicit SDK BadRequestError messages as well as the varied 400/422
    responses local and third-party providers return when they do not support
    ``response_format`` / ``json_schema``.
    """
    text = str(exc).lower()
    keywords = (
        "response_format",
        "response format",
        "json_schema",
        "json schema",
        "structured output",
        "structured_output",
        "schema",
    )
    if any(keyword in text for keyword in keywords):
        return True
    status = _status_code(exc)
    if status in (400, 422) and ("format" in text or "not support" in text or "unsupported" in text):
        return True
    return False


def _is_context_size_error(exc: Exception) -> bool:
    """True when the model rejected the request for being too large (not a 429)."""
    if _status_code(exc) == 413:
        return True
    text = str(exc).lower()
    return any(
        needle in text
        for needle in (
            "context_length_exceeded",
            "maximum context length",
            "context window",
            "request too large",
            "reduce your message size",
            "too many tokens",
        )
    )


class AIClient:
    def __init__(self, config: AIConfig | None = None):
        if config is None:
            config = AIConfig.from_env()
        self.config = config
        self._client = self._create_client()
        # Negotiated lazily; capable providers start at strict schema, others at
        # json_object so weaker models avoid a guaranteed-to-fail first call.
        self._structured_mode = (
            "json_schema"
            if self.config.provider in {"openai", "google", "ollama"}
            else "json_object"
        )
        self._context_budget: int | None = None
        # Which text-bearing payload keys this turn has already sent, per document id.
        # Reset at the top of each chat loop; spent by `_withhold_repeated_text`. One
        # `AIClient` serves one request and therefore one turn, so instance scope is
        # turn scope — the same reasoning `_context_budget` above relies on.
        self._sent_text_keys: dict[str, list[str]] = {}
        # Which *passages* this turn has already sent, per circular id, keyed by
        # `chat_retrieval.passage_key` — the index's own chunk ids, so a passage from
        # `search_corpus`, `get_circular_details` and `read_attachment` is the same
        # passage under the same key. The text-key ledger above says *that* a document
        # was sent; this says *which parts*, which is what lets a later search hand
        # over the parts it has not. Same scope, same reset.
        self._sent_passages: dict[str, set[str]] = {}
        # One digested record per tool call this turn makes, in execution order, for
        # the route to persist beside the answer. Turn-scoped for the same reason as
        # the ledger above.
        self._turn_steps: list[dict] = []

    @property
    def turn_steps(self) -> list[dict]:
        """The research steps of the turn just run, oldest first."""
        return list(self._turn_steps)

    def _create_client(self) -> Any:
        if self.config.provider == "ollama":
            return OllamaCloudClient(
                base_url=self.config.base_url,
                api_key=self.config.api_key,
            )
        kwargs: dict[str, Any] = {
            "base_url": self.config.base_url,
            # The SDK refuses to construct without *some* credential, so an unset key
            # becomes a placeholder here rather than an exception at construction. The
            # result is a 401 from the provider, which `friendly_chat_error` turns into
            # an "check your API key" message — a far better report than a crash while
            # building the client. Callers that must not reach a provider keyless check
            # first: `AIConfig.for_user` returns None instead of a keyless config.
            "api_key": self.config.api_key or "not-configured",
            # Bound each request so a stuck connection cannot hang generation for
            # the SDK default (~10 min) and then retry. Matches OllamaCloudClient.
            "timeout": 120.0,
        }
        if self.config.provider == "openrouter":
            kwargs["default_headers"] = {"X-Title": "SBPEye"}
        return OpenAI(**kwargs)

    @staticmethod
    def _model_metadata(model: Any) -> dict[str, Any]:
        if isinstance(model, dict):
            return model
        if hasattr(model, "model_dump"):
            return model.model_dump()
        return {}

    def _window_cache_key(self) -> tuple[str, str, str, str]:
        return (
            self.config.provider,
            self.config.base_url,
            self.config.model,
            self.config.effective_chat_model,
        )

    def detect_context_window(self) -> int | None:
        """Return the smallest provider-reported window used by this config.

        Cached per (provider, base URL, model, chat model) — see `_window_cache`. The
        models the key names are the only inputs to the answer, so an admin who changes
        the model in Settings misses the cache and gets a fresh probe, which is what
        that route reports on.
        """
        if self.config.provider in {"openai", "google"}:
            return None
        cache_key = self._window_cache_key()
        cached = _cached_context_window(cache_key)
        if cached is not _WINDOW_CACHE_MISS:
            return cached
        try:
            response = self._client.with_options(timeout=5.0, max_retries=0).models.list()
        except Exception:
            _store_context_window(cache_key, None)
            return None

        windows: dict[str, int] = {}
        for model in response.data:
            metadata = self._model_metadata(model)
            model_id = str(metadata.get("id") or getattr(model, "id", ""))
            for key in ("context_window", "context_length", "max_context_length"):
                value = metadata.get(key)
                if isinstance(value, int) and value > 0:
                    windows[model_id] = value
                    break

        model_ids = {self.config.model, self.config.effective_chat_model}
        detected = [windows.get(model_id) for model_id in model_ids]
        if any(value is None for value in detected):
            _store_context_window(cache_key, None)
            return None
        window = min(value for value in detected if value is not None)
        _store_context_window(cache_key, window)
        return window

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Cheap token estimate (~4 chars/token) used only for batch sizing."""
        return max(1, len(text) // 4)

    def resolve_context_budget(self) -> int:
        """Return the per-call input token budget for checklist extraction."""
        if self._context_budget is not None:
            return self._context_budget
        window = self.detect_context_window()
        if not window:
            window = _PROVIDER_CONTEXT_WINDOW.get(self.config.provider, _DEFAULT_CONTEXT_WINDOW)
        budget = int(window * _CONTEXT_INPUT_FRACTION)
        self._context_budget = max(budget, 1_000)
        return self._context_budget

    def resolve_turn_share(self) -> int:
        """Input tokens one contributor to a chat turn may spend.

        A turn is assembled from the selected-circular context plus up to
        `_MAX_TOOL_ITERATIONS` tool results, and the loop keeps every one of them for the
        rest of the turn. Sizing a single contributor against the whole window therefore
        fits on the first round and overflows by the third, which is the shape of every
        oversized request in the trace log: iteration 1 around 3k tokens, iteration 5
        around 114k, against per-tool ceilings that never moved between them. Dividing
        the input budget by the number of contributors makes "the turn fits" arithmetic
        rather than hope — six shares is the whole of it, by construction.

        Clamped at both ends, for the reasons `_TURN_SHARE_MAX_TOKENS` and
        `_TURN_SHARE_MIN_TOKENS` record.
        """
        share = self.resolve_context_budget() // _TURN_CONTRIBUTORS
        return max(_TURN_SHARE_MIN_TOKENS, min(_TURN_SHARE_MAX_TOKENS, share))

    def _search_payload_budgets(self) -> tuple[int, int, int]:
        """One search response's character ceilings: (inline bodies, passages, laws).

        `search_corpus` is the largest single contributor to an oversized chat request:
        a measured ~75,000 characters (~19k tokens) per call, from constants that did
        not know what model they were talking to. That is 2.3x the entire window of an
        8k local model and more than half of a 32k one — five times over in a turn that
        searches five times.

        Below a share the three ceilings scale together, so the arms keep their tuned
        40:24:8 proportions instead of one of them absorbing the whole reduction. At or
        above a share they are handed back untouched: the reasoning behind each number
        does not stop applying because the window got bigger.
        """
        ceilings = (
            SEARCH_INLINE_BODY_BUDGET_CHARS,
            SEARCH_PASSAGE_BUDGET_CHARS,
            LAW_SEARCH_PASSAGE_BUDGET_CHARS,
        )
        allowance = self.resolve_turn_share() * 4
        if allowance >= _SEARCH_RESPONSE_BUDGET_CHARS:
            return ceilings
        scaled = [
            max(1, ceiling * allowance // _SEARCH_RESPONSE_BUDGET_CHARS)
            for ceiling in ceilings
        ]
        return scaled[0], scaled[1], scaled[2]

    def list_models(self) -> list[dict[str, str]]:
        """Return provider model IDs in a normalized shape for the settings UI."""
        response = self._client.with_options(timeout=10.0, max_retries=0).models.list()
        models: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in response.data:
            metadata = self._model_metadata(item)
            model_id = str(
                metadata.get("id")
                or metadata.get("model")
                or metadata.get("name")
                or getattr(item, "id", "")
                or getattr(item, "model", "")
                or getattr(item, "name", "")
            ).strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            label = str(
                metadata.get("name")
                or metadata.get("display_name")
                or getattr(item, "name", "")
                or model_id
            ).strip()
            models.append({"id": model_id, "name": label or model_id})
        return sorted(models, key=lambda model: model["id"].lower())

    @staticmethod
    def _downgrade_structured_mode(mode: str) -> str:
        index = _STRUCTURED_MODES.index(mode)
        return _STRUCTURED_MODES[min(index + 1, len(_STRUCTURED_MODES) - 1)]

    def _build_completion_kwargs(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        model: str,
        temperature: float,
        json_schema: dict[str, Any] | None,
        mode: str,
    ) -> dict[str, Any]:
        system = system_prompt
        kwargs: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
        }
        if json_schema and mode == "json_schema":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "sbpeye_response",
                    "strict": True,
                    "schema": json_schema,
                },
            }
        elif json_schema and mode == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        elif json_schema:
            # Plain-text mode: no provider enforcement, so ask explicitly.
            system = (
                system_prompt
                + "\n\nRespond with only a single valid JSON object and no other text."
            )
        kwargs["messages"] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ]
        return kwargs

    @staticmethod
    def _normalized_completion_response(response: Any) -> dict[str, Any]:
        """Return a stable view plus the provider's complete JSON when available."""
        raw = to_jsonable(response)
        choices = getattr(response, "choices", None) or []
        choice = choices[0] if choices else None
        message = getattr(choice, "message", None) if choice is not None else None
        tool_calls = getattr(message, "tool_calls", None) or [] if message else []
        usage = getattr(response, "usage", None)
        usage_data = to_jsonable(usage) if usage is not None else {}
        if not isinstance(usage_data, dict):
            usage_data = {}
        return {
            "id": getattr(response, "id", None),
            "model": getattr(response, "model", None),
            "content": getattr(message, "content", None) if message else None,
            "role": getattr(message, "role", None) if message else None,
            "tool_calls": to_jsonable(tool_calls),
            "finish_reason": getattr(choice, "finish_reason", None) if choice else None,
            "usage": usage_data,
            "raw": raw,
        }

    def _trace_stream(
        self,
        stream: Any,
        *,
        handle: Any,
        owns_trace: bool,
        context_token: Any,
        attempt_id: str,
        attempt_number: int | None,
        stage: str,
        started: float,
    ):
        content_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        role: str | None = None
        finish_reason: str | None = None
        response_id: str | None = None
        response_model: str | None = None
        usage: dict[str, Any] = {}
        first_delta_ms: int | None = None
        try:
            for chunk in stream:
                response_id = getattr(chunk, "id", None) or response_id
                response_model = getattr(chunk, "model", None) or response_model
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    converted = to_jsonable(chunk_usage)
                    if isinstance(converted, dict):
                        usage = converted
                choices = getattr(chunk, "choices", None) or []
                for choice in choices:
                    finish_reason = getattr(choice, "finish_reason", None) or finish_reason
                    delta = getattr(choice, "delta", None)
                    if delta is None:
                        continue
                    role = getattr(delta, "role", None) or role
                    content = getattr(delta, "content", None)
                    if content:
                        content_parts.append(content)
                        if first_delta_ms is None:
                            first_delta_ms = round((time.monotonic() - started) * 1000)
                    for tc in getattr(delta, "tool_calls", None) or []:
                        index = int(getattr(tc, "index", 0) or 0)
                        item = tool_calls.setdefault(index, {
                            "id": "", "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        item["id"] = getattr(tc, "id", None) or item["id"]
                        item["type"] = getattr(tc, "type", None) or item["type"]
                        function = getattr(tc, "function", None)
                        if function:
                            item["function"]["name"] += getattr(function, "name", None) or ""
                            item["function"]["arguments"] += getattr(function, "arguments", None) or ""
                            if first_delta_ms is None:
                                first_delta_ms = round((time.monotonic() - started) * 1000)
                yield chunk
            elapsed = round((time.monotonic() - started) * 1000)
            emit_event("provider_response", {
                "id": response_id, "model": response_model, "role": role,
                "content": "".join(content_parts),
                "tool_calls": [tool_calls[key] for key in sorted(tool_calls)],
                "finish_reason": finish_reason, "usage": usage,
                "stream": True, "first_token_ms": first_delta_ms,
                "duration_ms": elapsed,
            }, stage=stage, attempt_id=attempt_id, attempt_number=attempt_number,
               elapsed_ms=elapsed, handle=handle)
            close_implicit_trace(handle, owns_trace, context_token)
        except GeneratorExit as exc:
            elapsed = round((time.monotonic() - started) * 1000)
            emit_event("provider_response", {
                "content": "".join(content_parts), "stream": True,
                "abandoned": True, "duration_ms": elapsed,
            }, stage=stage, attempt_id=attempt_id, attempt_number=attempt_number,
               elapsed_ms=elapsed, handle=handle)
            close_implicit_trace(handle, owns_trace, context_token, error=exc, cancelled=True)
            raise
        except BaseException as exc:
            elapsed = round((time.monotonic() - started) * 1000)
            emit_event("provider_error", {
                **exception_payload(exc), "duration_ms": elapsed,
            }, stage=stage, attempt_id=attempt_id, attempt_number=attempt_number,
               elapsed_ms=elapsed, handle=handle)
            close_implicit_trace(handle, owns_trace, context_token, error=exc)
            raise

    def _create_traced_completion(
        self,
        *,
        stage: str,
        structured_mode: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """The sole text-generation provider boundary in SBPEye."""
        handle, owns_trace, context_token = ensure_implicit_trace(
            self.config.provider, str(kwargs.get("model") or "") or None
        )
        attempt_id = str(uuid.uuid4())
        attempt_number = emit_event("provider_request", {
            "provider": self.config.provider,
            "base_url": self.config.base_url,
            "structured_mode": structured_mode or "text",
            "kwargs": kwargs,
        }, stage=stage, attempt_id=attempt_id, handle=handle)
        started = time.monotonic()
        try:
            response = self._client.chat.completions.create(**kwargs)
        except BaseException as exc:
            elapsed = round((time.monotonic() - started) * 1000)
            emit_event("provider_error", {
                **exception_payload(exc), "duration_ms": elapsed,
            }, stage=stage, attempt_id=attempt_id, attempt_number=attempt_number,
               elapsed_ms=elapsed, handle=handle)
            close_implicit_trace(handle, owns_trace, context_token, error=exc)
            raise
        if kwargs.get("stream"):
            return self._trace_stream(
                response, handle=handle, owns_trace=owns_trace,
                context_token=context_token, attempt_id=attempt_id,
                attempt_number=attempt_number, stage=stage, started=started,
            )
        elapsed = round((time.monotonic() - started) * 1000)
        normalized = self._normalized_completion_response(response)
        normalized["duration_ms"] = elapsed
        normalized["stream"] = False
        emit_event("provider_response", normalized, stage=stage,
                   attempt_id=attempt_id, attempt_number=attempt_number,
                   elapsed_ms=elapsed, handle=handle)
        close_implicit_trace(handle, owns_trace, context_token)
        return response

    def _complete(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str = "",
        temperature: float = 0.0,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        with trace_operation(
            "implicit.completion", "implicit", provider=self.config.provider,
            model=model or self.config.model,
        ):
            return self._complete_impl(
                system_prompt, user_prompt, model=model, temperature=temperature,
                json_schema=json_schema,
            )

    def _complete_impl(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str = "",
        temperature: float = 0.0,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        model = model or self.config.model
        empty_responses = 0
        while True:
            mode = self._structured_mode if json_schema else "text"
            kwargs = self._build_completion_kwargs(
                system_prompt,
                user_prompt,
                model=model,
                temperature=temperature,
                json_schema=json_schema,
                mode=mode,
            )
            try:
                response = self._create_traced_completion(
                    stage="completion", structured_mode=mode, **kwargs
                )
            except Exception as exc:
                if json_schema and mode != "text" and _is_response_format_error(exc):
                    # Provider rejected this structured-output tier; drop down and
                    # cache so the rest of the run skips the dead mode.
                    new_mode = self._downgrade_structured_mode(mode)
                    emit_event("structured_mode_changed", {
                        "old_mode": mode, "new_mode": new_mode,
                        "reason": "provider_rejection",
                    }, stage="completion")
                    emit_event("retry", {
                        "reason": "response_format_rejected", "retry_tier": new_mode,
                    }, stage="completion")
                    self._structured_mode = new_mode
                    continue
                raise

            content = _first_choice_content(response)
            if content is not None:
                return content

            # Retry the *same* tier rather than downgrading. The tier cache is a
            # one-way ratchet, so feeding an intermittent fault into it would strand
            # the client in a weaker mode for the rest of the process on evidence that
            # says nothing about what the model supports.
            empty_responses += 1
            emit_event("retry", {
                "reason": "empty_completion", "attempt": empty_responses,
                "retry_tier": mode,
            }, stage="completion")
            if empty_responses > _EMPTY_RESPONSE_RETRIES:
                raise _empty_response_error(
                    self.config.provider,
                    model,
                    f"{empty_responses} attempts, mode={mode}",
                )

    def _complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_schema: dict[str, Any],
        temperature: float = 0.0,
    ) -> str:
        with trace_operation(
            "implicit.completion", "implicit", provider=self.config.provider,
            model=self.config.model,
        ):
            return self._complete_json_impl(
                system_prompt, user_prompt, json_schema=json_schema,
                temperature=temperature,
            )

    def _complete_json_impl(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_schema: dict[str, Any],
        temperature: float = 0.0,
    ) -> str:
        """Complete and return text that parses as a JSON object.

        Beyond the API-error downgrade in :meth:`_complete`, this handles models
        that accept the structured-output request but answer with prose anyway:
        on an unparseable result it downgrades the cached mode and retries once.
        """
        raw = self._complete(
            system_prompt, user_prompt, temperature=temperature, json_schema=json_schema
        )
        try:
            self._parse_json_object(raw)
            return raw
        except ValueError as exc:
            emit_event("parse_error", {
                "parser": "json_object", "error": str(exc),
            }, stage="structured_output")
            if self._structured_mode == "text":
                return raw
            old_mode = self._structured_mode
            self._structured_mode = self._downgrade_structured_mode(old_mode)
            emit_event("structured_mode_changed", {
                "old_mode": old_mode, "new_mode": self._structured_mode,
                "reason": "invalid_json",
            }, stage="structured_output")
            emit_event("retry", {
                "reason": "invalid_json", "retry_tier": self._structured_mode,
            }, stage="structured_output")
            return self._complete(
                system_prompt, user_prompt, temperature=temperature, json_schema=json_schema
            )

    def _complete_chat(self, messages: list[dict[str, str]], model: str = "", temperature: float = 0.3) -> str:
        model = model or self.config.effective_chat_model
        response = self._create_traced_completion(
            stage="chat.helper",
            model=model,
            messages=messages,
            temperature=temperature,
        )
        content = _first_choice_content(response)
        if content is None:
            raise _empty_response_error(self.config.provider, model)
        return content

    @staticmethod
    def _is_tool_choice_none_error(exc: Exception) -> bool:
        return "Tool choice is none, but model called a tool" in str(exc)

    @staticmethod
    def _tool_iteration_limit_message() -> str:
        return (
            "I could not complete the answer because the model kept requesting "
            "additional database tool calls after the lookup limit was reached. "
            "Please retry the question, or narrow it to the selected circulars."
        )

    def _synthesis_evidence_budget(self) -> int:
        """Characters of tool results the final synthesis step may carry.

        This used to be ``max(4000, max_context_tokens * 4)``, which was wrong at both
        ends. `max_context_tokens` is not a context window: `_truncate_context` clips a
        single document's *characters* with it, and `laws_ai` already notes the unit
        mismatch as pre-existing. Its 4,000 default belongs to that meaning; read as a
        token window it allowed 16,000 characters of evidence for a whole turn, which is
        what the 2026-08-23 round ran with — a ten-lookup turn got one lookup. And where
        a real window *had* been detected the same expression handed the entire window to
        tool results, leaving nothing for the system prompt, the conversation or the
        answer.

        `resolve_context_budget` is the existing answer to both: it takes the provider's
        reported window where there is one, falls back to a per-provider default, and
        reserves `_CONTEXT_INPUT_FRACTION` of it for input. Reusing it makes the budget
        scale with the model instead of with a placeholder, and keeps a genuinely small
        local model (LM Studio's 8,192) safe without a special case.

        Roughly, in characters: LM Studio 19k, OpenRouter 78k, OpenAI 307k, Gemini 2.4M —
        against 16k for every one of them before. It is a ceiling, not a target; a turn
        that gathered less sends less.
        """
        return max(_SYNTHESIS_MIN_EVIDENCE_CHARS, self.resolve_context_budget() * 4)

    @staticmethod
    def _fair_shares(lengths: list[int], budget: int) -> list[int]:
        """Split `budget` across items wanting `lengths`, max-min fair.

        Every item gets an equal share; an item wanting less than its share takes only
        what it needs and its surplus is redistributed among the ones still short. The
        result does not depend on the order the items are given in, which is the property
        that matters here — no tool result can be starved by where it happens to sit.
        """
        shares = [0] * len(lengths)
        pending = [index for index, length in enumerate(lengths) if length > 0]
        remaining = max(0, budget)
        while pending and remaining > 0:
            share = remaining // len(pending)
            if share == 0:
                break
            for index in list(pending):
                grant = min(share, lengths[index] - shares[index])
                shares[index] += grant
                remaining -= grant
                if shares[index] >= lengths[index]:
                    pending.remove(index)
        return shares

    @staticmethod
    def _tool_result_sections(full_messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
        """Every tool result of the turn as ``(label, content)``, in call order.

        The label names the tool and the arguments it was called with. Without it the
        synthesis prompt is a run of unattributed JSON blobs, and a model asked to weigh
        a broad first search against a narrow fifth one cannot tell which is which.

        The tool message on the wire carries only `tool_call_id`, so the name is
        recovered from the assistant turn that requested it.
        """
        names: dict[str, str] = {}
        for item in full_messages:
            for call in item.get("tool_calls") or []:
                function = call.get("function") or {}
                arguments = function.get("arguments")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False) if arguments else ""
                names[call.get("id")] = (
                    f"{function.get('name') or 'tool'}({arguments[:160]})"
                )

        sections: list[tuple[str, str]] = []
        for item in full_messages:
            if item.get("role") != "tool":
                continue
            content = str(item.get("content") or "")
            if content:
                sections.append((names.get(item.get("tool_call_id"), "tool"), content))
        return sections

    def _tool_result_synthesis_messages(
        self,
        messages: list[dict[str, str]],
        full_messages: list[dict[str, Any]],
        circulars_context: str | None,
    ) -> list[dict[str, str]]:
        """Rebuild the turn for a final, tool-free answer.

        The budget is shared, not served first-come. It used to be spent in call order
        with no per-result bound, so the first tool result could take all of it and the
        loop then `break`; measured on chat session `9f5724b0` (benchmark P15, run 2),
        ten tool calls produced exactly one section of 16,000 characters, clipped
        mid-JSON, and the other nine never reached the model. The fifth of those nine had
        returned the State Bank of Pakistan Act — the one document that could answer the
        question — and the answer went out saying the Act was "not among the provided
        sources". Run 1 of the same question differed only in which tool the model
        happened to call first.

        So every result now gets a fair share of the window, what is clipped is marked as
        clipped, and the system prompt is told when the evidence in front of it is
        partial. An answer built on a truncated record is not automatically wrong; one
        that cannot tell it is reading a truncated record has no way to say so.
        """
        sections = self._tool_result_sections(full_messages)
        no_context = "No selected circular context was provided."
        # Everything except the tool results themselves is charged to the budget rather
        # than added on top of it, so the ceiling remains the ceiling. Per section, 96
        # covers the "Result of …:" line, the blank line after it, and the longest marker
        # any clip can produce; a section that is not clipped leaves its allowance
        # unspent. `circulars_context` is not charged here — it arrives already bounded
        # by the caller's own budget in `build_chat_context`, and charging it twice would
        # shrink the tool results to pay for something that has already been paid for.
        budget = self._synthesis_evidence_budget()
        overhead = len(_SYNTHESIS_FRAMING) + len(no_context)
        overhead += sum(len(label) + 96 for label, _ in sections)
        shares = self._fair_shares(
            [len(content) for _, content in sections], max(0, budget - overhead)
        )

        rendered: list[str] = []
        clipped = 0
        for (label, content), share in zip(sections, shares):
            if share < len(content):
                clipped += 1
                omitted = len(content) - share
                body = (
                    f"{content[:share]}\n… [clipped: {omitted} characters of this "
                    "result are not shown]"
                )
            else:
                body = content
            rendered.append(f"Result of {label}:\n{body}")

        synthesis_context = [
            "Selected circular context:",
            circulars_context or "No selected circular context was provided.",
        ]
        if rendered:
            synthesis_context.extend([
                _SYNTHESIS_EVIDENCE_HEADER,
                "\n\n".join(rendered),
            ])

        instructions = (
            "You are an expert assistant for SBP circulars and regulations. "
            "No tools are available in this step. Answer using only the selected "
            "context and source material below.\n\n"
            f"{_CITATION_RULES}\n\n{_ANSWER_CONTRACT}"
        )
        if clipped:
            instructions += (
                f"\n\n{clipped} of the {len(sections)} extracts below were too long to "
                "include whole and are marked where they were clipped. Answer from what "
                "is there, and if the answer depends on a part that was clipped, say "
                "which source it was and that you could not see all of it. Do not guess "
                "at clipped content."
            )

        return [
            {"role": "system", "content": instructions},
            *messages,
            {"role": "user", "content": "\n\n".join(synthesis_context)},
        ]

    def _truncate_context(self, content_text: str, limit: int | None = None) -> str:
        """Clip document text to the configured context budget before prompting."""
        if limit is None:
            limit = self.config.max_context_tokens
        return content_text[:limit] if len(content_text) > limit else content_text

    def summarize(self, title: str, content_text: str, *, subject: str = "circular") -> str:
        system = f"You are a concise financial regulations analyst. Summarize the following SBP {subject} in 3-5 sentences, focusing on the key regulatory changes, requirements, and impact on banks/DFIs/MFBs. Be factual and specific."
        truncated = self._truncate_context(content_text)
        user = f"Title: {title}\n\nContent:\n{truncated}"
        result = self._complete(system, user, temperature=0.2)
        return result.strip()

    def reduce_summaries(
        self, title: str, summaries: list[str], *, subject: str = "document"
    ) -> str:
        """Fold per-section summaries of one long document into a single summary.

        The map half of summarising something too long to read in one call. A law runs to
        382k characters, where a circular is a two-page letter — sending the head of it and
        calling the result a summary would describe the title page.
        """
        system = (
            f"You are a concise financial regulations analyst. You are given ordered "
            f"section summaries of a single SBP {subject}. Write one 3-5 sentence summary "
            "of the whole document, focusing on the obligations it imposes and who they "
            "apply to. Do not mention sections, summaries, or that the document was split. "
            "Be factual and specific."
        )
        sections = "\n\n".join(
            f"[Section {index}]\n{summary}" for index, summary in enumerate(summaries, 1)
        )
        user = f"Title: {title}\n\nSection summaries:\n{sections}"
        return self._complete(system, user, temperature=0.2).strip()

    def generate_tags(
        self, title: str, content_text: str, *, subject: str = "circular"
    ) -> list[str]:
        system = f"You are a financial regulations classifier. Select the most relevant tags from the following taxonomy that apply to the given SBP {subject}.\n\nTaxonomy: {json.dumps(TAG_TAXONOMY)}\n\nReturn ONLY a JSON object with a 'tags' key containing a list of 1-5 selected tag strings from the taxonomy."
        truncated = self._truncate_context(content_text)
        user = f"Title: {title}\n\nContent:\n{truncated}"
        result = self._complete(
            system,
            user,
            temperature=0.0,
            json_schema={
                "type": "object",
                "properties": {
                    "tags": {
                        "type": "array",
                        "items": {"type": "string", "enum": TAG_TAXONOMY},
                        "maxItems": 5,
                    },
                },
                "required": ["tags"],
                "additionalProperties": False,
            },
        )
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as exc:
            raise ValueError("The model returned invalid JSON for tags.") from exc
        tags = parsed.get("tags") if isinstance(parsed, dict) else None
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError("The model returned an invalid tags payload.")
        valid_tags = [tag for tag in tags if tag in TAG_TAXONOMY]
        if not valid_tags:
            valid_tags = tags[:5]
        return valid_tags[:5]

    @staticmethod
    def _response_excerpt(result: str, limit: int = 300) -> str:
        compact = re.sub(r"\s+", " ", result or "").strip()
        return compact[:limit] + ("..." if len(compact) > limit else "")

    @staticmethod
    def _parse_json_object(result: str) -> dict[str, Any]:
        text = (result or "").strip()
        if not text:
            raise ValueError("The model returned an empty checklist response.")

        candidates = [text]
        fenced = re.fullmatch(
            r"```(?:json)?\s*(.*?)\s*```",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if fenced:
            candidates.insert(0, fenced.group(1).strip())
        decoder = json.JSONDecoder()
        for start, char in enumerate(text):
            if char not in "[{":
                continue
            try:
                _, end = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                continue
            candidates.append(text[start:start + end])
            break

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, str):
                    parsed = json.loads(parsed)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"items": parsed}
        raise ValueError("The model returned an invalid checklist JSON payload.")

    @staticmethod
    def _checklist_extraction_schema() -> dict[str, Any]:
        string_field = {"type": "string"}
        return {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "requirement": string_field,
                            "classification": {
                                "type": "string",
                                "enum": ["required", "optional"],
                            },
                            "actor": string_field,
                            "action": string_field,
                            "object": string_field,
                            "conditions": string_field,
                            "deadline": string_field,
                            "evidence": string_field,
                            "applicability": string_field,
                            "source_unit_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                            },
                        },
                        "required": ["requirement", "classification", "source_unit_ids"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["items"],
            "additionalProperties": False,
        }

    @classmethod
    def _parse_checklist_items(
        cls,
        result: str,
        valid_source_ids: set[str],
    ) -> list[dict[str, Any]]:
        parsed = cls._parse_json_object(result)
        entries = parsed.get("items", parsed.get("checklist_items", parsed.get("checklist")))
        if not isinstance(entries, list):
            raise ValueError("The model returned a checklist payload without an items array.")

        normalized: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("The model returned a non-object checklist item.")
            requirement = entry.get("requirement", entry.get("item"))
            if not isinstance(requirement, str) or len(requirement.strip()) < 8:
                raise ValueError("The model returned a checklist item without a usable requirement.")
            classification = entry.get("classification")
            if classification is None and isinstance(entry.get("action_required"), bool):
                classification = "required" if entry["action_required"] else "optional"
            classification = str(classification or "").strip().lower()
            if classification not in {"required", "optional"}:
                raise ValueError("The model returned an invalid checklist classification.")

            source_ids = entry.get(
                "source_unit_ids",
                entry.get("source_ids", entry.get("citations", [])),
            )
            if isinstance(source_ids, str):
                source_ids = [source_ids]
            if not isinstance(source_ids, list):
                raise ValueError("The model returned invalid source citations.")
            citations_provided = bool(source_ids)
            cited = list(dict.fromkeys(
                str(source_id) for source_id in source_ids
                if str(source_id) in valid_source_ids
            ))
            if not cited and not citations_provided and len(valid_source_ids) == 1:
                cited = list(valid_source_ids)
            if not cited:
                raise ValueError("The model returned a checklist item without a valid source citation.")

            item = {
                "requirement": re.sub(r"\s+", " ", requirement).strip(),
                "classification": classification,
                "source_unit_ids": cited,
            }
            for field_name in (
                "actor", "action", "object", "conditions", "deadline", "evidence", "applicability"
            ):
                value = entry.get(field_name, "")
                item[field_name] = re.sub(r"\s+", " ", str(value or "")).strip()
            normalized.append(item)
        return normalized

    _CHECKLIST_SYSTEM_PROMPT = (
        "You are a conservative SBP regulatory compliance analyst. Extract "
        "actionable compliance requirements from the SOURCE BLOCKS below. The "
        "text may contain several sections, forms, or tables; read them together "
        "as one corpus so related clauses inform each other.\n\n"
        "INCLUDE: explicit duties, prohibitions, eligibility conditions, "
        "controls, deadlines, recordkeeping, submission requirements, required "
        "evidence, and explicit permissions or recommendations.\n"
        "EXCLUDE: headings, definitions without an obligation, explanatory "
        "narrative, addresses, greetings, signature labels, blank form fields, "
        "empty table cells, and formatting fragments. For forms and tables, "
        "extract only substantive required fields, attestations, evidence, "
        "submission actions, or format constraints; never turn individual words "
        "or decorative labels into items.\n"
        "GROUPING: combine an introductory clause with its dependent list when "
        "they form one obligation, but keep genuinely separate actions separate.\n"
        "CLASSIFICATION: use \"required\" for duties, prohibitions, conditions, "
        "and mandatory evidence; use \"optional\" only for explicit permissions "
        "or recommendations.\n"
        "FIELDS: each item is a JSON object with these keys: \"requirement\" (one "
        "self-contained sentence of at least 8 characters stating the obligation), "
        "\"classification\" (\"required\" or \"optional\"), \"actor\", \"action\", "
        "\"object\", \"conditions\", \"deadline\", \"evidence\", \"applicability\", "
        "and \"source_unit_ids\". Populate actor, action, object, conditions, "
        "deadline, evidence, and applicability when present, using an empty string "
        "when a field is absent.\n"
        "CITATIONS: \"source_unit_ids\" must be a non-empty JSON array citing the "
        "[SOURCE_ID: ...] value(s) from the block(s) the item came from, copied "
        "exactly. Use no other key name for citations.\n"
        "OUTPUT: return only a JSON object {\"items\":[...]}. If there is no "
        "actionable requirement, return {\"items\":[]}."
    )

    @staticmethod
    def _subject_label(circular, label: str | None) -> str:
        """Name the instrument under analysis, however the caller identified it.

        An explicit `label` wins, so a caller holding a law rather than a circular does not
        have to fake one. Neither is a programming error worth raising over — the label is
        prompt context, and an empty one degrades the extraction rather than breaking it.
        """
        if label:
            return label
        if circular is None:
            return ""
        return circular.reference or circular.title

    def _extract_checklist_batch(
        self,
        *,
        label: str,
        blocks: list,
        trace_callback=None,
    ) -> list[dict[str, Any]]:
        schema = self._checklist_extraction_schema()
        doc_label = blocks[0].doc_label
        pages = [
            page
            for block in blocks
            for page in (block.page_start, block.page_end)
            if page is not None
        ]
        page_span = f"pages {min(pages)}-{max(pages)}" if pages else "HTML"
        sections = "\n\n".join(
            f"--- Section: {block.ref} (type: {block.block_type}, pages "
            f"{block.page_start or 'HTML'}-{block.page_end or block.page_start or 'HTML'}) ---\n"
            f"{block.source_text}"
            for block in blocks
        )
        system = self._CHECKLIST_SYSTEM_PROMPT
        # "Subject" rather than "Circular": the same extractor now runs over laws and
        # regulations, and telling the model a 200-page Act is a circular is a claim about
        # the document it is reading. `Document` below stays the file within the subject —
        # the circular's body, an attachment PDF, or a law's archived edition.
        user = f"""Subject: {label}
Document: {doc_label}
Sections: {len(blocks)} ({page_span})

SOURCE BLOCKS:
{sections}"""
        valid_source_ids = {
            source_id for block in blocks for source_id in block.source_unit_ids
        }
        trace_block = (
            blocks[0]
            if len(blocks) == 1
            else SimpleNamespace(
                ref=f"{len(blocks)} sections ({blocks[0].ref} … {blocks[-1].ref})",
                block_id=blocks[0].block_id,
            )
        )
        if trace_callback:
            trace_callback("llm_input", {
                "block": trace_block,
                "system_prompt": system,
                "user_prompt": user,
            })
        result = self._complete_json(system, user, json_schema=schema, temperature=0.0)
        if trace_callback:
            trace_callback("llm_output", {"block": trace_block, "raw_response": result})
        try:
            return self._parse_checklist_items(result, valid_source_ids)
        except ValueError as exc:
            emit_event("parse_error", {
                "parser": "checklist_items", "error": str(exc),
            }, stage="checklist.parse")
            emit_event("retry", {
                "reason": "malformed_checklist", "next_attempt": 2,
            }, stage="checklist.retry")
            retry_system = (
                system
                + "\n\nYour previous response was malformed. Return the "
                "schema-compliant JSON object only, with valid SOURCE_ID citations."
            )
            retry_result = self._complete_json(
                retry_system, user, json_schema=schema, temperature=0.0
            )
            if trace_callback:
                trace_callback("llm_output", {
                    "block": trace_block,
                    "raw_response": retry_result,
                    "attempt": 2,
                })
            try:
                return self._parse_checklist_items(retry_result, valid_source_ids)
            except ValueError as exc:
                emit_event("parse_error", {
                    "parser": "checklist_items", "error": str(exc),
                    "attempt": 2,
                }, stage="checklist.parse")
                first = self._response_excerpt(result)
                second = self._response_excerpt(retry_result)
                raise ValueError(
                    "The model returned an invalid checklist response after retry. "
                    f"First: {first!r}; retry: {second!r}"
                ) from exc

    @staticmethod
    def _best_excerpt(requirement: str, source_text: str) -> str:
        """Pick the source sentence that best supports the requirement.

        Uses Jaccard token overlap (overlap relative to the union) with a minimum
        length floor so a single high-frequency word can no longer win with a
        misleadingly short fragment. Falls back to the full text when nothing
        meaningfully overlaps.
        """
        requirement_tokens = set(re.findall(r"[a-z0-9]+", requirement.casefold()))
        candidates = [
            re.sub(r"\s+", " ", candidate).strip(" -*|\n")
            for candidate in re.split(r"(?<=[.;:])\s+|\n+", source_text)
        ]
        candidates = [candidate for candidate in candidates if len(candidate) >= 20]

        def score(candidate: str) -> float:
            tokens = set(re.findall(r"[a-z0-9]+", candidate.casefold()))
            if not tokens or not requirement_tokens:
                return 0.0
            return len(requirement_tokens & tokens) / len(requirement_tokens | tokens)

        best = max(candidates, key=score, default="")
        excerpt = best if best and score(best) > 0.0 else source_text
        if len(excerpt) > 900:
            excerpt = excerpt[:897].rstrip() + "..."
        return excerpt

    @staticmethod
    def _materialize_checklist_item(entry, units_by_id) -> dict[str, Any]:
        source_units = [units_by_id[source_id] for source_id in entry["source_unit_ids"]]
        primary = source_units[0]
        refs = list(dict.fromkeys(unit.ref for unit in source_units))
        pages = [
            page
            for unit in source_units
            for page in (unit.page_start, unit.page_end)
            if page is not None
        ]
        digest = hashlib.sha256(
            f"{primary.doc_id}\0{entry['requirement']}\0{'|'.join(entry['source_unit_ids'])}".encode("utf-8")
        ).hexdigest()[:20]
        source_text = "\n\n".join(unit.source_text for unit in source_units)
        source_excerpt = AIClient._best_excerpt(entry["requirement"], source_text)
        return {
            "item_id": f"checklist:{digest}",
            "requirement": entry["requirement"],
            "classification": entry["classification"],
            "actor": entry["actor"],
            "action": entry["action"],
            "object": entry["object"],
            "conditions": entry["conditions"],
            "deadline": entry["deadline"],
            "evidence": entry["evidence"],
            "applicability": entry["applicability"],
            "ref": "; ".join(refs),
            "source_refs": refs,
            "source_unit_ids": entry["source_unit_ids"],
            "source_text": source_excerpt,
            "doc_id": primary.doc_id,
            "doc_type": primary.doc_type,
            "doc_label": primary.doc_label,
            "page_start": min(pages) if pages else None,
            "page_end": max(pages) if pages else None,
        }

    @staticmethod
    def _deduplicate_checklist_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduplicated: list[dict[str, Any]] = []
        by_key: dict[str, dict[str, Any]] = {}
        for item in items:
            semantic_fields = [
                item.get("actor"), item.get("action"), item.get("object"),
                item.get("conditions"), item.get("deadline"), item.get("applicability"),
            ]
            key_text = " | ".join(str(value or "") for value in semantic_fields)
            if not item.get("action") or not item.get("object"):
                key_text = str(item.get("requirement") or "")
            key = re.sub(r"[^a-z0-9]+", " ", key_text.casefold()).strip()
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = item
                deduplicated.append(item)
                continue
            existing["classification"] = (
                "required"
                if "required" in {existing.get("classification"), item.get("classification")}
                else "optional"
            )
            for list_field in ("source_refs", "source_unit_ids"):
                existing[list_field] = list(dict.fromkeys([
                    *existing.get(list_field, []), *item.get(list_field, [])
                ]))
            existing["ref"] = "; ".join(existing["source_refs"])
            merged_source_text = "\n\n".join(dict.fromkeys([
                existing.get("source_text", ""), item.get("source_text", "")
            ]))
            existing["source_text"] = (
                merged_source_text
                if len(merged_source_text) <= 900
                else merged_source_text[:897].rstrip() + "..."
            )
            pages = [
                page for page in (
                    existing.get("page_start"), existing.get("page_end"),
                    item.get("page_start"), item.get("page_end"),
                ) if page is not None
            ]
            if pages:
                existing["page_start"] = min(pages)
                existing["page_end"] = max(pages)
        return deduplicated

    @staticmethod
    def _record_block_gap(gaps: list[dict[str, Any]], block, exc: Exception) -> None:
        gaps.append({
            "doc_id": block.doc_id,
            "doc_type": block.doc_type,
            "doc_label": block.doc_label,
            "reason": "checklist_extraction_error",
            "error": str(exc),
            "block_id": block.block_id,
            "ref": block.ref,
            "page_start": block.page_start,
            "page_end": block.page_end,
        })

    def _run_checklist_batch(
        self,
        *,
        label: str,
        batch: list,
        units_by_id: dict[str, Any],
        gaps: list[dict[str, Any]],
        trace_callback=None,
    ) -> list[dict[str, Any]]:
        """Extract one batch, degrading to per-block calls when the batch fails.

        Rate-limit errors propagate so callers can abort. Parse and context-size
        failures on a multi-block batch are recovered by retrying each block on
        its own; a block that still fails is recorded as a coverage gap.
        """
        try:
            extracted = self._extract_checklist_batch(
                label=label,
                blocks=batch,
                trace_callback=trace_callback,
            )
            return [
                self._materialize_checklist_item(entry, units_by_id)
                for entry in extracted
            ]
        except Exception as exc:
            if is_rate_limit_error(exc):
                raise
            if not isinstance(exc, ValueError) and not _is_context_size_error(exc):
                raise
            if len(batch) == 1:
                self._record_block_gap(gaps, batch[0], exc)
                return []

        # Multi-block batch failed for a recoverable reason: salvage per block.
        materialized: list[dict[str, Any]] = []
        for block in batch:
            try:
                extracted = self._extract_checklist_batch(
                    label=label,
                    blocks=[block],
                    trace_callback=trace_callback,
                )
                materialized.extend(
                    self._materialize_checklist_item(entry, units_by_id)
                    for entry in extracted
                )
            except Exception as block_exc:
                if is_rate_limit_error(block_exc):
                    raise
                self._record_block_gap(gaps, block, block_exc)
        return materialized

    def generate_checklist(
        self,
        circular=None,
        *,
        label: str | None = None,
        delay: float = 0.0,
        progress_callback=None,
        trace_callback=None,
        documents: list[dict[str, Any]] | None = None,
        gaps: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Extract a cited obligations checklist from a circular, or from any prepared corpus.

        Two ways in. Pass a `circular` and the corpus is built from it. Pass `documents`
        and `label` and the subject can be anything the segmenter can read — which is how
        the laws arm reaches this without a `Circular` to hand (see `laws_ai.law_corpus`).
        """
        from .checklist import (
            build_analysis_blocks,
            build_checklist_corpus,
            build_extraction_batches,
            segment_document,
        )

        label = self._subject_label(circular, label)
        if documents is None:
            documents, discovered_gaps = build_checklist_corpus(circular)
            gaps = discovered_gaps if gaps is None else list(gaps)
        else:
            documents = list(documents)
            gaps = list(gaps or [])
        if trace_callback:
            for document in documents:
                trace_callback("document", {"document": document})
        document_units = []
        failed_document_ids: set[str] = set()
        for document in documents:
            try:
                units = segment_document(document)
            except Exception as exc:
                units = []
                failed_document_ids.add(document["doc_id"])
                gaps.append({
                    "doc_id": document["doc_id"],
                    "doc_type": document["doc_type"],
                    "doc_label": document["doc_label"],
                    "reason": "docling_conversion_error",
                    "error": str(exc),
                })
            document_units.append((document, units))
        if trace_callback:
            for document, units in document_units:
                trace_callback("parsing", {"document": document, "units": units})
        for document, units in document_units:
            if not units and document["doc_id"] not in failed_document_ids:
                gaps.append({
                    "doc_id": document["doc_id"],
                    "doc_type": document["doc_type"],
                    "doc_label": document["doc_label"],
                    "reason": "no_items",
                })
        document_blocks = [
            (document, units, build_analysis_blocks(units))
            for document, units in document_units
        ]
        if trace_callback:
            for document, _, blocks in document_blocks:
                trace_callback("analysis_blocks", {"document": document, "blocks": blocks})
        # Pack each document's blocks into the fewest LLM calls that fit the
        # model's context window. A small circular collapses to a single call so
        # the model sees the whole document at once.
        budget = self.resolve_context_budget()
        document_batches = [
            (document, build_extraction_batches(blocks, budget, self._estimate_tokens))
            for document, _, blocks in document_blocks
        ]
        total_batches = sum(len(batches) for _, batches in document_batches)
        completed = 0
        checklist_items: list[dict[str, Any]] = []
        all_units = [unit for _, units in document_units for unit in units]
        units_by_id = {unit.unit_id: unit for unit in all_units}
        if progress_callback:
            progress_callback(0, total_batches)

        for _, batches in document_batches:
            for batch in batches:
                materialized = self._run_checklist_batch(
                    label=label,
                    batch=batch,
                    units_by_id=units_by_id,
                    gaps=gaps,
                    trace_callback=trace_callback,
                )
                checklist_items.extend(materialized)
                completed += 1
                if trace_callback:
                    trace_callback("normalized_block", {
                        "block": (
                            batch[0]
                            if len(batch) == 1
                            else SimpleNamespace(
                                ref=f"{len(batch)} sections",
                                block_id=batch[0].block_id,
                            )
                        ),
                        "items": materialized,
                        "completed": completed,
                        "total": total_batches,
                    })
                if progress_callback:
                    progress_callback(completed, total_batches)
                if delay > 0 and completed < total_batches:
                    time.sleep(delay)

        checklist_items = self._deduplicate_checklist_items(checklist_items)
        return {
            "schema_version": 2,
            "status": "completed_with_gaps" if gaps else "completed",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "coverage_gaps": gaps,
            "checklist_items": checklist_items,
            "source_units": [unit.payload() for unit in all_units],
            "analysis_blocks": [
                {
                    key: value
                    for key, value in block.payload().items()
                    if key != "source_text"
                }
                for _, _, blocks in document_blocks
                for block in blocks
            ],
        }

    @staticmethod
    def _entity_extraction_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "entity_type": {
                                "type": "string",
                                "enum": [
                                    "ratio",
                                    "monetary_threshold",
                                    "percentage_limit",
                                    "numeric_limit",
                                    "deadline",
                                    "effective_date",
                                ],
                            },
                            "metric": {"type": "string"},
                            "comparator": {
                                "type": ["string", "null"],
                                "enum": ["min", "max", "exactly", "range", None],
                            },
                            "value_numeric": {"type": ["number", "null"]},
                            "value_high": {"type": ["number", "null"]},
                            "unit": {
                                "type": ["string", "null"],
                                "enum": ["%", "PKR", "USD", "times", "days", "months", None],
                            },
                            "value_text": {"type": "string"},
                            "subject": {"type": "string"},
                            "effective_date": {"type": ["string", "null"]},
                            "context_snippet": {"type": "string"},
                            "source_unit_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "confidence": {"type": ["number", "null"]},
                        },
                        "required": [
                            "entity_type", "metric", "comparator", "value_numeric",
                            "value_high", "unit", "value_text", "subject",
                            "effective_date", "context_snippet", "source_unit_ids",
                            "confidence",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["entities"],
            "additionalProperties": False,
        }

    @staticmethod
    def _entity_system_prompt() -> str:
        return """You are a meticulous SBP regulatory data analyst. From the SOURCE BLOCKS supplied, extract every specific regulatory VALUE that a bank/DFI/MFB must comply with. Capture:
- ratios named in the text (e.g. CAR/Capital Adequacy Ratio, LCR, NSFR, Leverage Ratio, CCB);
- monetary thresholds (minimum paid-up capital, MCR, exposure/finance limits) in PKR or USD;
- percentage limits (caps, floors, weights, rates);
- other numeric limits (e.g. number of branches, multiples/"times", tenor in days/months);
- deadlines and effective dates attached to a requirement.

For each value output:
- entity_type: one of ratio | monetary_threshold | percentage_limit | numeric_limit | deadline | effective_date.
- metric: the canonical short name of what the value measures (e.g. "CAR", "LCR", "NSFR", "MCR", "Paid-up Capital", "Leverage Ratio", "Exposure Limit"). Use a concise noun phrase if no standard acronym exists.
- comparator: min (for "at least"/"minimum"/"not less than"), max (for "maximum"/"shall not exceed"/"up to"), exactly (a fixed required value), or range; null for pure dates.
- value_numeric: the value NORMALIZED TO BASE UNITS as a plain number. Convert scales: "Rs. 23 billion" -> 23000000000, "Rs 5 crore" -> 50000000, "US$ 300 million" -> 300000000, "8%" -> 8, "1.5 times" -> 1.5. For dates use null.
- value_high: upper bound when comparator is range, else null.
- unit: % | PKR | USD | times | days | months, or null for dates.
- value_text: the value exactly as written in the text (e.g. "Rs. 23 billion", "8%").
- subject: who/what the value applies to (e.g. "locally incorporated banks", "MFBs", "foreign banks with up to 5 branches"); empty string if unspecified.
- effective_date: the date this value takes effect or is due, as ISO YYYY-MM-DD, or null if none. For entity_type deadline/effective_date this is the date itself.
- context_snippet: the sentence/clause the value came from (<= 300 chars).
- source_unit_ids: one or more SOURCE_ID values cited exactly as supplied.
- confidence: 0.0-1.0.

Emit one entry per distinct (metric, subject, value) tuple — phased schedules produce multiple entries. Do NOT extract values from definitions, examples, or narrative that impose no requirement. If the blocks contain no regulatory value, return {"entities": []}. Return only the JSON object."""

    def _parse_entities(self, result: str, valid_source_ids: set[str]) -> list[dict[str, Any]]:
        parsed = self._parse_json_object(result)
        raw_entities = parsed.get("entities")
        if not isinstance(raw_entities, list):
            raise ValueError("The model returned an invalid entities payload.")
        valid_types = {
            "ratio", "monetary_threshold", "percentage_limit",
            "numeric_limit", "deadline", "effective_date",
        }
        cleaned: list[dict[str, Any]] = []
        for entry in raw_entities:
            if not isinstance(entry, dict):
                continue
            entity_type = str(entry.get("entity_type") or "").strip()
            if entity_type not in valid_types:
                continue
            # Keep the raw citations the model returned; they are resolved against the
            # block's units later (tolerant of full ids or suffix-only ids). A mangled
            # citation must not discard an otherwise-valid extracted value.
            source_ids = [
                str(sid).strip() for sid in (entry.get("source_unit_ids") or []) if str(sid).strip()
            ]
            value_numeric = self._coerce_number(entry.get("value_numeric"))
            value_text = re.sub(r"\s+", " ", str(entry.get("value_text") or "")).strip()
            effective_date = self._coerce_date(entry.get("effective_date"))
            # A ratio/threshold/limit with no usable number carries no queryable value.
            if entity_type in {"ratio", "monetary_threshold", "percentage_limit", "numeric_limit"} and value_numeric is None:
                continue
            if entity_type in {"deadline", "effective_date"}:
                # Models often place the date in value_text rather than effective_date.
                if effective_date is None:
                    effective_date = self._coerce_date(value_text)
                if effective_date is None and not value_text:
                    continue
            cleaned.append({
                "entity_type": entity_type,
                "metric": re.sub(r"\s+", " ", str(entry.get("metric") or "")).strip() or None,
                "comparator": (str(entry.get("comparator")).strip() or None) if entry.get("comparator") else None,
                "value_numeric": value_numeric,
                "value_high": self._coerce_number(entry.get("value_high")),
                "unit": (str(entry.get("unit")).strip() or None) if entry.get("unit") else None,
                "value_text": value_text or None,
                "subject": re.sub(r"\s+", " ", str(entry.get("subject") or "")).strip() or None,
                "effective_date": effective_date,
                "context_snippet": re.sub(r"\s+", " ", str(entry.get("context_snippet") or "")).strip()[:500] or None,
                "source_unit_ids": list(dict.fromkeys(source_ids)),
                "confidence": self._coerce_number(entry.get("confidence")),
            })
        return cleaned

    @staticmethod
    def _coerce_number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = re.sub(r"[,\s]", "", value)
            try:
                return float(cleaned)
            except ValueError:
                return None
        return None

    @staticmethod
    def _coerce_date(value: Any):
        if not value or not isinstance(value, str):
            return None
        text = value.strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text[:10])
        except ValueError:
            return None

    def _extract_entities_batch(
        self, *, label: str, blocks: list
    ) -> list[dict[str, Any]]:
        system = self._entity_system_prompt()
        doc_label = blocks[0].doc_label
        pages = [
            page
            for block in blocks
            for page in (block.page_start, block.page_end)
            if page is not None
        ]
        page_span = f"pages {min(pages)}-{max(pages)}" if pages else "HTML"
        sections = "\n\n".join(
            f"--- Section: {block.ref} (pages "
            f"{block.page_start or 'HTML'}-{block.page_end or block.page_start or 'HTML'}) ---\n"
            f"{block.source_text}"
            for block in blocks
        )
        user = f"""Subject: {label}
Document: {doc_label}
Sections: {len(blocks)} ({page_span})

SOURCE BLOCKS:
{sections}"""
        result = self._complete(
            system, user, temperature=0.0, json_schema=self._entity_extraction_schema()
        )
        valid_source_ids = {
            source_id for block in blocks for source_id in block.source_unit_ids
        }
        try:
            return self._parse_entities(result, valid_source_ids)
        except ValueError as exc:
            emit_event("parse_error", {
                "parser": "entities", "error": str(exc),
            }, stage="entities.parse")
            emit_event("retry", {
                "reason": "malformed_entities", "next_attempt": 2,
            }, stage="entities.retry")
            retry_system = (
                system
                + "\nYour previous response was malformed. Return the schema-compliant JSON object only, with valid SOURCE_ID citations."
            )
            retry_result = self._complete(
                retry_system, user, temperature=0.0, json_schema=self._entity_extraction_schema()
            )
            return self._parse_entities(retry_result, valid_source_ids)

    def _run_entities_batch(self, *, label: str, batch: list) -> list[dict[str, Any]]:
        """Extract one batch, degrading to per-block calls when the batch fails.

        The same recovery the checklist arm uses, and it matters more here: before
        batching, a malformed response cost one block's values, and now it would cost
        every block packed alongside it.
        """
        try:
            return self._extract_entities_batch(
                label=label, blocks=batch
            )
        except Exception as exc:
            if is_rate_limit_error(exc):
                raise
            if not isinstance(exc, ValueError) and not _is_context_size_error(exc):
                raise
            if len(batch) == 1:
                return []

        extracted: list[dict[str, Any]] = []
        for block in batch:
            try:
                extracted.extend(
                    self._extract_entities_batch(
                        label=label, blocks=[block]
                    )
                )
            except Exception as block_exc:
                if is_rate_limit_error(block_exc):
                    raise
                if not isinstance(block_exc, ValueError) and not _is_context_size_error(block_exc):
                    raise
        return extracted

    def extract_entities(
        self,
        circular=None,
        *,
        label: str | None = None,
        delay: float = 0.0,
        progress_callback=None,
        documents: list[dict[str, Any]] | None = None,
        gaps: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Extract structured regulatory values from a circular, or from any prepared corpus.

        Takes the same two ways in as `generate_checklist`. Returns one dict per value,
        with keys matching the CircularEntity columns (entity_type, metric, comparator,
        value_numeric, value_high, unit, value_text, subject, effective_date,
        context_snippet, source_unit_id, page_start, confidence)."""
        from .checklist import (
            build_analysis_blocks,
            build_checklist_corpus,
            build_extraction_batches,
            segment_document,
        )

        label = self._subject_label(circular, label)
        if documents is None:
            documents, _ = build_checklist_corpus(circular)
        document_units: list[tuple[dict[str, Any], list]] = []
        for document in documents:
            try:
                units = segment_document(document)
            except Exception:
                units = []
            document_units.append((document, units))

        units_by_id = {
            unit.unit_id: unit for _, units in document_units for unit in units
        }
        # Models frequently cite only the hash suffix of a unit_id ("doc:suffix").
        # Map suffixes back so those citations still resolve to a page.
        suffix_to_id: dict[str, str] = {}
        for uid in units_by_id:
            suffix_to_id.setdefault(uid.rsplit(":", 1)[-1], uid)
        # Pack blocks into the fewest calls that fit the context window, exactly as the
        # checklist arm does. One call per block was affordable on a two-page circular
        # and is not on a 200-page regulation, where the same document is 80-150 blocks.
        budget = self.resolve_context_budget()
        document_batches = [
            (
                units,
                build_extraction_batches(
                    build_analysis_blocks(units), budget, self._estimate_tokens
                ),
            )
            for _, units in document_units
        ]
        total_batches = sum(len(batches) for _, batches in document_batches)
        completed = 0
        if progress_callback:
            progress_callback(0, total_batches)

        entities: list[dict[str, Any]] = []
        for _, batches in document_batches:
            for batch in batches:
                for entry in self._run_entities_batch(
                    label=label, batch=batch
                ):
                    resolved_ids = []
                    for sid in entry["source_unit_ids"]:
                        uid = sid if sid in units_by_id else suffix_to_id.get(sid)
                        if uid:
                            resolved_ids.append(uid)
                    source_units = [units_by_id[uid] for uid in resolved_ids]
                    pages = [
                        unit.page_start
                        for unit in source_units
                        if unit.page_start is not None
                    ]
                    entry["source_unit_id"] = resolved_ids[0] if resolved_ids else None
                    entry["page_start"] = min(pages) if pages else None
                    del entry["source_unit_ids"]
                    entities.append(entry)
                completed += 1
                if progress_callback:
                    progress_callback(completed, total_batches)
                if delay > 0 and completed < total_batches:
                    time.sleep(delay)
        return entities

    # Fixed from the corpus, not guessed — see `RegDocumentRelationship`.
    LAW_RELATION_TYPES = ("made_under", "amends", "repeals", "references")
    # What a circular does to a regulation it names. `listing` and `annexure_of` are
    # written by the deterministic passes and are not judgements a model should make.
    CIRCULAR_LAW_ACTIONS = ("amends", "implements", "clarifies", "references")

    @staticmethod
    def _relation_schema(key: str, types: tuple[str, ...]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                key: {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "type": {"type": "string", "enum": list(types)},
                            "confidence": {"type": ["number", "null"]},
                        },
                        "required": ["id", "type", "confidence"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [key],
            "additionalProperties": False,
        }

    def _parse_relations(
        self, result: str, key: str, valid_ids: set[str], types: tuple[str, ...]
    ) -> dict[str, dict[str, Any]]:
        parsed = self._parse_json_object(result)
        rows = parsed.get(key)
        if not isinstance(rows, list):
            raise ValueError(f"The model returned an invalid {key} payload.")
        classified: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            identifier = str(row.get("id") or "").strip()
            relation = str(row.get("type") or "").strip()
            # An id the model invented cannot be written as an edge, and a type outside
            # the vocabulary is not a weaker claim — it is an unusable one.
            if identifier not in valid_ids or relation not in types:
                continue
            classified[identifier] = {
                "type": relation,
                "confidence": self._coerce_number(row.get("confidence")),
            }
        return classified

    def classify_law_references(
        self, source_title: str, candidates: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Decide what one law's mention of another actually asserts.

        The candidates are already known to be named in the text; the only question is
        meaning. Most mentions are definitional, so the prompt has to make `references`
        the comfortable answer rather than a failure to find something stronger.
        """
        if not candidates:
            return {}
        system = (
            "You are a financial regulations analyst. A source SBP instrument names other "
            "instruments in its text. For each candidate, classify the relationship the "
            "SOURCE has to that candidate, using the quoted excerpts as your only evidence.\n"
            "- 'made_under': the source is subordinate legislation issued under the "
            "candidate's authority (\"in exercise of the powers conferred by section X of "
            "the candidate\", \"made under\", \"framed under\").\n"
            "- 'amends': the source alters the candidate's text (substitutes, inserts or "
            "omits words, or is an Amendment Act of it).\n"
            "- 'repeals': the source repeals or withdraws the candidate.\n"
            "- 'references': any other mention — a definition borrowed from it, a "
            "cross-reference, a list of related laws. THIS IS THE MOST COMMON ANSWER; use "
            "it whenever the excerpts do not clearly show one of the three above.\n"
            "Return only {\"relations\":[{\"id\":..., \"type\":..., \"confidence\":0.0-1.0}]}, "
            "one entry per candidate, copying each id exactly."
        )
        blocks = "\n\n".join(
            f"[id: {candidate['id']}] {candidate['title']}\n"
            + "\n".join(f"  \"…{snippet}…\"" for snippet in candidate["snippets"][:3])
            for candidate in candidates
        )
        user = f"SOURCE: {source_title}\n\nCANDIDATES:\n{blocks}"
        schema = self._relation_schema("relations", self.LAW_RELATION_TYPES)
        result = self._complete(system, user, temperature=0.0, json_schema=schema)
        valid_ids = {str(candidate["id"]) for candidate in candidates}
        return self._parse_relations(
            result, "relations", valid_ids, self.LAW_RELATION_TYPES
        )

    def classify_circular_law_actions(
        self, law_title: str, circulars: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Decide what each circular does to a regulation it names.

        Phase 6b of the laws plan. The deterministic pass can prove a circular *names* a
        regulation; only the wording says whether it amends it, implements it, explains
        it, or merely mentions it in passing.
        """
        if not circulars:
            return {}
        system = (
            "You are a financial regulations analyst. Each candidate is an SBP circular "
            "that names the TARGET regulation. Using the quoted excerpts as your only "
            "evidence, classify what the circular does to the target.\n"
            "- 'amends': changes the target's text or the substance of its requirements.\n"
            "- 'implements': gives effect to it — operating instructions, formats, "
            "timelines, or reporting required by the target.\n"
            "- 'clarifies': explains or interprets the target without changing it.\n"
            "- 'references': mentions it in passing, as background or a cross-reference. "
            "Use this whenever the excerpts do not clearly show one of the three above.\n"
            "Return only {\"actions\":[{\"id\":..., \"type\":..., \"confidence\":0.0-1.0}]}, "
            "one entry per candidate, copying each id exactly."
        )
        blocks = "\n\n".join(
            f"[id: {circular['id']}] {circular['label']}\n"
            + "\n".join(f"  \"…{snippet}…\"" for snippet in circular["snippets"][:2])
            for circular in circulars
        )
        user = f"TARGET REGULATION: {law_title}\n\nCANDIDATE CIRCULARS:\n{blocks}"
        schema = self._relation_schema("actions", self.CIRCULAR_LAW_ACTIONS)
        result = self._complete(system, user, temperature=0.0, json_schema=schema)
        valid_ids = {str(circular["id"]) for circular in circulars}
        return self._parse_relations(
            result, "actions", valid_ids, self.CIRCULAR_LAW_ACTIONS
        )

    def extract_relationships(self, title: str, reference: str, content_text: str) -> dict:
        system = (
            "You are a financial regulations analyst. Extract any mentions of this circular relating to "
            "previous circulars — whether it amends, supersedes, cancels, adds to, or clarifies them. Return "
            "ONLY valid JSON with these keys: 'amends' (list of reference strings), 'supersedes' (list), "
            "'cancels' (list), 'adds_to' (list), 'clarifies' (list). Each reference string should be as close "
            "to the original format as possible, e.g. 'BPRD Circular No. 12 of 2023'.\n"
            "Type definitions — classify each referenced circular under exactly one:\n"
            "- 'amends': changes, revises, relaxes, or extends ANY requirement, value, rate, date, or "
            "deadline of the earlier circular, even if all other instructions remain unchanged. Extending "
            "an implementation date or timeline IS an amendment, not a clarification.\n"
            "- 'supersedes': fully replaces the earlier circular.\n"
            "- 'cancels': withdraws the earlier circular without replacement.\n"
            "- 'adds_to': introduces new requirements or annexures on top of the earlier circular, which "
            "stays in force unchanged.\n"
            "- 'clarifies': explains or interprets the earlier circular WITHOUT changing any requirement, "
            "value, or date. When anything changes, prefer 'amends' over 'clarifies'.\n"
            "Also detect BLANKET supersession: when the circular supersedes, withdraws, repeals, or "
            "consolidates ALL previous/earlier instructions on a subject WITHOUT naming specific circulars "
            "(e.g. 'This will supersede all previous instructions issued on the subject', 'all earlier "
            "instructions stand withdrawn', 'consolidated the existing instructions on the subject'). In that "
            "case set 'supersedes_all_previous' to true and set 'subject' to a short noun phrase naming that "
            "subject (e.g. 'Cash Reserve Requirement'). If there is no such blanket clause, set "
            "'supersedes_all_previous' to false and 'subject' to an empty string.\n"
            "Also detect ATTACHMENT LISTS: when the circular states that the withdrawn/superseded/cancelled "
            "circulars are LISTED in an attached annexure, appendix, or enclosure rather than named in the "
            "text (e.g. 'the circulars listed at Annexure-A stand withdrawn/superseded'). In that case set "
            "'references_attachment_list' to true, set 'attachment_list_label' to the label as written "
            "(e.g. 'Annexure-A'), and set 'attachment_list_action' to 'supersedes' (consolidation or "
            "withdrawn/superseded wording) or 'cancels' (pure cancellation/withdrawal without replacement). "
            "If there is no such attachment list, set 'references_attachment_list' to false, "
            "'attachment_list_label' to an empty string, and 'attachment_list_action' to 'supersedes'."
        )
        truncated = self._truncate_context(
            content_text,
            limit=max(self.config.max_context_tokens, RELATIONSHIP_CONTEXT_CHARS),
        )
        user = f"Title: {title}\nReference: {reference}\n\nContent:\n{truncated}"
        relationship_properties = {
            key: {"type": "array", "items": {"type": "string"}}
            for key in ("amends", "supersedes", "cancels", "adds_to", "clarifies")
        }
        relationship_properties["supersedes_all_previous"] = {"type": "boolean"}
        relationship_properties["subject"] = {"type": "string"}
        relationship_properties["references_attachment_list"] = {"type": "boolean"}
        relationship_properties["attachment_list_label"] = {"type": "string"}
        relationship_properties["attachment_list_action"] = {
            "type": "string",
            "enum": ["supersedes", "cancels"],
        }
        result = self._complete(
            system,
            user,
            temperature=0.0,
            json_schema={
                "type": "object",
                "properties": relationship_properties,
                "required": list(relationship_properties),
                "additionalProperties": False,
            },
        )
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as exc:
            raise ValueError("The model returned invalid JSON for relationships.") from exc
        if not isinstance(parsed, dict):
            raise ValueError("The model returned an invalid relationships payload.")
        relationships = {}
        for key in ("amends", "supersedes", "cancels", "adds_to", "clarifies"):
            values = parsed.get(key, [])
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ValueError(f"The model returned invalid {key} relationships.")
            relationships[key] = values
        relationships["supersedes_all_previous"] = bool(parsed.get("supersedes_all_previous", False))
        subject = parsed.get("subject", "")
        relationships["subject"] = subject.strip() if isinstance(subject, str) else ""
        relationships["references_attachment_list"] = bool(parsed.get("references_attachment_list", False))
        label = parsed.get("attachment_list_label", "")
        relationships["attachment_list_label"] = label.strip() if isinstance(label, str) else ""
        action = parsed.get("attachment_list_action", "supersedes")
        relationships["attachment_list_action"] = action if action in ("supersedes", "cancels") else "supersedes"
        return relationships

    _REQUIREMENTS_SYSTEM_PROMPT = (
        "You are a conservative SBP regulatory analyst. Extract the discrete "
        "REQUIREMENTS this circular currently imposes, so they can later be "
        "diffed against amending circulars.\n"
        "A requirement is one self-contained obligation, prohibition, rate, "
        "limit, procedure step, or condition. Keep each item atomic: one "
        "requirement per changeable fact, so a later amendment to a single "
        "rate or threshold maps to exactly one item.\n"
        "EXCLUDE greetings, addresses, signature blocks, and narrative that "
        "imposes nothing.\n"
        "For each item output:\n"
        "- requirement: one self-contained sentence stating the obligation, "
        "preserving exact figures, rates, and dates as written.\n"
        "- section: the heading or topic the item falls under, as written in "
        "the circular (empty string if none).\n"
        "- value: the single key figure the requirement pins down (e.g. "
        "'11.00%', 'Rs. 5 million', '30 days'), exactly as written; empty "
        "string when the requirement has no central figure.\n"
        "- applies_to: who the item binds (e.g. 'All Commercial Banks'); "
        "empty string if unspecified.\n"
        "Return only a JSON object {\"items\": [...]}."
    )

    @staticmethod
    def _requirements_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "requirement": {"type": "string"},
                            "section": {"type": "string"},
                            "value": {"type": "string"},
                            "applies_to": {"type": "string"},
                        },
                        "required": ["requirement"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["items"],
            "additionalProperties": False,
        }

    def _parse_requirement_items(self, result: str) -> list[dict[str, Any]]:
        parsed = self._parse_json_object(result)
        entries = parsed.get("items", parsed.get("requirements"))
        if not isinstance(entries, list):
            raise ValueError("The model returned a payload without an items array.")
        cleaned: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            requirement = re.sub(r"\s+", " ", str(entry.get("requirement") or "")).strip()
            if len(requirement) < 8:
                continue
            cleaned.append({
                "requirement": requirement,
                "section": re.sub(r"\s+", " ", str(entry.get("section") or "")).strip(),
                "value": re.sub(r"\s+", " ", str(entry.get("value") or "")).strip(),
                "applies_to": re.sub(r"\s+", " ", str(entry.get("applies_to") or "")).strip(),
            })
        if not cleaned:
            raise ValueError("The model returned no usable requirements.")
        return cleaned

    def extract_requirements(
        self, title: str, reference: str, content_text: str
    ) -> list[dict[str, Any]]:
        """Extract the base circular's requirement list for chain consolidation."""
        user = (
            f"Circular: {reference or title}\nTitle: {title}\n\n"
            f"CIRCULAR TEXT:\n{self._truncate_context(content_text)}"
        )
        schema = self._requirements_schema()
        result = self._complete_json(
            self._REQUIREMENTS_SYSTEM_PROMPT, user, json_schema=schema, temperature=0.0
        )
        try:
            return self._parse_requirement_items(result)
        except ValueError as exc:
            emit_event("parse_error", {
                "parser": "requirements", "error": str(exc),
            }, stage="consolidation.extract")
            emit_event("retry", {
                "reason": "malformed_requirements", "next_attempt": 2,
            }, stage="consolidation.extract")
            retry_system = (
                self._REQUIREMENTS_SYSTEM_PROMPT
                + "\n\nYour previous response was malformed. Return the "
                "schema-compliant JSON object only."
            )
            return self._parse_requirement_items(
                self._complete_json(retry_system, user, json_schema=schema, temperature=0.0)
            )

    _ALIGNMENT_SYSTEM_PROMPT = (
        "You are a conservative SBP regulatory analyst consolidating an "
        "amendment chain. You are given the CURRENT consolidated requirement "
        "list (each item has a req_id) and the full text of an AMENDING "
        "circular. Decide what the amendment changes.\n"
        "Report ONLY the changes — do not restate unchanged requirements. "
        "Amendments typically change a few figures and state that 'other "
        "instructions remain unchanged'; take that literally.\n"
        "Each change is one of:\n"
        "- {\"action\": \"modify\", \"req_id\": \"...\", \"requirement\": "
        "\"the full updated sentence\", \"value\": \"the new key figure as "
        "written in the amending circular\"} — when an existing item's "
        "figure, deadline, scope, or wording is changed. Preserve the exact "
        "new figures from the amending text.\n"
        "- {\"action\": \"add\", \"requirement\": \"...\", \"section\": "
        "\"...\", \"value\": \"...\", \"applies_to\": \"...\"} — when the "
        "amendment introduces a genuinely new requirement that matches no "
        "existing req_id.\n"
        "- {\"action\": \"remove\", \"req_id\": \"...\"} — when the "
        "amendment withdraws or deletes an existing requirement.\n"
        "Never invent figures: every value you output must appear verbatim "
        "in the amending circular's text. If the amendment changes nothing "
        "in the list, return an empty changes array.\n"
        "Return only a JSON object {\"changes\": [...]}."
    )

    @staticmethod
    def _alignment_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "changes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["modify", "add", "remove"],
                            },
                            "req_id": {"type": "string"},
                            "requirement": {"type": "string"},
                            "section": {"type": "string"},
                            "value": {"type": "string"},
                            "applies_to": {"type": "string"},
                        },
                        "required": ["action"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["changes"],
            "additionalProperties": False,
        }

    def _parse_alignment_changes(self, result: str) -> list[dict[str, Any]]:
        parsed = self._parse_json_object(result)
        entries = parsed.get("changes", parsed.get("items"))
        if not isinstance(entries, list):
            raise ValueError("The model returned a payload without a changes array.")
        cleaned: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            action = str(entry.get("action") or "").strip().lower()
            if action not in ("modify", "add", "remove"):
                continue
            cleaned.append({
                "action": action,
                "req_id": str(entry.get("req_id") or "").strip(),
                "requirement": re.sub(r"\s+", " ", str(entry.get("requirement") or "")).strip(),
                "section": re.sub(r"\s+", " ", str(entry.get("section") or "")).strip(),
                "value": re.sub(r"\s+", " ", str(entry.get("value") or "")).strip(),
                "applies_to": re.sub(r"\s+", " ", str(entry.get("applies_to") or "")).strip(),
            })
        return cleaned

    def align_requirements(
        self,
        *,
        current_requirements: list[dict[str, Any]],
        amending_reference: str,
        amending_title: str,
        amending_text: str,
    ) -> list[dict[str, Any]]:
        """Classify what one amending circular changes in the consolidated state."""
        user = (
            f"Amending circular: {amending_reference or amending_title}\n"
            f"Title: {amending_title}\n\n"
            "CURRENT CONSOLIDATED REQUIREMENTS:\n"
            f"{json.dumps(current_requirements, indent=1)}\n\n"
            f"AMENDING CIRCULAR TEXT:\n{self._truncate_context(amending_text)}"
        )
        schema = self._alignment_schema()
        result = self._complete_json(
            self._ALIGNMENT_SYSTEM_PROMPT, user, json_schema=schema, temperature=0.0
        )
        try:
            return self._parse_alignment_changes(result)
        except ValueError:
            retry_system = (
                self._ALIGNMENT_SYSTEM_PROMPT
                + "\n\nYour previous response was malformed. Return the "
                "schema-compliant JSON object only."
            )
            return self._parse_alignment_changes(
                self._complete_json(retry_system, user, json_schema=schema, temperature=0.0)
            )

    def select_superseded(
        self,
        current_title: str,
        subject: str,
        candidates: list[dict],
    ) -> list[str]:
        """Given a blanket supersession on `subject`, decide which candidate circulars it covers.

        `candidates` is a list of {"id", "title", "date", "snippet"} dicts. Returns the subset of
        candidate ids that genuinely concern the same subject and are therefore superseded.
        """
        if not candidates:
            return []
        system = (
            "You are a financial regulations analyst. A newer circular supersedes ALL previous "
            "instructions on a stated subject. From the list of older candidate circulars, identify "
            "ONLY those that concern the SAME subject and are therefore superseded. Be strict: include a "
            "candidate only if its title/content clearly addresses the same subject. Exclude circulars on "
            "merely adjacent or broader topics. Return ONLY valid JSON: "
            '{"superseded_ids": [list of candidate id strings]}.'
        )
        lines = [
            f"Superseding circular title: {current_title}",
            f"Subject superseded: {subject}",
            "",
            "Candidates (older circulars):",
        ]
        for candidate in candidates:
            entry = f"- id={candidate['id']} | date={candidate.get('date', '')} | title={candidate['title']}"
            snippet = candidate.get("snippet")
            if snippet:
                entry += f" | snippet={snippet}"
            lines.append(entry)
        valid_ids = [candidate["id"] for candidate in candidates]
        result = self._complete(
            system,
            "\n".join(lines),
            temperature=0.0,
            json_schema={
                "type": "object",
                "properties": {
                    "superseded_ids": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["superseded_ids"],
                "additionalProperties": False,
            },
        )
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as exc:
            raise ValueError("The model returned invalid JSON for superseded selection.") from exc
        selected = parsed.get("superseded_ids", []) if isinstance(parsed, dict) else []
        if not isinstance(selected, list):
            return []
        allowed = set(valid_ids)
        return [str(item) for item in selected if str(item) in allowed]

    @staticmethod
    def _inline_body_texts(
        *result_groups: list[dict],
        budget: int = SEARCH_INLINE_BODY_BUDGET_CHARS,
        sent: dict[str, list[str]] | None = None,
    ) -> dict[str, str]:
        """Pick which hits are short enough to hand over whole, within one budget.

        `matching_passage` is a 25-word window chosen by term density, which on a
        circular reliably prefers the addressee block ("All Authorized Dealers in
        Foreign Exchange… Attention of Authorized Dealers is invited towards…") to the
        one sentence that states the rule — the salutation of an FX circular is denser
        in an FX query's words than the operative clause is. For a two-page letter the
        fix is not a better window but no window: send the letter.

        The groups are consumed round-robin so the arms share the budget evenly; the
        semantic arm is the one that surfaces a circular whose title looks unrelated,
        and it must not starve because the lexical arm was serialized first. A circular
        in both arms is charged once and inlined in both.
        """
        texts: dict[str, str] = {}
        remaining = budget
        for row in zip_longest(*result_groups):
            for result in row:
                if result is None:
                    continue
                circular = result["circular"]
                if circular.id in texts:
                    continue
                body = (circular.content_text or "").strip()
                if not body or len(body) > SEARCH_INLINE_BODY_MAX_CHARS:
                    continue
                # Sent earlier in this turn: recorded so the serializer can mark it as
                # provided-earlier, but not charged, because the bytes are not going out
                # again. Charging would spend the ceiling on a letter that gets stripped
                # and starve the letters after it, which is the one way this change could
                # make an answer worse.
                if sent is not None and circular.id in sent:
                    texts[circular.id] = body
                    continue
                if len(body) > remaining:
                    return texts
                texts[circular.id] = body
                remaining -= len(body)
        return texts

    @staticmethod
    def _passage_sets(
        *result_groups: list[dict],
        body_texts: dict[str, str],
        budget: int = SEARCH_PASSAGE_BUDGET_CHARS,
        sent: dict[str, list[str]] | None = None,
        sent_passages: dict[str, set[str]] | None = None,
        per_result: int = SEARCH_PASSAGES_PER_RESULT_CHARS,
    ) -> dict[str, list[dict]]:
        """Pick which hits hand over their matched chunks whole, within one budget.

        A 25-word window is a fair preview of prose and a liar on a table. PDF
        extraction interleaves a table's columns, so the window that is densest in the
        query's words lands mid-row: on the Asaan limits table it renders
        "Max Credit Balance16: PKR 1,000,000 … 2 Asaan Account" and stops before the
        figure belonging to the Asaan row, pairing one row's label with another row's
        number. No window size fixes that — the whole chunk does.

        Budgeted round-robin across the arms, on the same fairness argument as
        `_inline_body_texts`: the semantic arm is the one that surfaces a circular whose
        title looks unrelated, and it must not starve because the lexical arm was
        serialized first.

        A circular in both arms is served in both — the lists are meant to be readable
        independently — so it is charged for both. Charging once would let the budget
        understate what actually goes on the wire by about half.

        `sent_passages` is the turn's passage ledger, ``{circular_id: {passage key}}``
        (`chat_retrieval.passage_key`). A passage already in the model's context is
        skipped here — not charged, not sent — so the budget goes to what is new, and a
        repeat circular whose *new* passages survive is served once (`copies` = 1)
        rather than stripped. `per_result` caps one circular's share: with attachment
        evidence no longer cut at three chunks, one annexure could otherwise take the
        whole response's passage budget from the results after it.
        """
        appearances: dict[str, int] = {}
        for group in result_groups:
            for result in group:
                circular_id = result["circular"].id
                appearances[circular_id] = appearances.get(circular_id, 0) + 1

        chosen: dict[str, list[dict]] = {}
        remaining = budget
        for row in zip_longest(*result_groups):
            for result in row:
                if result is None:
                    continue
                circular = result["circular"]
                if circular.id in chosen:
                    continue
                # How many copies of this text the response will actually carry, which is
                # what the budget is for. Without the turn ledger a circular in both arms
                # is serialized in both, so it costs twice. With the ledger only the first
                # occurrence keeps its passages, so it costs once — and if this turn has
                # already sent them, the entry is stripped downstream and costs nothing.
                copies = appearances.get(circular.id, 1)
                if sent is not None:
                    already = circular.id in sent and sent_passages is None
                    copies = 0 if already else 1
                seen = (sent_passages or {}).get(circular.id) or set()
                kept: list[dict] = []
                spent = 0
                for passage in result.get("passages") or []:
                    text = (passage.get("text") or "").strip()
                    # A body chunk repeats text `full_circular_text` already sent whole.
                    if not text or (
                        passage.get("match_source") != "attachment"
                        and circular.id in body_texts
                    ):
                        continue
                    if sent_passages is not None and (
                        _passage_ledger_key(circular.id, passage) in seen
                    ):
                        continue
                    # The first passage is worth exceeding the per-result share for —
                    # a single table chunk can be long — the ones after it are not.
                    if kept and spent + len(text) > per_result:
                        break
                    if spent + len(text) * copies > remaining:
                        break
                    kept.append({**passage, "text": text})
                    spent += len(text) * copies
                if kept:
                    chosen[circular.id] = kept
                    remaining -= spent
        return chosen

    @staticmethod
    def _dedupe_repeat_row(
        payload: dict,
        document_id: str,
        row_keys: tuple[str, ...],
        sent: dict[str, list[str]] | None,
        fresh_keys: tuple[str, ...] = (),
    ) -> dict:
        """Reduce `payload` to a pointer when this turn already serialized the document.

        Returns the payload to send — the original on a document's first appearance, a
        reduced dict on every later one. A `sent` of ``None`` disables the ledger, which
        is what every caller outside a chat turn gets: "already sent" only means something
        within one conversation, and a serializer reached from anywhere else must not
        withhold on the strength of a turn that is not happening.

        What survives is identity and placement — enough to read the row as *this document
        also ranked here*, which is the whole of what a second row was ever telling the
        model. `duplicate_of_earlier_entry` marks it, and `text_provided_earlier` names
        which text keys the earlier row carried, so the model can tell what it already has
        rather than only that it has something. Without that pointer a stripped row reads
        as a document whose text is unavailable, and the model spends a
        `get_circular_details` round recovering what is already in its context — which
        would cost more than the duplicate did.

        Scope is the turn: `docs/CHAT_CONTEXT_PLAN.md` C1a. The known limit is that a later
        row cannot *upgrade* an earlier one — a circular first seen with only an excerpt
        keeps the excerpt even if a later search would have matched real passages. Deciding
        when a second copy is an upgrade is C1's fidelity ladder, and this is deliberately
        the part that needs no such judgement.
        """
        if sent is None:
            return payload
        previous = sent.get(document_id)
        if previous is None:
            sent[document_id] = [key for key in _DOCUMENT_TEXT_KEYS if key in payload]
            return payload
        reduced = {key: payload[key] for key in row_keys if key in payload}
        reduced["duplicate_of_earlier_entry"] = True
        if previous:
            reduced["text_provided_earlier"] = previous
        # Passages the row carries that no earlier row did — the passage ledger has
        # already filtered them (`_search_result_payload`), so anything left under the
        # key is new to the model and travels. Without this a second search that ranks
        # the same annexure by a sharper query hands back a pointer to the *first*
        # search's chunks, and no query the model can write ever reaches the rest of the
        # document: measured on the Basel III liquidity question, six successive
        # searches with the answer at rank 8 of the store, all answered "provided
        # earlier". `_DOCUMENT_TEXT_KEYS`' record is raised so the pointer stays honest.
        for key in fresh_keys:
            if key in payload:
                reduced[key] = payload[key]
                reduced["passages_not_provided_earlier"] = True
                if key not in previous:
                    sent[document_id] = [*previous, key]
        return reduced

    @staticmethod
    def _search_result_payload(
        result: dict,
        body_texts: dict[str, str] | None = None,
        passage_sets: dict[str, list[dict]] | None = None,
        sent: dict[str, list[str]] | None = None,
        sent_passages: dict[str, set[str]] | None = None,
    ) -> dict:
        """Serialize one search result for a tool response.

        `lexical_rank`/`semantic_rank` are carried through when present (the dual-arm
        path sets them) so the model can see where each retriever placed a circular,
        and which ones both arms agreed on. `full_circular_text`, when `_inline_body_texts`
        granted it, is the complete covering letter — but a letter is not a document:
        `attachment_text_chars` reports how much annexure text sits behind it that no
        field here contains, which is what makes a cover letter recognisable as one.

        `sent` is the turn's text ledger. Both retrieval arms serialize from the same
        `body_texts`/`passage_sets` lookups, so a circular in both arms produces two
        byte-identical copies; across calls the same circular comes back again. The
        ledger sends the text once — see `_withhold_repeated_text`.
        """
        circular = result["circular"]
        matching_passage = re.sub(r"</?mark>", "", result.get("snippet") or "")
        attachment_citation = None
        if result.get("attachment_id") and result.get("attachment_filename"):
            attachment_citation = (
                f"[[attachment:{result['attachment_id']}|"
                f"{result['attachment_filename']}]]"
            )
        payload = {
            "title": circular.title,
            "reference": circular.reference,
            "department": circular.department,
            "date": circular.date.strftime("%Y-%m-%d") if circular.date else None,
            "summary": circular.summary[:500] if circular.summary else None,
            "status": circular.status or "active",
            "tags": json.loads(circular.tags) if circular.tags else [],
            "url": circular.url,
            "match_source": result.get("match_source"),
            "attachment_citation": attachment_citation,
            "citation": f"[[circular:{circular.id}|{circular.display_name}]]",
        }

        attachment_chars = sum(
            len(item.content_text or "") for item in circular.attachments
        )
        if attachment_chars:
            payload["attachment_text_chars"] = attachment_chars

        passages = (passage_sets or {}).get(circular.id) or []
        # The passage ledger is consulted at the wire, not in `_passage_sets`: both arms
        # read the same `passage_sets` entry, and it is the first row serialized that
        # carries the passages, so recording them there is what makes the second row a
        # pointer to them rather than a second copy.
        seen = sent_passages.setdefault(circular.id, set()) if sent_passages is not None else None
        if seen is not None:
            passages = [
                item for item in passages
                if _passage_ledger_key(circular.id, item) not in seen
            ]
        if passages:
            payload["matching_passages"] = [
                {
                    "passage": item["text"],
                    "source": item.get("match_source"),
                    "page": item.get("source_page"),
                    "chunk_index": item.get("chunk_index"),
                    "attachment_citation": (
                        f"[[attachment:{item['attachment_id']}|"
                        f"{item['attachment_filename']}]]"
                        if item.get("attachment_id") and item.get("attachment_filename")
                        else None
                    ),
                    **({"context_for_neighbour": True} if item.get("is_neighbour") else {}),
                }
                for item in passages
            ]
            if seen is not None:
                seen.update(_passage_ledger_key(circular.id, item) for item in passages)
        elif matching_passage:
            # Budget spent, or a lexical-only hit with no located chunk. A window is
            # all there is here, so it is labelled as the excerpt it is.
            payload["matching_passage_excerpt"] = matching_passage

        body = (body_texts or {}).get(circular.id)
        if body:
            payload["full_circular_text"] = body
        for key in ("lexical_rank", "semantic_rank"):
            if key in result:
                payload[key] = result[key]
        # Carried straight through from `_relationship_annotation`: what changed this
        # circular, and the instruction to read it. Measured, 64.8% of amended circulars
        # reached the model with no amender anywhere in the result set — this is the
        # field that closes it. See `docs/CHAT_CONTEXT_PLAN.md` C11.
        for key in ("amended_by", "replaced_by", "older_changes_not_shown", "note"):
            if key in result:
                payload[key] = result[key]
        return AIClient._dedupe_repeat_row(
            payload, circular.id, _REPEAT_ROW_KEYS, sent,
            fresh_keys=("matching_passages",) if sent_passages is not None and passages else (),
        )

    @staticmethod
    def _law_search_payloads(
        results: list[dict],
        *,
        budget: int = LAW_SEARCH_PASSAGE_BUDGET_CHARS,
        sent: dict[str, list[str]] | None = None,
    ) -> list[dict]:
        """Serialize the law arm of a search response, under one shared budget.

        Deliberately thinner than `_search_result_payload`. A circular's body is inlined
        whole because a two-page letter usually *is* the answer; a law's never is, so
        what a law result owes the reader is enough passage to tell whether this is the
        instrument worth opening, and a citation to open it with. Anything more spends
        the window on a document the model has not yet decided it needs.
        """
        payloads: list[dict] = []
        remaining = budget
        for result in results:
            document = result["law"]
            version = result.get("version")
            payload = {
                "title": document.title,
                "law_type": document.doc_type,
                "part_label": document.part_label or None,
                "source_url": (version.file_url if version else None) or document.source_url,
                "citation": f"[[law:{document.id}|{document.title}]]",
            }
            if version is not None and version.content_text:
                # What is NOT in this payload, stated as a number, on the same argument
                # `attachment_text_chars` makes for a circular's annexures: a result that
                # looks complete and is 2% of the instrument is how an Act gets answered
                # from a snippet.
                payload["full_text_chars"] = len(version.content_text)
            passages = [item for item in (result.get("passages") or []) if item.get("text")]
            # Serialized as usual and stripped by `_withhold_repeated_text` below when this
            # turn already sent them. Not charged, for the reason `_inline_body_texts`
            # gives: budget spent on bytes that never leave starves the laws after it.
            charged = sent is None or document.id not in sent
            kept: list[dict] = []
            for item in passages:
                text = item["text"].strip()
                if charged and len(text) > remaining:
                    break
                kept.append({
                    "passage": text,
                    "locator": item.get("source_ref"),
                    "page": item.get("source_page"),
                })
                if charged:
                    remaining -= len(text)
            if kept:
                payload["passages"] = kept
            elif result.get("snippet"):
                payload["matching_passage_excerpt"] = re.sub(
                    r"</?mark>", "", result["snippet"]
                )
            for key in ("lexical_rank", "semantic_rank"):
                if key in result:
                    payload[key] = result[key]
            payloads.append(
                AIClient._dedupe_repeat_row(
                    payload, document.id, _REPEAT_LAW_ROW_KEYS, sent
                )
            )
        return payloads

    def _execute_tool(
        self,
        name: str,
        arguments: dict,
        db: Session,
        selected_circular_ids: list[str] | None = None,
        user_query: str = "",
    ) -> str:
        """Execute a tool by name and return the result as a JSON string.
        IDs are exposed only inside opaque citation tokens that the UI can resolve."""
        try:
            if name == "search_selected_documents":
                from .chat_retrieval import ScopedChatRetriever

                if not selected_circular_ids:
                    return json.dumps({"error": "No circulars are selected for this chat"})
                query = str(arguments.get("query", "")).strip()
                if not query:
                    return json.dumps({"error": "No search query provided"})
                limit = max(1, min(int(arguments.get("limit", 5)), 10))
                retriever = ScopedChatRetriever(db, selected_circular_ids)
                # One ledger set across the selection: chunk ids carry their document,
                # so a set per circular buys nothing here.
                sent = set().union(
                    *(self._sent_passages.get(cid, set()) for cid in retriever.circular_ids)
                )
                results = retriever.search(
                    query,
                    limit=limit,
                    token_budget=max(1, self.config.max_context_tokens // 4),
                    sent_chunk_ids=sent,
                )
                by_chunk = {chunk.chunk_id: chunk for chunk in retriever._chunks}
                for chunk_id in retriever.last_sent_chunk_ids:
                    chunk = by_chunk.get(chunk_id)
                    if chunk is not None:
                        self._sent_passages.setdefault(chunk.circular_id, set()).add(chunk_id)
                return json.dumps({"results": results, "count": len(results)})

            if name == "search_corpus":
                from .chat_retrieval import (
                    FRESHNESS_QUERY_PATTERN,
                    WITHDRAWN_QUERY_PATTERN,
                )
                from .search import (
                    WITHDRAWN_STATUSES,
                    _relationship_annotation,
                    _withdrawn_pointer,
                    search_engine,
                )
                query = arguments.get("query", "")
                department = arguments.get("department", "")
                tag = arguments.get("tag", "")
                limit = int(arguments.get("limit", 10))
                # Superseded and cancelled circulars are 13.2% of every result set and
                # are not the rule any more, so they leave the ranked lists unless the
                # question is *about* withdrawal. A circular named outright still
                # arrives via `reference_matches` — see `dual_arm_search`.
                include_withdrawn = bool(WITHDRAWN_QUERY_PATTERN.search(str(query)))

                # "Latest / most recent" questions want date order over relevance, and
                # the fused engine already sorts by date for them. Only ordinary
                # relevance queries go down the dual-arm path.
                if FRESHNESS_QUERY_PATTERN.search(str(query)):
                    results, _ = search_engine.search(
                        query, db, limit=limit,
                        department=department if department else None,
                        tag=tag if tag else None,
                        sort_by="date",
                    )
                    relaxed_department = False
                    if not results and department:
                        results, _ = search_engine.search(
                            query, db, limit=limit,
                            tag=tag if tag else None, sort_by="date",
                        )
                        relaxed_department = bool(results)
                    # The date-sorted branch answers "what is the latest…", which is the
                    # last place a withdrawn circular belongs. `search()` is shared with
                    # the browse UI — where a human *should* be able to find a cancelled
                    # circular — so the rule is applied here rather than in the engine.
                    # Demoted, not dropped, exactly as in the ranked arms.
                    withdrawn_matches = []
                    if not include_withdrawn:
                        kept = []
                        for item in results:
                            if (item["circular"].status or "active") in WITHDRAWN_STATUSES:
                                withdrawn_matches.append(
                                    _withdrawn_pointer(item["circular"])
                                )
                            else:
                                kept.append(item)
                        results = kept
                    for item in results:
                        annotation = _relationship_annotation(item["circular"])
                        if annotation:
                            item.update(annotation)
                    inline_budget, passage_budget, _ = self._search_payload_budgets()
                    sent = self._sent_text_keys
                    sent_passages = self._sent_passages
                    body_texts = self._inline_body_texts(
                        results, budget=inline_budget, sent=sent
                    )
                    passage_sets = self._passage_sets(
                        results, body_texts=body_texts, budget=passage_budget, sent=sent,
                        sent_passages=sent_passages,
                    )
                    return json.dumps({
                        "ranking": "date",
                        **_withdrawn_section(withdrawn_matches),
                        "results": [
                            self._search_result_payload(
                                r, body_texts, passage_sets, sent, sent_passages
                            )
                            for r in results
                        ],
                        "count": len(results),
                        "department_filter_relaxed": relaxed_department,
                    })

                # `department` and `tag` are circular-only concepts, so a filtered call
                # drops the law arm rather than returning laws that never faced the
                # filter beside circulars that did.
                filtered = bool(department or tag)
                arms = search_engine.dual_arm_search(
                    query, db, limit=limit,
                    department=department if department else None,
                    tag=tag if tag else None,
                    include_laws=not filtered,
                    include_withdrawn=include_withdrawn,
                )
                relaxed_department = False
                if department and not any(arms.values()):
                    arms = search_engine.dual_arm_search(
                        query, db, limit=limit, tag=tag if tag else None,
                        include_laws=not bool(tag),
                        include_withdrawn=include_withdrawn,
                    )
                    relaxed_department = bool(any(arms.values()))

                law_results = arms.pop("law_results", [])
                withdrawn_matches = arms.pop("withdrawn_matches", [])
                inline_budget, passage_budget, law_budget = self._search_payload_budgets()
                sent = self._sent_text_keys
                sent_passages = self._sent_passages
                body_texts = self._inline_body_texts(
                    *arms.values(), budget=inline_budget, sent=sent
                )
                passage_sets = self._passage_sets(
                    *arms.values(), body_texts=body_texts, budget=passage_budget, sent=sent,
                    sent_passages=sent_passages,
                )
                # `arms` is ordered reference_matches, lexical_results, semantic_results, so
                # the strongest hit is the one that carries the text and the weaker arms
                # point back at it. Which arm wins does not affect what the model reads —
                # every arm serializes the same lookup, so the copies were identical.
                payload = {
                    key: [
                        self._search_result_payload(
                            r, body_texts, passage_sets, sent, sent_passages
                        )
                        for r in results
                    ]
                    for key, results in arms.items()
                }
                law_payload = self._law_search_payloads(
                    law_results, budget=law_budget, sent=sent
                )
                unique = {
                    item["citation"]
                    for results in payload.values() for item in results
                }
                result = {
                    "ranking": "dual_arm",
                    **payload,
                    **_withdrawn_section(withdrawn_matches),
                    "law_results": law_payload,
                    "count": len(unique) + len(law_payload),
                    "department_filter_relaxed": relaxed_department,
                }
                if filtered:
                    result["laws_excluded_by_filter"] = (
                        "The laws and regulations corpus was not searched: department "
                        "and tag filters apply to circulars only. Repeat the search "
                        "without them to include Acts and regulations."
                    )
                return json.dumps(result)

            elif name == "get_latest_circulars":
                from .models import Circular
                department = arguments.get("department", "")
                limit = int(arguments.get("limit", 5))
                q = db.query(Circular).order_by(Circular.date.desc())
                if department:
                    q = q.filter(Circular.department.ilike(f"%{department}%"))
                rows = q.limit(limit).all()
                out = []
                for c in rows:
                    out.append({
                        "title": c.title,
                        "reference": c.reference,
                        "department": c.department,
                        "date": c.date.strftime("%Y-%m-%d") if c.date else None,
                        "summary": c.summary[:500] if c.summary else None,
                        "status": c.status or "active",
                        "tags": json.loads(c.tags) if c.tags else [],
                        "url": c.url,
                        "citation": f"[[circular:{c.id}|{c.display_name}]]",
                    })
                return json.dumps({"results": out, "count": len(out)})

            elif name == "get_circular_details":
                return self._circular_details_tool(arguments, db, user_query)

            elif name == "read_attachment":
                return self._read_attachment_tool(arguments, db)

            elif name == "get_law_details":
                return self._law_details_tool(arguments, db)

            elif name == "get_circulars_by_tag":
                from .models import Circular
                tag = arguments.get("tag", "")
                limit = int(arguments.get("limit", 10))
                rows = db.query(Circular).filter(
                    Circular.tags.like(f'%"{tag}"%')
                ).order_by(Circular.date.desc()).limit(limit).all()
                out = []
                for c in rows:
                    out.append({
                        "title": c.title,
                        "reference": c.reference,
                        "department": c.department,
                        "date": c.date.strftime("%Y-%m-%d") if c.date else None,
                        "summary": c.summary[:500] if c.summary else None,
                        "status": c.status or "active",
                        "tags": json.loads(c.tags) if c.tags else [],
                        "url": c.url,
                        "citation": f"[[circular:{c.id}|{c.display_name}]]",
                    })
                return json.dumps({"results": out, "count": len(out)})

            elif name == "query_regulatory_values":
                from sqlalchemy import and_ as _and, func as _func, or_ as _or

                from .laws_links import law_label
                from .models import (
                    Circular, CircularEntity, RegDocument, RegDocumentVersion,
                )
                # Outer joins: a law-sourced value has no circular, and an inner join
                # would drop exactly the values most worth answering with.
                query = (
                    db.query(CircularEntity)
                    .outerjoin(Circular, CircularEntity.circular_id == Circular.id)
                    .outerjoin(RegDocument, CircularEntity.document_id == RegDocument.id)
                    .outerjoin(
                        RegDocumentVersion,
                        CircularEntity.version_id == RegDocumentVersion.id,
                    )
                )
                metric = str(arguments.get("metric", "")).strip()
                subject = str(arguments.get("subject", "")).strip()
                entity_type = str(arguments.get("entity_type", "")).strip()
                unit = str(arguments.get("unit", "")).strip()
                comparator = str(arguments.get("comparator", "")).strip()
                if metric:
                    from .search import resolve_metric_terms
                    distinct_metrics = [
                        m[0] for m in db.query(CircularEntity.metric).distinct() if m[0]
                    ]
                    matched = resolve_metric_terms(metric, distinct_metrics)
                    if matched:
                        query = query.filter(CircularEntity.metric.in_(matched))
                    else:
                        query = query.filter(CircularEntity.metric.ilike(f"%{metric}%"))
                if subject:
                    query = query.filter(CircularEntity.subject.ilike(f"%{subject}%"))
                if entity_type:
                    query = query.filter(CircularEntity.entity_type == entity_type)
                if unit:
                    query = query.filter(CircularEntity.unit == unit)
                if comparator:
                    query = query.filter(CircularEntity.comparator == comparator)
                if arguments.get("min_value") is not None:
                    query = query.filter(CircularEntity.value_numeric >= float(arguments["min_value"]))
                if arguments.get("max_value") is not None:
                    query = query.filter(CircularEntity.value_numeric <= float(arguments["max_value"]))
                if arguments.get("current_only"):
                    query = query.filter(_or(
                        _and(
                            CircularEntity.subject_kind == "circular",
                            ~Circular.status.in_(("superseded", "cancelled")),
                        ),
                        _and(
                            CircularEntity.subject_kind == "law",
                            RegDocumentVersion.is_current == 1,
                        ),
                    ))
                limit = max(1, min(int(arguments.get("limit", 20)), 50))
                rows = query.order_by(
                    CircularEntity.effective_date.desc().nullslast(),
                    _func.coalesce(
                        Circular.date, RegDocumentVersion.first_seen_at
                    ).desc().nullslast(),
                ).limit(200).all()
                if arguments.get("current_only"):
                    seen: set[tuple] = set()
                    deduped = []
                    for entity in rows:
                        key = ((entity.metric or "").lower(), (entity.subject or "").lower())
                        if key in seen:
                            continue
                        seen.add(key)
                        deduped.append(entity)
                    rows = deduped
                rows = rows[:limit]
                out = []
                for entity in rows:
                    c = entity.circular
                    d = entity.document
                    row = {
                        "metric": entity.metric,
                        "entity_type": entity.entity_type,
                        "comparator": entity.comparator,
                        "value": entity.value_numeric,
                        "value_high": entity.value_high,
                        "unit": entity.unit,
                        "value_text": entity.value_text,
                        "subject": entity.subject,
                        "effective_date": entity.effective_date.strftime("%Y-%m-%d") if entity.effective_date else None,
                        "context": entity.context_snippet,
                        "source_kind": entity.subject_kind or "circular",
                        "circular_status": (c.status or "active") if c else None,
                        "circular_date": c.date.strftime("%Y-%m-%d") if c and c.date else None,
                        "citation": f"[[circular:{c.id}|{c.display_name}]]" if c else None,
                    }
                    if d is not None:
                        # The instrument that states the value, and whether the edition it
                        # was read from is still the one in force.
                        row["document"] = d.title
                        row["in_force"] = bool(
                            entity.version is not None and entity.version.is_current
                        )
                        row["citation"] = f"[[law:{d.id}|{law_label(d)}]]"
                    out.append(row)
                return json.dumps({"results": out, "count": len(out)})

            elif name == "search_regulatory_inventory":
                return self._inventory_tool(arguments, db)

            else:
                return json.dumps({"error": f"Unknown tool: {name}"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    @staticmethod
    def _resolve_law(db: Session, title: str):
        """Find the law a title names, or nothing.

        Exact, then prefix, then substring, then the law-only search arm. The cascade
        stops at the search arm's *first* result the way `get_circular_details` does,
        with one difference that matters: this reports what it resolved rather than
        presenting it as what was asked for. Handing back a near-match silently is how
        `get_circular_details("State Bank of Pakistan Act, 1956")` answered with a 1999
        cash-reserve circular whose title merely mentions the Act.
        """
        from .models import RegDocument
        from .search import search_engine

        cleaned = title.strip()
        if not cleaned:
            return None
        live = db.query(RegDocument).filter(RegDocument.delisted_at.is_(None))
        for condition in (
            RegDocument.title.ilike(cleaned),
            RegDocument.title.ilike(f"{cleaned}%"),
            RegDocument.title.ilike(f"%{cleaned}%"),
        ):
            match = live.filter(condition).first()
            if match is not None:
                return match
        results, _ = search_engine.search(cleaned, db, limit=1, source="laws")
        return results[0]["law"] if results else None

    @staticmethod
    def _resolve_circular(ref: str, db: Session) -> tuple[Any, dict | None]:
        """One circular for a reference or title, or the error payload to return.

        ``(circular, None)`` on success, ``(None, payload)`` when the reference is
        ambiguous or nothing matched. Shared by `get_circular_details` and
        `read_attachment`, so the two tools resolve "BPRD Circular No. 08 of 2016"
        to the same document — a reader that opens an annexure by a different
        resolution than the search that named it is how the wrong annexure gets read.
        """
        from sqlalchemy import or_

        from .models import Circular
        from .search import SearchEngine, search_engine

        has_year = bool(re.search(r"\b(?:19\d{2}|20\d{2})\b", ref))
        ref_matches = SearchEngine._search_by_reference(ref, db, limit=5)
        if len(ref_matches) > 1 and not has_year:
            return None, {
                "error": (
                    "Ambiguous circular reference. Include the year to "
                    "retrieve a specific circular."
                ),
                "candidates": [
                    {
                        "title": item.title,
                        "reference": item.reference,
                        "department": item.department,
                        "date": item.date.strftime("%Y-%m-%d") if item.date else None,
                        "citation": (
                            f"[[circular:{item.id}|{item.display_name}]]"
                        ),
                    }
                    for item in ref_matches
                ],
            }

        c = ref_matches[0] if ref_matches else None
        # Try exact reference match, then title ILIKE, when the query is
        # not a parsed circular reference.
        if not c:
            c = db.query(Circular).filter(Circular.reference == ref).first()
        if not c:
            c = db.query(Circular).filter(
                or_(
                    Circular.title.ilike(f"%{ref}%"),
                    Circular.reference.ilike(f"%{ref}%"),
                )
            ).first()
        if not c:
            results, _ = search_engine.search(ref, db, limit=1)
            c = results[0]["circular"] if results else None
        if not c:
            return None, {"error": f"Circular not found: {ref}"}
        return c, None

    def _circular_details_tool(self, arguments: dict, db: Session, user_query: str) -> str:
        """One circular: identity, covering letter, and passages from its annexures.

        The passages are retrieved for `query` when the model gives one, else for the
        user's question. Before `query` existed every call retrieved for the original
        question, so a model that had learned from a first read *where* in the annexure
        the answer sat — "Part 1, section 4" — could not act on it: a second call
        returned the same five passages as the first. `read_attachment` is the tool for
        a page or a paragraph by number; this one takes a sharper question.
        """
        ref = str(arguments.get("circular_reference", "")).strip()
        if not ref:
            return json.dumps({"error": "No circular reference provided"})
        c, error = self._resolve_circular(ref, db)
        if error is not None:
            return json.dumps(error)

        from .chat_retrieval import build_chat_context
        from .search import _relationship_annotation

        query = str(arguments.get("query", "") or "").strip() or user_query or ref
        sent = self._sent_passages.setdefault(c.id, set())
        document_context, retriever = build_chat_context(
            db,
            [c.id],
            query,
            self.config.max_context_tokens,
            sent_chunk_ids=sent,
        )
        sent.update(retriever.last_sent_chunk_ids)
        # C11 rule 1: this path never filters on status — a circular the asker
        # named is returned whatever became of it — so the withdrawal has to be
        # *stated* here instead. Handing over a superseded circular's full text
        # with nothing but a `status` field to mark it is how a withdrawn rule
        # gets quoted as current.
        changed = _relationship_annotation(c) or {}
        return json.dumps({
            **changed,
            "title": c.title,
            "reference": c.reference,
            "department": c.department,
            "date": c.date.strftime("%Y-%m-%d") if c.date else None,
            "url": c.url,
            "summary": c.summary,
            "tags": json.loads(c.tags) if c.tags else [],
            "compliance_checklist": compact_required_checklist(c.compliance_checklist),
            "status": c.status or "active",
            "content_preview": (c.content_text or "")[:2000],
            "document_context": document_context,
            "citation": f"[[circular:{c.id}|{c.display_name}]]",
            "attachment_citations": [
                f"[[attachment:{item.id}|{item.filename}]]"
                for item in c.attachments
            ],
        })

    @staticmethod
    def _match_attachment(circular: Any, wanted: str) -> tuple[Any, list[Any]]:
        """The attachment `wanted` names, or the readable candidates when it does not.

        `wanted` may be a filename, a filename without its extension, or the citation
        handle the model was shown (``[[a:C8-Annex]]``, ``[[attachment:…|C8-Annex.pdf]]``).
        An empty `wanted` resolves when the circular has exactly one attachment with
        text, which is the common case: a circular and its annexure.
        """
        readable = [
            item for item in circular.attachments
            if (item.content_text or "").strip()
        ]
        label = wanted.strip()
        if label.startswith("[[") and label.endswith("]]"):
            label = label[2:-2]
            label = label.split("|", 1)[-1] if "|" in label else label.split(":", 1)[-1]
        label = label.strip().casefold()
        if not label:
            return (readable[0] if len(readable) == 1 else None), readable
        slug = re.sub(r"[^a-z0-9]+", "-", label).strip("-")
        for item in readable:
            filename = (item.filename or "").casefold()
            stem = re.sub(r"\.[a-z0-9]{1,5}$", "", filename)
            if label in (filename, stem, item.id.casefold()):
                return item, readable
            if slug and slug == re.sub(r"[^a-z0-9]+", "-", stem).strip("-"):
                return item, readable
        partial = [
            item for item in readable
            if label in (item.filename or "").casefold()
            or (slug and slug in re.sub(r"[^a-z0-9]+", "-", (item.filename or "").casefold()))
        ]
        if len(partial) == 1:
            return partial[0], readable
        return None, readable

    def _read_attachment_tool(self, arguments: dict, db: Session) -> str:
        """Read inside one circular attachment — the annexure analogue of `get_law_details`.

        The gap it closes, from session `15fd3ff1` (2026-09-08): asked what Basel III
        counts as a "stable" deposit, the model read the annexure's contents page,
        named the section holding the answer, and had no way to ask for it. Six
        `search_corpus` calls later it answered that the section was "not available".
        The text was on page 15 of a 63-page attachment the corpus had held for two
        months.
        """
        from .chat_retrieval import ScopedAttachmentRetriever

        ref = str(arguments.get("circular_reference", "")).strip()
        if not ref:
            return json.dumps({"error": "No circular reference provided"})
        circular, error = self._resolve_circular(ref, db)
        if error is not None:
            return json.dumps(error)

        wanted = str(arguments.get("attachment", "") or "")
        attachment, readable = self._match_attachment(circular, wanted)
        citation = f"[[circular:{circular.id}|{circular.display_name}]]"
        if attachment is None:
            if not readable:
                return json.dumps({
                    "error": "This circular has no attachment with extracted text.",
                    "citation": citation,
                    "attachments": [
                        {
                            "attachment_citation": f"[[attachment:{item.id}|{item.filename}]]",
                            "extraction_status": item.extraction_status or "unknown",
                        }
                        for item in circular.attachments
                    ],
                })
            return json.dumps({
                "error": (
                    "Name which attachment to read — this circular has more than one."
                    if not wanted.strip()
                    else "No attachment of this circular matches that name."
                ),
                "citation": citation,
                "attachments": [
                    {
                        "attachment_citation": f"[[attachment:{item.id}|{item.filename}]]",
                        "text_chars": len(item.content_text or ""),
                    }
                    for item in readable
                ],
            })

        budget = max(1, self.config.max_context_tokens // 4)
        retriever = ScopedAttachmentRetriever(attachment)
        query = str(arguments.get("query", "") or "").strip()
        section = str(arguments.get("section", "") or "").strip()
        limit = max(1, min(int(arguments.get("limit", 5) or 5), 10))
        page: int | None = None
        raw_page = arguments.get("page")
        if raw_page not in (None, ""):
            try:
                page = int(str(raw_page).strip().lstrip("pP").strip(". "))
            except ValueError:
                return json.dumps({"error": f"`page` must be a number, got {raw_page!r}"})

        pages = retriever.pages
        passages: list[dict] = []
        notes: list[str] = []
        if page is not None:
            passages = retriever.page(page, token_budget=budget)
            if not passages:
                span = f"{pages[0]}-{pages[-1]}" if pages else "none"
                notes.append(
                    f"No page {page} in this attachment (pages with text: {span})."
                )
        if not passages and section:
            passages = retriever.section(section, token_budget=budget)
            if not passages:
                notes.append(
                    f"No paragraph or section numbered {section} was located; the "
                    "passages below, if any, come from the query instead."
                )
        if not passages and query:
            passages = retriever.search(query, limit=limit, token_budget=budget)
        if not passages and not (query or section or page is not None):
            passages = retriever.search(
                circular.title or attachment.filename, limit=limit, token_budget=budget
            )
        if not passages:
            notes.append(
                "Nothing in this attachment matched. Do not infer its contents — say "
                "what you could not find."
            )

        self._sent_passages.setdefault(circular.id, set()).update(
            retriever.chunk_ids(passages)
        )
        payload = {
            "circular": circular.reference or circular.title,
            "citation": citation,
            "attachment_citation": f"[[attachment:{attachment.id}|{attachment.filename}]]",
            "filename": attachment.filename,
            "full_text_chars": len(attachment.content_text or ""),
            "chunk_count": retriever.chunk_count,
            "pages": (
                {"first": pages[0], "last": pages[-1], "count": len(pages)}
                if pages else None
            ),
            "passages": passages,
            "passage_count": len(passages),
        }
        if notes:
            payload["note"] = " ".join(notes)
        return json.dumps(payload)

    def _law_details_tool(self, arguments: dict, db: Session) -> str:
        """Read inside one law — the statute analogue of `get_circular_details`."""
        from .chat_retrieval import ScopedLawRetriever

        requested = str(arguments.get("law_title", "")).strip()
        if not requested:
            return json.dumps({"error": "No law title provided"})

        document = self._resolve_law(db, requested)
        if document is None:
            return json.dumps({
                "error": f"No law, Act or regulation found matching: {requested}",
                "note": (
                    "The corpus does not hold this instrument. Say so rather than "
                    "answering from a circular that mentions it."
                ),
            })

        version = document.current_version
        if version is None or not (version.content_text or "").strip():
            return json.dumps({
                "error": f"No readable text in force for: {document.title}",
                "resolved_title": document.title,
                "citation": f"[[law:{document.id}|{document.title}]]",
            })

        budget = max(1, self.config.max_context_tokens // 4)
        retriever = ScopedLawRetriever(db, document)
        section = str(arguments.get("section", "")).strip()
        query = str(arguments.get("query", "")).strip()
        limit = max(1, min(int(arguments.get("limit", 5)), 10))

        passages: list[dict] = []
        if section:
            passages = retriever.section(section, token_budget=budget)
        if not passages and query:
            passages = retriever.search(query, limit=limit, token_budget=budget)
        if not passages and not query and not section:
            passages = retriever.search(document.title, limit=limit, token_budget=budget)

        payload = {
            "requested": requested,
            "resolved_title": document.title,
            "law_type": document.doc_type,
            "part_label": document.part_label or None,
            "parent_title": document.parent.title if document.parent else None,
            "source_url": version.file_url or document.source_url,
            "effective_from": (
                version.effective_from.strftime("%Y-%m-%d")
                if version.effective_from else None
            ),
            "full_text_chars": len(version.content_text or ""),
            "citation": f"[[law:{document.id}|{document.title}]]",
            "passages": passages,
            "passage_count": len(passages),
        }
        if section and not passages:
            payload["note"] = (
                f"No provision numbered {section} was located in this document. The "
                "passages below, if any, come from the query instead."
            )
        if not passages:
            payload["note"] = (
                "Nothing in this document matched. Do not infer its contents — say what "
                "you could not find."
            )
        return json.dumps(payload)

    def _inventory_tool(self, arguments: dict, db: Session) -> str:
        """Adapter only — the search semantics live in InventorySearchService.

        Imported lazily: `inventory.llm` reaches back into this module for its client,
        so a module-level import here would close the cycle. It also keeps chat off the
        inventory package's import path when the tool is never called.
        """
        from .inventory.schemas import (
            InventoryError,
            InventoryFilters,
            InventorySearchRequest,
        )
        from .inventory.service import InventorySearchService
        from .database import collection, embedding_backend, embedding_config
        from .inventory.llm import AIClientAdapter
        from .models import Circular

        query = str(arguments.get("query", "")).strip()
        if not query:
            return json.dumps({"error": "No subject provided to take inventory of"})

        source = str(arguments.get("sources", "all")).strip().lower()
        sources = ["circulars", "laws"] if source not in ("circulars", "laws") else [source]
        # No arbitrary row cap: a fixed ceiling turns "every document that mentions X"
        # into "some documents that mention X", which is the one thing this feature
        # exists not to do. What genuinely bounds the answer is the context window, so
        # that is what bounds it — measured and disclosed below, never silently.
        requested = arguments.get("limit")
        limit = max(1, int(requested)) if requested is not None else _INVENTORY_MAX_ROWS

        department = str(arguments.get("department", "")).strip()
        start_year = arguments.get("start_year")
        end_year = arguments.get("end_year")
        filters = InventoryFilters(
            departments=[department] if department else [],
            start_year=int(start_year) if start_year is not None else None,
            end_year=int(end_year) if end_year is not None else None,
        )

        request = InventorySearchRequest(
            query=query,
            sources=sources,
            filters=filters,
            # Adjudication is 60+ s per batch of 12 and a chat turn cannot wait for it.
            # Term generation stays on: it is one call, and it is the recall backbone —
            # without it a question-shaped query retrieves almost nothing. HyDE is off
            # because the dense arm is only a supplement and it costs another call.
            generate_terms=True,
            use_hyde=False,
            skip_adjudication=True,
            extract_spans=False,
            max_results=limit,
            evidence_per_result=1,
        )
        request.max_candidates = max(request.max_candidates, _INVENTORY_MAX_ROWS)

        try:
            response = InventorySearchService(
                collection, embedding_backend, embedding_config,
                llm=AIClientAdapter(self),
            ).search(request, db)
        except InventoryError as exc:
            return json.dumps({"error": f"[{exc.code}] {exc}"})

        # Roughly four characters per token, matching `_estimate_tokens` elsewhere. A
        # quarter of the window is the same share `search_selected_documents` takes.
        budget = max(1, self.config.max_context_tokens // 4) * 4
        spent = 0
        omitted_for_size = 0

        results = []
        for result in response.results:
            evidence = result.evidence[0] if result.evidence else None
            item = {
                "title": result.title,
                "matched_terms": result.matched_terms[:6],
                "passage": (evidence.passage[:_INVENTORY_PASSAGE_CHARS] if evidence else ""),
                "locator": _inventory_locator(evidence),
            }
            if result.result_kind == "circular":
                row = db.query(Circular).filter(Circular.id == result.document_id).first()
                item["reference"] = result.reference
                item["date"] = result.date
                item["department"] = result.department
                if row is not None:
                    item["citation"] = f"[[circular:{row.id}|{row.display_name}]]"
                # The passage text opens with the chunk's "{filename}. Page N. " prefix,
                # so withholding the attachment token shows the model a filename it is
                # asked to cite and gives it nothing to cite with. Measured: it invented
                # `[[attachment:C2-AML-CFT-Regulations.pdf|...]]` from exactly that gap.
                if evidence is not None and evidence.source_kind == "attachment":
                    item["attachment_citation"] = (
                        f"[[attachment:{evidence.source_id}|{evidence.source_label}]]"
                    )
            else:
                item["law_type"] = result.law_type
                item["parent_title"] = result.parent_title
                # The laws reader resolves `[[law:<id>]]` and has since the corpus
                # shipped; withholding it left the model naming an Act it had no way to
                # cite, which is the gap it filled by inventing attachment handles.
                item["citation"] = f"[[law:{result.document_id}|{result.title}]]"

            cost = len(json.dumps(item, ensure_ascii=False))
            if results and spent + cost > budget:
                omitted_for_size += 1
                continue
            spent += cost
            results.append(item)

        coverage = response.coverage
        dropped = response.results_truncated + omitted_for_size
        payload = {
            "reviewed": False,
            "note": (
                "Unreviewed candidates: each document contains a search term, but "
                "nothing has judged whether it discusses the subject. Check each "
                "passage before citing it."
            ),
            "search_terms": response.retrieval_policy.term_set,
            "documents_matched": coverage.candidates_union,
            "documents_returned": len(results),
            "complete": not dropped,
            "results": results,
        }
        if dropped:
            # Say it in the payload, not just as a flag: the model has to pass this on
            # or it will present a partial list as an exhaustive one.
            payload["omitted"] = dropped
            payload["note"] += (
                f" This list is INCOMPLETE: {dropped} further matching document(s) "
                "were not included. Tell the user the list was cut short and how many "
                "were left out."
            )
        return json.dumps(payload)

    def _chat_system_prompt(self, circulars_context: str | None = None) -> str:
        """The system prompt for a chat turn, with or without pre-selected circulars.

        Both branches compose `_CITATION_RULES` and `_ANSWER_CONTRACT` so the only text
        that differs between them is the framing and the tool routing, which is the only
        thing that genuinely does differ. Editing a shared rule in one branch and not the
        other is how the three copies drifted before.
        """
        if circulars_context:
            return f"""You are an expert assistant for analyzing State Bank of Pakistan (SBP) circulars and regulations.
You have been provided with pre-selected circulars as context below. Answer primarily from these,
but you also have tools to search the database if the user asks about circulars not covered here.

{_CITATION_RULES}

TOOLS
- Be precise and highlight regulatory differences when comparing circulars.
- Use search_selected_documents when the included passages do not contain enough detail. It
can search the complete selected circulars and their attachments. Do not claim attachment
content is unavailable merely because it was not included in the initial context.
- Use global circular search tools only when the user explicitly requests broader research.

{_ANSWER_CONTRACT}

Pre-selected circulars:
{circulars_context}"""
        return f"""You are an expert assistant for SBP circulars and regulations.
The database holds two corpora: SBP circulars and circular letters, and the laws corpus —
Acts of Parliament, Prudential Regulations, and guidelines. search_corpus searches both.
Use your tools to search and retrieve relevant documents before answering.

{_CITATION_RULES}

TOOLS
- If you need more details on a circular found in a search, use the get_circular_details
tool with the circular reference or title, and give it a `query` for what you need.
- When the answer sits inside an attachment — an annexure, framework, instructions or
guidelines PDF behind a covering letter — read it with read_attachment. It takes a
`page`, a paragraph or section number (`section`), or a `query`, and returns the matched
chunks with their neighbours. A search result that names the page or section you need is
a reason to call it, not a reason to search again.
- When the answer depends on what an Act or set of Regulations says, read the instrument
itself with get_law_details. get_circular_details searches circulars only and cannot fetch
an Act; a circular that cites an Act is not a source for what the Act requires.
- If the instrument you need is not in the corpus, say so plainly. Never substitute a
circular on an adjacent topic for a statute you could not retrieve.

{_ANSWER_CONTRACT}"""

    def _chat_full_messages(
        self,
        messages: list[dict[str, str]],
        circulars_context: str | None,
        selected_circular_ids: list[str] | None,
        handles: CitationHandles | None = None,
    ) -> list[dict]:
        """Prepend the (context-aware) system prompt to the conversation.

        Given a handle map, the selected-circular context and every replayed assistant
        turn are rewritten so the model sees short handles instead of ids. Rewriting the
        history is not optional: answers are persisted with real tokens and the routes
        rebuild the conversation from those rows, so a turn-scoped map alone would leave
        the model reading uuids off its own transcript from the second turn onward.
        """
        if handles is not None:
            circulars_context = handles.to_handles(circulars_context)
            messages = _messages_with_handles(messages, handles)
        system_prompt = self._chat_system_prompt(
            circulars_context if selected_circular_ids else None
        )
        return [{"role": "system", "content": system_prompt}] + messages

    def _apply_tool_calls(
        self,
        full_messages: list[dict],
        assistant_content: str,
        tool_call_dicts: list[dict],
        db: Session,
        selected_circular_ids: list[str] | None,
        handles: CitationHandles | None = None,
    ) -> None:
        """Record the assistant's tool requests, run each tool, and append its result.

        `tool_call_dicts` are normalized to the OpenAI on-the-wire shape so the streaming
        and non-streaming paths share this loop.
        """
        full_messages.append({
            "role": "assistant",
            "content": assistant_content,
            "tool_calls": tool_call_dicts,
        })
        user_query = next(
            (
                str(item.get("content") or "")
                for item in reversed(full_messages)
                if item.get("role") == "user"
            ),
            "",
        )
        for tc in tool_call_dicts:
            emit_event("tool_request", {
                "tool_call_id": tc.get("id"),
                "name": (tc.get("function") or {}).get("name"),
                "raw_arguments": (tc.get("function") or {}).get("arguments"),
            }, stage="chat.tools")
            tool_started = time.monotonic()
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError as exc:
                args = {}
                emit_event("parse_error", {
                    "parser": "tool_arguments", "tool_call_id": tc.get("id"),
                    "error": str(exc),
                }, stage="chat.tools")
            try:
                result = self._execute_tool(
                    tc["function"]["name"], args, db,
                    selected_circular_ids, user_query,
                )
                # Digested from the payload as the tools built it, before the rewrite
                # below: the step is read by a person, and a person needs the real
                # citation tokens the UI resolves, not the model's handles.
                self._turn_steps.append(build_step(
                    tc["function"]["name"], args, result,
                    label=tool_activity_label(tc["function"]["name"]),
                    elapsed_ms=round((time.monotonic() - tool_started) * 1000),
                ))
                if handles is not None:
                    # Recorded post-rewrite so the trace shows what the model was
                    # actually handed, handles and all.
                    result = handles.to_handles(result)
                emit_event("tool_result", {
                    "tool_call_id": tc.get("id"), "name": tc["function"]["name"],
                    "arguments": args, "result": result, "success": True,
                }, stage="chat.tools", elapsed_ms=round((time.monotonic() - tool_started) * 1000))
            except Exception as exc:
                emit_event("tool_result", {
                    "tool_call_id": tc.get("id"), "name": tc["function"]["name"],
                    "arguments": args, "success": False, "error": exception_payload(exc),
                }, stage="chat.tools", elapsed_ms=round((time.monotonic() - tool_started) * 1000))
                # A turn that dies here still persists what it managed to answer, so
                # the step that killed it belongs in the record beside the rest.
                self._turn_steps.append(failed_step(
                    tc["function"]["name"], args,
                    label=tool_activity_label(tc["function"]["name"]),
                    error=str(exc) or exc.__class__.__name__,
                    elapsed_ms=round((time.monotonic() - tool_started) * 1000),
                ))
                raise
            full_messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

    def chat(
        self,
        messages: list[dict[str, str]],
        db: Session,
        circulars_context: str | None = None,
        selected_circular_ids: list[str] | None = None,
    ) -> str:
        with trace_operation(
            "chat.turn", "implicit", provider=self.config.provider,
            model=self.config.effective_chat_model,
        ):
            return self._chat_impl(
                messages, db, circulars_context, selected_circular_ids
            )

    def _chat_impl(
        self,
        messages: list[dict[str, str]],
        db: Session,
        circulars_context: str | None = None,
        selected_circular_ids: list[str] | None = None,
    ) -> str:
        handles = CitationHandles()
        # One turn, one ledger. `AIClient` is built per request so this is already empty,
        # but the reset states the scope rather than relying on the caller's lifecycle.
        self._sent_text_keys = {}
        self._sent_passages = {}
        self._turn_steps = []
        full_messages = self._chat_full_messages(
            messages, circulars_context, selected_circular_ids, handles
        )

        for iteration in range(_MAX_TOOL_ITERATIONS):
            response = self._create_traced_completion(
                stage=f"chat.iteration.{iteration + 1}",
                model=self.config.effective_chat_model,
                messages=full_messages,
                temperature=0.3,
                tools=tools_for_turn(selected_circular_ids),
                tool_choice="auto",
            )

            msg = _first_choice_message(response)
            if msg is None:
                # Raised, not swallowed: the chat routes already funnel exceptions
                # through friendly_chat_error, so this reaches the user as a clear
                # "try again" rather than as an empty assistant turn that looks like
                # the model chose to say nothing.
                raise _empty_response_error(
                    self.config.provider, self.config.effective_chat_model
                )

            if not msg.tool_calls:
                return _expanded_answer(msg.content or "", handles)

            self._apply_tool_calls(
                full_messages,
                msg.content or "",
                [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
                db,
                selected_circular_ids,
                handles,
            )

        # Fallback if max iterations reached. Use a fresh synthesis prompt so
        # models that keep requesting tools do not see prior tool-call messages.
        synthesis_messages = self._tool_result_synthesis_messages(
            _messages_with_handles(messages, handles),
            full_messages,
            handles.to_handles(circulars_context),
        )
        try:
            final_response = self._create_traced_completion(
                stage="chat.final_synthesis",
                model=self.config.effective_chat_model,
                messages=synthesis_messages,
                temperature=0.3,
            )
        except APIError as exc:
            if self._is_tool_choice_none_error(exc):
                return self._tool_iteration_limit_message()
            raise
        content = _first_choice_content(final_response)
        if content is None:
            raise _empty_response_error(
                self.config.provider, self.config.effective_chat_model
            )
        return _expanded_answer(content, handles)

    def stream_chat(
        self,
        messages: list[dict[str, str]],
        db: Session,
        circulars_context: str | None = None,
        selected_circular_ids: list[str] | None = None,
    ):
        # Pinned to one context so the trace opened below — and any trace the
        # caller already opened around this loop — stays current across every
        # resumption of the stream, whichever thread drives it.
        yield from bind_context(
            self._stream_chat_traced(
                messages, db, circulars_context, selected_circular_ids
            )
        )

    def _stream_chat_traced(
        self,
        messages: list[dict[str, str]],
        db: Session,
        circulars_context: str | None = None,
        selected_circular_ids: list[str] | None = None,
    ):
        with trace_operation(
            "chat.turn", "implicit", provider=self.config.provider,
            model=self.config.effective_chat_model,
        ):
            yield from self._stream_chat_impl(
                messages, db, circulars_context, selected_circular_ids
            )

    def _stream_chat_impl(
        self,
        messages: list[dict[str, str]],
        db: Session,
        circulars_context: str | None = None,
        selected_circular_ids: list[str] | None = None,
    ):
        handles = CitationHandles()
        # One turn, one ledger. `AIClient` is built per request so this is already empty,
        # but the reset states the scope rather than relying on the caller's lifecycle.
        self._sent_text_keys = {}
        self._sent_passages = {}
        self._turn_steps = []
        full_messages = self._chat_full_messages(
            messages, circulars_context, selected_circular_ids, handles
        )

        for iteration in range(_MAX_TOOL_ITERATIONS):
            yield {"phase": "thinking"}
            stream = self._create_traced_completion(
                stage=f"chat.iteration.{iteration + 1}",
                model=self.config.effective_chat_model,
                messages=full_messages,
                temperature=0.3,
                tools=tools_for_turn(selected_circular_ids),
                tool_choice="auto",
                stream=True,
            )

            content_parts: list[str] = []
            tool_calls: dict[int, dict] = {}
            saw_choice = False
            # Deltas split a citation anywhere, so what reaches the client is the
            # expander's output rather than the provider's — the route downstream
            # persists exactly the text the reader saw, with real tokens in it.
            expander = StreamExpander(handles)

            for chunk in stream:
                if not chunk.choices:
                    continue
                saw_choice = True
                delta = chunk.choices[0].delta
                if delta.content:
                    content_parts.append(delta.content)
                    ready = expander.feed(delta.content)
                    if ready:
                        yield ready

                for tc in delta.tool_calls or []:
                    index = tc.index
                    item = tool_calls.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tc.id:
                        item["id"] = tc.id
                    if tc.type:
                        item["type"] = tc.type
                    if tc.function:
                        if tc.function.name:
                            item["function"]["name"] += tc.function.name
                        if tc.function.arguments:
                            item["function"]["arguments"] += tc.function.arguments

            # An empty `choices` on an individual chunk is normal framing and is skipped
            # above. A whole stream without a single one is the streaming form of the
            # empty-body fault, and would otherwise end the turn silently — the user
            # sees a blank answer and no reason for it. Only status markers have been
            # yielded so far, never assistant text, so the route's handler turns this
            # into an error event rather than truncating a half-written reply.
            if not saw_choice:
                raise _empty_response_error(
                    self.config.provider, self.config.effective_chat_model, "streamed"
                )

            trailing = expander.close()
            if trailing:
                yield trailing
            _report_dropped_citations(handles)

            if not tool_calls:
                return

            ordered_calls = [tool_calls[index] for index in sorted(tool_calls)]
            yield {
                "phase": "tools",
                "tools": [
                    tool_activity_label(call["function"]["name"])
                    for call in ordered_calls
                ],
                # Where this round's calls will land in `turn_steps`. The narration
                # that belongs to them is assembled by the route from streamed tokens
                # and cannot be passed down here, so the route is told which steps to
                # attach it to instead. Read before `_apply_tool_calls` runs, so it is
                # the index of the round's first call.
                "step_offset": len(self._turn_steps),
            }

            self._apply_tool_calls(
                full_messages,
                "".join(content_parts),
                ordered_calls,
                db,
                selected_circular_ids,
                handles,
            )

        yield {"phase": "thinking"}
        synthesis_messages = self._tool_result_synthesis_messages(
            _messages_with_handles(messages, handles),
            full_messages,
            handles.to_handles(circulars_context),
        )
        try:
            final_stream = self._create_traced_completion(
                stage="chat.final_synthesis",
                model=self.config.effective_chat_model,
                messages=synthesis_messages,
                temperature=0.3,
                stream=True,
            )
            final_expander = StreamExpander(handles)
            for chunk in final_stream:
                if not chunk.choices:
                    continue
                content = chunk.choices[0].delta.content
                if content:
                    ready = final_expander.feed(content)
                    if ready:
                        yield ready
            trailing = final_expander.close()
            if trailing:
                yield trailing
            _report_dropped_citations(handles)
        except APIError as exc:
            if self._is_tool_choice_none_error(exc):
                yield self._tool_iteration_limit_message()
                return
            raise

    def check_availability(self) -> dict:
        """Lightweight reachability/auth probe for the configured provider.

        Uses ``models.list()`` (with a short timeout and no retries) so it does
        not consume chat tokens or count against tight generation rate limits.
        Returns a coarse state the sidebar can render without exposing raw
        provider payloads.
        """
        try:
            self._client.with_options(timeout=5.0, max_retries=0).models.list()
        except Exception as exc:
            state, detail = classify_provider_state(exc)
            return {
                "available": False,
                "state": state,
                "detail": detail,
                "provider": self.config.provider,
                "model": self.config.effective_chat_model,
            }
        return {
            "available": True,
            "state": "online",
            "detail": "Backend reachable",
            "provider": self.config.provider,
            "model": self.config.effective_chat_model,
        }

    def test_connection(self) -> dict:
        with trace_operation(
            "settings.connection_test", "connection_test",
            provider=self.config.provider, model=self.config.model,
        ) as trace:
            result = self._test_connection_impl()
            emit_event("normalized_result", result, stage="settings.connection_test")
            if not result.get("success"):
                finish_trace(
                    trace, "failed",
                    RuntimeError(str(result.get("error") or "Connection test failed")),
                )
            return result

    def _test_connection_impl(self) -> dict:
        try:
            response = self._create_traced_completion(
                stage="settings.connection_test",
                model=self.config.model,
                messages=[{"role": "user", "content": "Say 'Connection successful.'"}],
                max_tokens=10,
                temperature=0.0,
            )
            content = _first_choice_content(response)
            if content is None:
                # Reaching the provider but getting nothing back is a failed connection
                # test, not a successful one with an empty string.
                return {
                    "success": False,
                    "error": str(
                        _empty_response_error(self.config.provider, self.config.model)
                    ),
                }
            return {"success": True, "response": content}
        except Exception as e:
            return {"success": False, "error": str(e)}


def get_ai_client(db=None) -> AIClient:
    """Build a client from the stored provider settings, falling back to the environment.

    `db` is accepted but no longer read: `settings` moved to the runtime-state database,
    while most of the seventeen call sites hold a corpus session. Rather than thread a
    second session through all of them, the config is read from its own short-lived app
    session here. The parameter stays so those call sites — and the tests that stub this
    function — are unaffected.
    """
    session = AppSessionLocal()
    try:
        config = AIConfig.from_db(session)
    finally:
        session.close()
    if config is None:
        config = AIConfig.from_env()
    return AIClient(config)


def encrypt_key_for_storage(api_key: str) -> str:
    """Wrapper so routes do not import `auth` for one call, and so the encryption used
    for stored provider keys has exactly one entry point."""
    from .auth import encrypt_secret

    return encrypt_secret(api_key)


def get_ai_client_for_user(user) -> AIClient:
    """The client for a user-initiated request, built from that user's own credentials.

    Separate from `get_ai_client` on purpose. That one resolves the deployment-level
    config and drives admin-triggered corpus generation, which is the admin's spend under
    1.3. This one drives chat, which is per-user and unmetered, and it refuses to fall
    back — a tester without a key gets told to add one rather than quietly billing
    somebody else.
    """
    config = AIConfig.for_user(user)
    if config is None:
        raise MissingUserAIConfig(
            "Add your own AI provider API key in Settings before using chat. "
            "Each account uses its own credentials on this deployment."
        )
    client = AIClient(config)
    # `AIConfig.for_user` cannot fill this in: it has no client to ask the provider
    # with, so it left the dataclass default of 4,000 — which is not this model's
    # window, or any model's. Nothing in chat reads the field's other meaning (the
    # character clip in `_truncate_context` belongs to the corpus generation paths,
    # which build their config with `get_ai_client`), so what belongs in it here is
    # the turn share for the model actually in use. `build_chat_context` spends it
    # across two retrieval arms at `// 4` each, hence two halves of one share.
    #
    # Both ends of this were measured on the same deployment. Left at 4,000, a
    # 1,310,720-token model grounded a turn on 1,000 tokens per arm; set to the raw
    # window — which is what the settings row still holds — one turn's context reached
    # 927,622 characters and the request 285,007 prompt tokens.
    config.max_context_tokens = client.resolve_turn_share() * 2
    return client
