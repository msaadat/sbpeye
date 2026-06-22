import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from openai import BadRequestError, OpenAI

from sqlalchemy.orm import Session

from .checklist import compact_required_checklist


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
            "name": "search_circulars",
            "description": "Search SBP circulars by keyword, topic, department, or tag. Use this when the user asks for circulars on a specific subject, regulation, or topic. Returns matching circulars with title, date, department, reference, and summary.",
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
            "description": "Fetch the full details of a specific circular by its reference number or title. Use this when the user refers to a specific circular by reference (e.g. 'BPRD Circular No. 12 of 2023') or when you need the complete content of a circular found in a search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "circular_reference": {"type": "string", "description": "The circular's reference number, e.g. 'BPRD Circular No. 12 of 2023' or title"}
                },
                "required": ["circular_reference"]
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
    }
]

@dataclass
class AIConfig:
    provider: str = "lmstudio"
    base_url: str = "http://localhost:1234/v1"
    api_key: str = "lm-studio"
    model: str = "local-model"
    chat_model: str = ""
    max_context_tokens: int = 4000

    @property
    def effective_chat_model(self) -> str:
        return self.chat_model or self.model

    @staticmethod
    def from_env() -> "AIConfig":
        return AIConfig(
            provider=os.getenv("AI_PROVIDER", "lmstudio"),
            base_url=os.getenv("AI_BASE_URL", "http://localhost:1234/v1"),
            api_key=os.getenv("AI_API_KEY", "lm-studio"),
            model=os.getenv("AI_MODEL", "local-model"),
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
            return AIConfig(
                provider=kv.get("ai_provider", "lmstudio"),
                base_url=kv.get("ai_base_url", "http://localhost:1234/v1"),
                api_key=kv.get("ai_api_key", "lm-studio"),
                model=kv.get("ai_model", "local-model"),
                chat_model=kv.get("ai_chat_model", ""),
                max_context_tokens=int(kv.get("ai_max_context_tokens", "4000")),
            )
        except Exception:
            return None

    def save_to_db(self, db):
        from .models import Settings
        for key, value in [
            ("ai_provider", self.provider),
            ("ai_base_url", self.base_url),
            ("ai_api_key", self.api_key),
            ("ai_model", self.model),
            ("ai_chat_model", self.chat_model),
            ("ai_max_context_tokens", str(self.max_context_tokens)),
        ]:
            existing = db.query(Settings).filter(Settings.key == key).first()
            if existing:
                existing.value = value
            else:
                db.add(Settings(key=key, value=value))
        db.commit()


class AIClient:
    def __init__(self, config: AIConfig | None = None):
        if config is None:
            config = AIConfig.from_env()
        self.config = config
        self._client = self._create_client()

    def _create_client(self) -> OpenAI:
        if self.config.provider == "google":
            return OpenAI(
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                api_key=self.config.api_key,
            )
        return OpenAI(
            base_url=self.config.base_url,
            api_key=self.config.api_key,
        )

    def _complete(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str = "",
        temperature: float = 0.0,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        model = model or self.config.model
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
        }
        if json_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "sbpeye_response",
                    "strict": True,
                    "schema": json_schema,
                },
            }
        try:
            response = self._client.chat.completions.create(**kwargs)
        except BadRequestError as exc:
            if not json_schema or "response_format" not in str(exc):
                raise
            # Some OpenAI-compatible local servers only accept text responses.
            kwargs["response_format"] = {"type": "text"}
            response = self._client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""

    def _complete_chat(self, messages: list[dict[str, str]], model: str = "", temperature: float = 0.3) -> str:
        model = model or self.config.effective_chat_model
        response = self._client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
        )
        return response.choices[0].message.content or ""

    def summarize(self, title: str, content_text: str) -> str:
        system = "You are a concise financial regulations analyst. Summarize the following SBP circular in 3-5 sentences, focusing on the key regulatory changes, requirements, and impact on banks/DFIs/MFBs. Be factual and specific."
        truncated = content_text[: self.config.max_context_tokens] if len(content_text) > self.config.max_context_tokens else content_text
        user = f"Title: {title}\n\nContent:\n{truncated}"
        result = self._complete(system, user, temperature=0.2)
        return result.strip()

    def generate_tags(self, title: str, content_text: str) -> list[str]:
        system = f"You are a financial regulations classifier. Select the most relevant tags from the following taxonomy that apply to the given SBP circular.\n\nTaxonomy: {json.dumps(TAG_TAXONOMY)}\n\nReturn ONLY a JSON object with a 'tags' key containing a list of 1-5 selected tag strings from the taxonomy."
        truncated = content_text[: self.config.max_context_tokens] if len(content_text) > self.config.max_context_tokens else content_text
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

            source_ids = entry.get("source_unit_ids", entry.get("source_ids", []))
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

    def _extract_checklist_block(
        self,
        *,
        circular_label: str,
        block,
        trace_callback=None,
    ) -> list[dict[str, Any]]:
        system = """You are a conservative SBP regulatory compliance analyst. Extract actionable compliance requirements from exactly one complete SOURCE BLOCK.

Include explicit duties, prohibitions, eligibility conditions, controls, deadlines, recordkeeping, submission requirements, required evidence, and explicit permissions or recommendations. Exclude headings, definitions without an obligation, explanatory narrative, addresses, greetings, signature labels, blank form fields, empty table cells, and formatting fragments. For forms and tables, extract only substantive required fields, attestations, evidence, submission actions, or format constraints; do not turn individual words or decorative labels into items. Combine an introductory clause with its dependent list when they form one obligation, but keep genuinely separate actions separate. Use "required" for duties, prohibitions, conditions, and mandatory evidence; use "optional" only for explicit permissions or recommendations. Populate actor, action, object, conditions, deadline, evidence, and applicability when present, using an empty string when a field is absent. Cite one or more SOURCE_ID values exactly as supplied. If the block contains no actionable requirement, return {"items":[]}. Return only the JSON object."""
        user = f"""Circular: {circular_label}
Document: {block.doc_label}
Block reference: {block.ref}
Block type: {block.block_type}
Pages: {block.page_start or 'HTML'}-{block.page_end or block.page_start or 'HTML'}

SOURCE BLOCK:
{block.source_text}"""
        if trace_callback:
            trace_callback("llm_input", {
                "block": block,
                "system_prompt": system,
                "user_prompt": user,
            })
        result = self._complete(
            system,
            user,
            temperature=0.0,
            json_schema=self._checklist_extraction_schema(),
        )
        if trace_callback:
            trace_callback("llm_output", {"block": block, "raw_response": result})
        valid_source_ids = set(block.source_unit_ids)
        try:
            return self._parse_checklist_items(result, valid_source_ids)
        except ValueError:
            retry_system = (
                system
                + "\nYour previous response was malformed. Return the schema-compliant JSON object only, with valid SOURCE_ID citations."
            )
            retry_result = self._complete(
                retry_system,
                user,
                temperature=0.0,
                json_schema=self._checklist_extraction_schema(),
            )
            if trace_callback:
                trace_callback("llm_output", {
                    "block": block,
                    "raw_response": retry_result,
                    "attempt": 2,
                })
            try:
                return self._parse_checklist_items(retry_result, valid_source_ids)
            except ValueError as exc:
                first = self._response_excerpt(result)
                second = self._response_excerpt(retry_result)
                raise ValueError(
                    "The model returned an invalid checklist response after retry. "
                    f"First: {first!r}; retry: {second!r}"
                ) from exc

    @staticmethod
    def _materialize_checklist_item(entry, block, units_by_id) -> dict[str, Any]:
        source_units = [units_by_id[source_id] for source_id in entry["source_unit_ids"]]
        refs = list(dict.fromkeys(unit.ref for unit in source_units))
        pages = [
            page
            for unit in source_units
            for page in (unit.page_start, unit.page_end)
            if page is not None
        ]
        digest = hashlib.sha256(
            f"{block.doc_id}\0{entry['requirement']}\0{'|'.join(entry['source_unit_ids'])}".encode("utf-8")
        ).hexdigest()[:20]
        source_text = "\n\n".join(unit.source_text for unit in source_units)
        requirement_tokens = set(re.findall(r"[a-z0-9]+", entry["requirement"].casefold()))
        candidates = [
            re.sub(r"\s+", " ", candidate).strip(" -*|\n")
            for candidate in re.split(r"(?<=[.;:])\s+|\n+", source_text)
        ]
        candidates = [candidate for candidate in candidates if candidate]
        source_excerpt = max(
            candidates,
            key=lambda candidate: len(
                requirement_tokens
                & set(re.findall(r"[a-z0-9]+", candidate.casefold()))
            ),
            default=source_text,
        )
        if len(source_excerpt) > 900:
            source_excerpt = source_excerpt[:897].rstrip() + "..."
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
            "doc_id": block.doc_id,
            "doc_type": block.doc_type,
            "doc_label": block.doc_label,
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

    def generate_checklist(
        self,
        circular,
        *,
        delay: float = 0.0,
        progress_callback=None,
        trace_callback=None,
        documents: list[dict[str, Any]] | None = None,
        gaps: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        from .checklist import build_analysis_blocks, build_checklist_corpus, segment_document

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
        total_blocks = sum(len(blocks) for _, _, blocks in document_blocks)
        completed = 0
        checklist_items: list[dict[str, Any]] = []
        all_units = [unit for _, units in document_units for unit in units]
        units_by_id = {unit.unit_id: unit for unit in all_units}
        circular_label = circular.reference or circular.title
        if progress_callback:
            progress_callback(0, total_blocks)

        for _, _, blocks in document_blocks:
            for block in blocks:
                try:
                    extracted = self._extract_checklist_block(
                        circular_label=circular_label,
                        block=block,
                        trace_callback=trace_callback,
                    )
                    materialized = [
                        self._materialize_checklist_item(entry, block, units_by_id)
                        for entry in extracted
                    ]
                    checklist_items.extend(materialized)
                except ValueError as exc:
                    materialized = []
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
                if trace_callback:
                    trace_callback("normalized_block", {
                        "block": block,
                        "items": materialized,
                        "completed": completed + 1,
                        "total": total_blocks,
                    })
                completed += 1
                if progress_callback:
                    progress_callback(completed, total_blocks)
                if delay > 0 and completed < total_blocks:
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

    def extract_relationships(self, title: str, reference: str, content_text: str) -> dict:
        system = "You are a financial regulations analyst. Extract any mentions of this circular relating to previous circulars — whether it amends, supersedes, cancels, adds to, or clarifies them. Return ONLY valid JSON with these keys: 'amends' (list of reference strings), 'supersedes' (list), 'cancels' (list), 'adds_to' (list), 'clarifies' (list). Each reference string should be as close to the original format as possible, e.g. 'BPRD Circular No. 12 of 2023'."
        truncated = content_text[: self.config.max_context_tokens] if len(content_text) > self.config.max_context_tokens else content_text
        user = f"Title: {title}\nReference: {reference}\n\nContent:\n{truncated}"
        relationship_properties = {
            key: {"type": "array", "items": {"type": "string"}}
            for key in ("amends", "supersedes", "cancels", "adds_to", "clarifies")
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
        return relationships

    def _execute_tool(
        self,
        name: str,
        arguments: dict,
        db: Session,
        selected_circular_ids: list[str] | None = None,
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
                results = retriever.search(
                    query,
                    limit=limit,
                    token_budget=max(1, self.config.max_context_tokens // 4),
                )
                return json.dumps({"results": results, "count": len(results)})

            if name == "search_circulars":
                from .search import search_engine
                from .models import Circular
                query = arguments.get("query", "")
                department = arguments.get("department", "")
                tag = arguments.get("tag", "")
                limit = int(arguments.get("limit", 10))
                results, _ = search_engine.search(
                    query, db, limit=limit,
                    department=department if department else None,
                    tag=tag if tag else None,
                )
                out = []
                for r in results:
                    c = r["circular"]
                    out.append({
                        "title": c.title,
                        "reference": c.reference,
                        "department": c.department,
                        "date": c.date.strftime("%Y-%m-%d") if c.date else None,
                        "summary": c.summary[:500] if c.summary else None,
                        "status": c.status or "active",
                        "tags": json.loads(c.tags) if c.tags else [],
                        "url": c.url,
                        "citation": f"[[circular:{c.id}|{c.reference or c.title}]]",
                    })
                return json.dumps({"results": out, "count": len(out)})

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
                        "citation": f"[[circular:{c.id}|{c.reference or c.title}]]",
                    })
                return json.dumps({"results": out, "count": len(out)})

            elif name == "get_circular_details":
                from .models import Circular
                from sqlalchemy import or_
                ref = arguments.get("circular_reference", "").strip()
                if not ref:
                    return json.dumps({"error": "No circular reference provided"})
                # Try exact reference match first, then title ILIKE
                c = db.query(Circular).filter(Circular.reference == ref).first()
                if not c:
                    c = db.query(Circular).filter(
                        or_(
                            Circular.title.ilike(f"%{ref}%"),
                            Circular.reference.ilike(f"%{ref}%"),
                        )
                    ).first()
                if not c:
                    return json.dumps({"error": f"Circular not found: {ref}"})
                return json.dumps({
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
                    "citation": f"[[circular:{c.id}|{c.reference or c.title}]]",
                    "attachment_citations": [
                        f"[[attachment:{item.id}|{item.filename}]]"
                        for item in c.attachments
                    ],
                })

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
                        "citation": f"[[circular:{c.id}|{c.reference or c.title}]]",
                    })
                return json.dumps({"results": out, "count": len(out)})

            else:
                return json.dumps({"error": f"Unknown tool: {name}"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    def _chat_system_prompt(self, circulars_context: str | None = None) -> str:
        if circulars_context:
            return f"""You are an expert assistant for analyzing State Bank of Pakistan (SBP) circulars and regulations.
You have been provided with pre-selected circulars as context below. Answer primarily from these,
but you also have tools to search the database if the user asks about circulars not covered here.

IMPORTANT RULES:
1. Cite a circular only with the exact [[circular:ID|label]] token supplied in context or tool results.
2. Cite an attachment only with the exact [[attachment:ID|label]] token supplied in context or tool results.
Never expose IDs outside those tokens, alter a token, invent a token, or turn plain-text references into links.
3. Be precise and highlight regulatory differences when comparing circulars.
4. Use search_selected_documents when the included passages do not contain enough detail. It can
search the complete selected circulars and their attachments. Do not claim attachment content is
unavailable merely because it was not included in the initial context.
5. Use global circular search tools only when the user explicitly requests broader research.

Pre-selected circulars:
{circulars_context}"""
        return """You are an expert assistant for SBP circulars and regulations.
Use your tools to search and retrieve relevant circulars from the database before answering.

IMPORTANT RULES:
1. Cite a circular only with an exact [[circular:ID|label]] token returned by a tool.
2. Cite an attachment only with an exact [[attachment:ID|label]] token returned by a tool.
Never expose IDs outside those tokens, alter a token, invent a token, or turn plain-text references into links.
3. If you need more details on a circular found in a search, use the get_circular_details tool with the circular reference or title."""

    def chat(
        self,
        messages: list[dict[str, str]],
        db: Session,
        circulars_context: str | None = None,
        selected_circular_ids: list[str] | None = None,
    ) -> str:
        system_prompt = self._chat_system_prompt(
            circulars_context if selected_circular_ids else None
        )
        full_messages = [{"role": "system", "content": system_prompt}] + messages

        max_iterations = 5
        for _ in range(max_iterations):
            response = self._client.chat.completions.create(
                model=self.config.effective_chat_model,
                messages=full_messages,
                temperature=0.3,
                tools=TOOLS,
                tool_choice="auto",
            )

            msg = response.choices[0].message

            if not msg.tool_calls:
                return msg.content or ""

            # Append the assistant's tool_calls request
            full_messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                    }
                    for tc in msg.tool_calls
                ]
            })

            # Execute each tool and append results
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                result = self._execute_tool(
                    tc.function.name, args, db, selected_circular_ids
                )
                full_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

        # Fallback if max iterations reached
        final_response = self._client.chat.completions.create(
            model=self.config.effective_chat_model,
            messages=full_messages,
            temperature=0.3,
        )
        return final_response.choices[0].message.content or ""

    def stream_chat(
        self,
        messages: list[dict[str, str]],
        db: Session,
        circulars_context: str | None = None,
        selected_circular_ids: list[str] | None = None,
    ):
        system_prompt = self._chat_system_prompt(
            circulars_context if selected_circular_ids else None
        )
        full_messages = [{"role": "system", "content": system_prompt}] + messages

        max_iterations = 5
        for _ in range(max_iterations):
            stream = self._client.chat.completions.create(
                model=self.config.effective_chat_model,
                messages=full_messages,
                temperature=0.3,
                tools=TOOLS,
                tool_choice="auto",
                stream=True,
            )

            content_parts: list[str] = []
            tool_calls: dict[int, dict] = {}

            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    content_parts.append(delta.content)
                    yield delta.content

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

            if not tool_calls:
                return

            full_messages.append({
                "role": "assistant",
                "content": "".join(content_parts),
                "tool_calls": [tool_calls[index] for index in sorted(tool_calls)],
            })

            for tc in [tool_calls[index] for index in sorted(tool_calls)]:
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}
                result = self._execute_tool(
                    tc["function"]["name"], args, db, selected_circular_ids
                )
                full_messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

        final_stream = self._client.chat.completions.create(
            model=self.config.effective_chat_model,
            messages=full_messages,
            temperature=0.3,
            stream=True,
        )
        for chunk in final_stream:
            if not chunk.choices:
                continue
            content = chunk.choices[0].delta.content
            if content:
                yield content

    def test_connection(self) -> dict:
        try:
            response = self._client.chat.completions.create(
                model=self.config.model,
                messages=[{"role": "user", "content": "Say 'Connection successful.'"}],
                max_tokens=10,
                temperature=0.0,
            )
            content = response.choices[0].message.content or ""
            return {"success": True, "response": content}
        except Exception as e:
            return {"success": False, "error": str(e)}


def get_ai_client(db=None) -> AIClient:
    config = None
    if db is not None:
        config = AIConfig.from_db(db)
    if config is None:
        config = AIConfig.from_env()
    return AIClient(config)
