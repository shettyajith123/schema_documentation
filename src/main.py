from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv
from tqdm import tqdm

from doc_generator import DocumentationGenerator
from markdown_writer import MarkdownWriter
from schema_extractor import SqlServerExtractor


def setup_logging(base_dir: Path) -> logging.Logger:
    base_dir = Path(base_dir)
    log_dir = base_dir / "_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "schema-documenter.log"

    logger = logging.getLogger("schema_documenter")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Avoid duplicate handlers if main() is called multiple times.
    if not logger.handlers:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)

        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)

        logger.addHandler(ch)
        logger.addHandler(fh)

    # Also configure root for libraries.
    logging.getLogger().setLevel(logging.INFO)
    return logger


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}


def parse_tables_arg(value: Optional[str]) -> Optional[list[str]]:
    if not value:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    normalized: list[str] = []
    for t in items:
        if "." in t:
            normalized.append(t)
        else:
            normalized.append(f"dbo.{t}")
    return normalized


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate Markdown docs for SQL Server tables")
    parser.add_argument("--database", required=True, help="Target database name")
    parser.add_argument("--server", default="localhost", help="SQL Server host or HOST\\INSTANCE")
    parser.add_argument("--output-dir", default=None, help="Output directory (default: docs)")
    parser.add_argument("--dry-run", action="store_true", help="Skip LLM; just extract and preview schema")
    parser.add_argument(
        "--tables",
        default=None,
        help="Optional comma-separated list of tables (schema.table). Default: all tables",
    )

    args = parser.parse_args(argv)

    load_dotenv(override=False)
    config = load_config(Path("config.yaml"))

    cfg_sql = (config.get("sql_server") or {}) if isinstance(config, dict) else {}
    cfg_llm = (config.get("llm") or {}) if isinstance(config, dict) else {}
    cfg_output = (config.get("output") or {}) if isinstance(config, dict) else {}

    output_dir = Path(
        args.output_dir
        or os.getenv("OUTPUT_DIR")
        or str(cfg_output.get("base_dir") or "docs")
    )

    logger = setup_logging(output_dir)

    # Driver: set env var used by extractor (keeps extractor __init__ signature per requirement).
    driver = os.getenv("SQL_DRIVER") or str(cfg_sql.get("driver") or "").strip()
    if driver:
        os.environ["SQL_DRIVER"] = driver

    server = args.server
    database = args.database

    username = os.getenv("SQL_USER")
    password = os.getenv("SQL_PASSWORD")

    tables_filter = parse_tables_arg(args.tables)

    logger.info("Connecting to SQL Server server=%s database=%s", server, database)
    try:
        extractor = SqlServerExtractor(
            server=server,
            database=database,
            username=username,
            password=password,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("%s", exc)
        return 2

    try:
        all_tables = extractor.get_tables()
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to list tables: %s", exc)
        return 3

    target_tables = all_tables
    if tables_filter is not None:
        # Respect user list; warn if not found.
        missing = [t for t in tables_filter if t not in all_tables]
        for t in missing:
            logger.warning("Requested table not found (or no permissions): %s", t)
        target_tables = [t for t in tables_filter if t in all_tables]

    if not target_tables:
        logger.error("No tables to document.")
        return 4

    logger.info("Tables found: %s (targeting %s)", len(all_tables), len(target_tables))

    # Dry run: extract and print summary without calling LLM.
    if args.dry_run:
        preview: list[dict[str, Any]] = []
        for table in tqdm(target_tables, desc="Extracting schema", unit="table"):
            try:
                schema = extractor.get_table_schema(table)
                preview.append(
                    {
                        "table": schema["table"],
                        "columns": schema["columns"],
                        "primary_keys": schema["primary_keys"],
                        "foreign_keys": schema["foreign_keys"],
                        "indexes": schema["indexes"],
                        "row_count": schema["row_count"],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to extract %s: %s", table, exc)
                continue

        print(json.dumps(preview, indent=2, default=str))
        logger.info("Dry run complete. Extracted schemas: %s", len(preview))
        return 0 if preview else 5

    model = str(cfg_llm.get("model") or "gpt-4o-mini")
    temperature = float(cfg_llm.get("temperature") or 0.1)

    if not os.getenv("OPENAI_API_KEY"):
        logger.error("OPENAI_API_KEY is not set. Create a .env file based on .env.example")
        return 6

    generator = DocumentationGenerator(model=model, temperature=temperature)
    writer = MarkdownWriter(base_dir=output_dir, database_name=database)

    docs_generated = 0
    failures: dict[str, str] = {}
    total_in_tokens = 0
    total_out_tokens = 0

    for table in tqdm(target_tables, desc="Generating docs", unit="table"):
        try:
            table_schema = extractor.get_table_schema(table)
        except Exception as exc:  # noqa: BLE001
            msg = f"Schema extraction failed: {exc}"
            logger.error("%s | %s", table, msg)
            failures[table] = msg
            continue

        try:
            md, in_tok, out_tok = generator.generate_table_doc(table_schema)
            writer.write_table_doc(table, md)
            docs_generated += 1
            total_in_tokens += in_tok
            total_out_tokens += out_tok
        except Exception as exc:  # noqa: BLE001
            msg = f"Doc generation failed: {exc}"
            logger.error("%s | %s", table, msg)
            failures[table] = msg
            continue

    writer.write_database_index(target_tables)

    # Update master index based on subfolders present.
    existing_dbs = [p.name for p in output_dir.iterdir() if p.is_dir() and not p.name.startswith("_")]
    writer.write_master_index(existing_dbs)

    estimated_cost = DocumentationGenerator.estimate_cost_usd(
        model=model,
        input_tokens=total_in_tokens,
        output_tokens=total_out_tokens,
    )

    logger.info(
        "Summary: tables=%s docs_generated=%s failures=%s", len(target_tables), docs_generated, len(failures)
    )
    logger.info(
        "Estimated usage: input_tokens=%s output_tokens=%s estimated_cost_usd=%.6f (approx)",
        total_in_tokens,
        total_out_tokens,
        estimated_cost,
    )

    if failures:
        logger.info("Failures (showing up to 10):")
        for t, err in list(failures.items())[:10]:
            logger.info("- %s: %s", t, err)

    return 0 if docs_generated > 0 else 7


if __name__ == "__main__":
    raise SystemExit(main())
