from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Optional

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from tqdm import tqdm


@dataclass
class GenerationStats:
    tables_attempted: int = 0
    tables_succeeded: int = 0
    tables_failed: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    reported_input_tokens: int = 0
    reported_output_tokens: int = 0


class DocumentationGenerator:
    """Generate Markdown documentation for a table using an LLM."""

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        temperature: float = 0.1,
        max_retries: int = 3,
    ) -> None:
        self._logger = logging.getLogger(self.__class__.__name__)
        self.model = model
        self.temperature = temperature
        self.max_retries = max_retries

        self._llm = ChatOpenAI(model=model, temperature=temperature)

    @staticmethod
    def _approx_tokens(text: str) -> int:
        # Common heuristic: ~4 chars per token (English-ish). Good enough for estimates.
        return max(1, int(len(text) / 4))

    def _build_messages(self, table_schema_dict: dict[str, Any]) -> list[Any]:
        table_name = table_schema_dict.get("table", "(unknown)")

        system = SystemMessage(
            content=(
                "You are a senior data analyst and data architect. "
                "Generate Markdown documentation for a SQL Server table. "
                "Infer business meaning from table/column names, primary keys, foreign keys, indexes, and sample data. "
                "Do NOT invent facts not supported by the schema; if uncertain, say 'Unknown' or 'Likely'. "
                "Output Markdown only."
            )
        )

        schema_json = json.dumps(table_schema_dict, indent=2, default=str)

        human = HumanMessage(
            content=(
                f"Document the table: {table_name}\n\n"
                "Required sections (use these headings):\n"
                "1. ## Overview\n"
                "   - Describe the table and inferred business purpose.\n"
                "2. ## Columns\n"
                "   - Provide a Markdown table with columns: Name, Type, Nullable, Default, Description.\n"
                "   - Mark primary key columns clearly (e.g., add ' (PK)' to the Name).\n"
                "3. ## Keys & Relationships\n"
                "   - List primary keys and foreign keys.\n"
                "   - Include a Mermaid diagram showing relationships (prefer `erDiagram` if possible).\n"
                "4. ## Indexes\n"
                "   - List indexes with their columns and brief usage notes (what queries they likely support).\n"
                "5. ## Sample Data\n"
                "   - Show a Markdown table preview of sample rows (truncate long values).\n"
                "6. ## Data Volume\n"
                "   - Use row_count to describe approximate volume and any implications.\n"
                "7. ## Data Quality Red Flags\n"
                "   - Call out concerns like: columns always null in sample, suspicious defaults, inconsistent key patterns, etc.\n\n"
                "Table schema JSON:\n"
                f"```json\n{schema_json}\n```\n"
            )
        )

        return [system, human]

    def _extract_usage(self, response: Any) -> tuple[int, int]:
        # Best-effort extraction across langchain-openai versions.
        usage = None
        if hasattr(response, "response_metadata"):
            usage = response.response_metadata.get("token_usage") or response.response_metadata.get(
                "usage"
            )
        if not usage and hasattr(response, "usage_metadata"):
            usage = response.usage_metadata

        if isinstance(usage, dict):
            input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            output_tokens = int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            )
            return input_tokens, output_tokens

        return 0, 0

    def generate_table_doc(self, table_schema_dict: dict[str, Any]) -> tuple[str, int, int]:
        messages = self._build_messages(table_schema_dict)

        # Estimate tokens from our prompt.
        prompt_text = "\n\n".join(getattr(m, "content", str(m)) for m in messages)
        estimated_input = self._approx_tokens(prompt_text)

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._llm.invoke(messages)
                content = getattr(response, "content", "")
                if not isinstance(content, str) or not content.strip():
                    raise RuntimeError("LLM returned empty content")

                in_tok, out_tok = self._extract_usage(response)
                if out_tok == 0:
                    # Fall back to heuristic for output tokens.
                    out_tok = self._approx_tokens(content)

                return content, max(estimated_input, in_tok), out_tok
            except Exception as exc:  # noqa: BLE001
                last_exc = exc if isinstance(exc, Exception) else RuntimeError(str(exc))
                wait_s = min(8.0, (2 ** (attempt - 1)) + random.random())
                self._logger.warning(
                    "LLM call failed (attempt %s/%s): %s", attempt, self.max_retries, exc
                )
                time.sleep(wait_s)

        raise RuntimeError(f"LLM generation failed after {self.max_retries} retries") from last_exc

    @staticmethod
    def estimate_cost_usd(
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """Estimate cost using env-provided per-1M token prices.

        Env vars (optional):
        - OPENAI_PRICE_{MODEL}_INPUT_PER_1M_USD
        - OPENAI_PRICE_{MODEL}_OUTPUT_PER_1M_USD

        If missing, falls back to conservative defaults and labels should be treated as approximate.
        """

        key_model = re_sub_non_alnum(model).upper()
        in_rate = os.getenv(f"OPENAI_PRICE_{key_model}_INPUT_PER_1M_USD")
        out_rate = os.getenv(f"OPENAI_PRICE_{key_model}_OUTPUT_PER_1M_USD")

        # Defaults are intentionally conservative and may be outdated.
        input_per_1m = float(in_rate) if in_rate else 0.15
        output_per_1m = float(out_rate) if out_rate else 0.60

        return (input_tokens / 1_000_000) * input_per_1m + (output_tokens / 1_000_000) * output_per_1m

    def generate_table_docs_batch(
        self,
        tables: list[dict[str, Any]],
        progress: bool = True,
    ) -> tuple[dict[str, str], dict[str, str], GenerationStats]:
        docs: dict[str, str] = {}
        failures: dict[str, str] = {}
        stats = GenerationStats()

        iterator = tables
        if progress:
            iterator = list(tables)
            iterator = tqdm(iterator, desc="Generating docs", unit="table")

        for table_schema in iterator:
            table_name = str(table_schema.get("table", "(unknown)"))
            stats.tables_attempted += 1
            try:
                md, in_tok, out_tok = self.generate_table_doc(table_schema)
                docs[table_name] = md
                stats.tables_succeeded += 1
                stats.estimated_input_tokens += in_tok
                stats.estimated_output_tokens += out_tok
            except Exception as exc:  # noqa: BLE001
                failures[table_name] = str(exc)
                stats.tables_failed += 1

        return docs, failures, stats


def re_sub_non_alnum(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value)
