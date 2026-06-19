import json
import os
from dataclasses import dataclass, field
from typing import Any

from openai import BadRequestError, OpenAI

from sqlalchemy.orm import Session


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

    def generate_checklist(self, title: str, content_text: str) -> list[dict]:
        system = "You are a compliance analyst. Generate a compliance checklist from the given SBP circular. Each item should be a specific, actionable requirement from the circular. Return a JSON object with a 'checklist' key containing an array of objects, each with 'item' (the requirement) and 'action_required' (boolean, true if banks must take concrete action)."
        truncated = content_text[: self.config.max_context_tokens] if len(content_text) > self.config.max_context_tokens else content_text
        user = f"Title: {title}\n\nContent:\n{truncated}"
        result = self._complete(
            system,
            user,
            temperature=0.1,
            json_schema={
                "type": "object",
                "properties": {
                    "checklist": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "item": {"type": "string"},
                                "action_required": {"type": "boolean"},
                            },
                            "required": ["item", "action_required"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["checklist"],
                "additionalProperties": False,
            },
        )
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as exc:
            raise ValueError("The model returned invalid JSON for the checklist.") from exc
        checklist = parsed.get("checklist") if isinstance(parsed, dict) else None
        if not isinstance(checklist, list):
            raise ValueError("The model returned an invalid checklist payload.")
        normalized = []
        for entry in checklist:
            if not isinstance(entry, dict) or not isinstance(entry.get("item"), str):
                raise ValueError("The model returned an invalid checklist item.")
            action_required = entry.get("action_required", False)
            if not isinstance(action_required, bool):
                raise ValueError("The model returned a non-boolean checklist action flag.")
            normalized.append({
                "item": entry["item"].strip(),
                "action_required": action_required,
            })
        return normalized

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

    def _execute_tool(self, name: str, arguments: dict, db: Session) -> str:
        """Execute a tool by name and return the result as a JSON string.
        All tool responses intentionally omit internal circular IDs to prevent the LLM
        from exposing them to the user."""
        try:
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
                    "compliance_checklist": json.loads(c.compliance_checklist) if c.compliance_checklist else [],
                    "status": c.status or "active",
                    "content_preview": (c.content_text or "")[:2000],
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
1. NEVER cite internal circular IDs in your responses. Always use the circular reference (e.g., "BPRD Circular No. 12 of 2023") or title.
2. When referring to a specific circular, use its reference number and title — never UUIDs or database IDs. In addition, when citing a circular, always format it as a Markdown link using its viewURL: /view_circular?cir={{url}})
Never fabricate URLs — only use urls from tool results.
3. Be precise and highlight regulatory differences when comparing circulars.

Pre-selected circulars:
{circulars_context}"""
        return """You are an expert assistant for SBP circulars and regulations.
Use your tools to search and retrieve relevant circulars from the database before answering.

IMPORTANT RULES:
1. NEVER cite internal circular IDs in your responses. Always use the circular reference (e.g., "BPRD Circular No. 12 of 2023") or title.
2. When referring to a specific circular, use its reference number and title — never UUIDs or database IDs. In addition, when citing a circular, always format it as a Markdown link using its viewURL: /view_circular?cir={{url}})
Never fabricate URLs — only use urls from tool results.
3. If you need more details on a circular found in a search, use the get_circular_details tool with the circular reference or title."""

    def chat(self, messages: list[dict[str, str]], db: Session, circulars_context: str | None = None) -> str:
        system_prompt = self._chat_system_prompt(circulars_context)
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
                result = self._execute_tool(tc.function.name, args, db)
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

    def stream_chat(self, messages: list[dict[str, str]], db: Session, circulars_context: str | None = None):
        system_prompt = self._chat_system_prompt(circulars_context)
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
                result = self._execute_tool(tc["function"]["name"], args, db)
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
