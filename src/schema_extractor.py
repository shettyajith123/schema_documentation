from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional, TypedDict
from urllib.parse import quote_plus

import pyodbc
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError


class ColumnInfo(TypedDict):
    name: str
    data_type: str
    max_length: Optional[str]
    is_nullable: bool
    default_value: Optional[str]


class ForeignKeyInfo(TypedDict):
    column: str
    referenced_table: str
    referenced_column: str


class IndexInfo(TypedDict, total=False):
    name: str
    columns: list[str]
    is_unique: bool
    type_desc: str


class TableSchema(TypedDict):
    table: str  # schema.table
    schema: str
    name: str
    columns: list[ColumnInfo]
    primary_keys: list[str]
    foreign_keys: list[ForeignKeyInfo]
    indexes: list[IndexInfo]
    sample_data: list[dict[str, Any]]
    row_count: int


class DatabaseSchema(TypedDict):
    server: str
    database: str
    extracted_at: str
    tables: list[TableSchema]


@dataclass(frozen=True)
class _ParsedTable:
    schema: str
    table: str


class SqlServerExtractor:
    """Extract SQL Server table metadata using pyodbc + SQLAlchemy."""

    def __init__(
        self,
        server: str,
        database: str,
        username: Optional[str],
        password: Optional[str],
    ) -> None:
        self.server = server
        self.database = database
        self.username = (username or "").strip() or None
        self.password = (password or "").strip() or None

        self._logger = logging.getLogger(self.__class__.__name__)
        self._driver = (os.getenv("SQL_DRIVER") or "").strip() or None
        self._engine: Engine = self._create_engine()

        # Validate early so CLI fails fast on connection problems.
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(self._format_connection_error(exc)) from exc

    def _format_connection_error(self, exc: BaseException) -> str:
        msg = str(exc)
        hints: list[str] = []

        if "Data source name not found" in msg or "IM002" in msg:
            hints.append(
                "ODBC driver not found. Install 'ODBC Driver 17 for SQL Server' (or 18) and set sql_server.driver in config.yaml."
            )
        if "Login failed" in msg or "28000" in msg:
            if self.username and self.password:
                hints.append("SQL authentication failed. Check SQL_USER/SQL_PASSWORD.")
            else:
                hints.append(
                    "Windows authentication failed. Ensure your Windows user has access to the database."
                )
        if "Server does not exist" in msg or "Could not open a connection" in msg:
            hints.append(
                "Cannot reach SQL Server. Check --server, SQL Server service, TCP/IP settings, and firewall."
            )

        hint_text = ("\n" + "\n".join(f"- {h}" for h in hints)) if hints else ""
        return f"Failed to connect to SQL Server (server={self.server}, database={self.database}). {msg}{hint_text}"

    def _detect_driver(self) -> str:
        configured = self._driver
        if configured:
            return configured

        available = [d.strip() for d in pyodbc.drivers()]
        # Prefer newest first.
        for candidate in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
            if candidate in available:
                return candidate

        if available:
            return available[-1]

        # If no drivers are found, surface a clear error.
        raise RuntimeError(
            "No ODBC drivers detected. Install 'ODBC Driver 17 for SQL Server' (or 18)."
        )

    def _create_engine(self) -> Engine:
        driver = self._detect_driver()

        # Use a raw ODBC connection string to support protocol prefixes like:
        # - lpc:.\SQLEXPRESS (Shared Memory)
        # - np:.\SQLEXPRESS  (Named Pipes)
        # and to pass named instances through without SQLAlchemy URL parsing quirks.
        # Driver 18 enables encryption by default; local dev often needs relaxed settings.
        parts: list[str] = [
            f"DRIVER={{{driver}}}",
            f"SERVER={self.server}",
            f"DATABASE={self.database}",
            "TrustServerCertificate=yes",
            "Encrypt=no",
        ]
        if self.username and self.password:
            parts.extend([f"UID={self.username}", f"PWD={self.password}"])
        else:
            parts.append("Trusted_Connection=yes")

        odbc_connect = ";".join(parts) + ";"
        connect_str = quote_plus(odbc_connect)

        return create_engine(
            f"mssql+pyodbc:///?odbc_connect={connect_str}",
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            future=True,
        )

    @staticmethod
    def _parse_table_name(table_name: str) -> _ParsedTable:
        raw = table_name.strip().strip("[]")
        if not raw:
            raise ValueError("table_name is empty")

        # Allow input like schema.table or [schema].[table].
        raw = raw.replace("[", "").replace("]", "")
        if "." in raw:
            schema, table = raw.split(".", 1)
        else:
            schema, table = "dbo", raw

        schema = schema.strip()
        table = table.strip()
        if not schema or not table:
            raise ValueError(f"Invalid table_name: {table_name!r}")

        return _ParsedTable(schema=schema, table=table)

    @staticmethod
    def _quote_ident(name: str) -> str:
        # Basic SQL Server identifier quoting.
        safe = name.replace("]", "]]" )
        return f"[{safe}]"

    def get_tables(self) -> list[str]:
        """Return all user tables as fully qualified names: schema.table."""
        sql = text(
            """
            SELECT s.name AS schema_name, t.name AS table_name
            FROM sys.tables t
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            WHERE t.is_ms_shipped = 0
            ORDER BY s.name, t.name
            """
        )

        try:
            with self._engine.connect() as conn:
                rows = conn.execute(sql).mappings().all()
        except SQLAlchemyError as exc:
            raise RuntimeError("Failed to query tables.") from exc

        return [f"{r['schema_name']}.{r['table_name']}" for r in rows]

    def get_columns(self, table_name: str) -> list[ColumnInfo]:
        parsed = self._parse_table_name(table_name)

        sql = text(
            """
            SELECT
                c.name AS column_name,
                ty.name AS data_type,
                c.max_length AS max_length,
                c.is_nullable AS is_nullable,
                dc.definition AS default_value
            FROM sys.columns c
            INNER JOIN sys.types ty ON c.user_type_id = ty.user_type_id
            INNER JOIN sys.tables t ON c.object_id = t.object_id
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            LEFT JOIN sys.default_constraints dc ON c.default_object_id = dc.object_id
            WHERE s.name = :schema_name AND t.name = :table_name
            ORDER BY c.column_id
            """
        )

        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    sql,
                    {"schema_name": parsed.schema, "table_name": parsed.table},
                ).mappings().all()
        except SQLAlchemyError as exc:
            raise RuntimeError(f"Failed to query columns for {table_name}.") from exc

        results: list[ColumnInfo] = []
        for r in rows:
            data_type = str(r["data_type"])
            max_len_raw: Optional[int] = r["max_length"]

            max_length: Optional[str]
            if max_len_raw is None:
                max_length = None
            elif max_len_raw == -1:
                max_length = "MAX"
            else:
                adjusted = int(max_len_raw)
                if data_type.lower() in {"nvarchar", "nchar", "ntext"}:
                    adjusted = adjusted // 2
                max_length = str(adjusted)

            default_value = r.get("default_value")
            if default_value is not None:
                default_value = str(default_value)

            results.append(
                {
                    "name": str(r["column_name"]),
                    "data_type": data_type,
                    "max_length": max_length,
                    "is_nullable": bool(r["is_nullable"]),
                    "default_value": default_value,
                }
            )

        return results

    def get_primary_keys(self, table_name: str) -> list[str]:
        parsed = self._parse_table_name(table_name)
        sql = text(
            """
            SELECT col.name AS column_name
            FROM sys.indexes i
            INNER JOIN sys.index_columns ic
                ON i.object_id = ic.object_id AND i.index_id = ic.index_id
            INNER JOIN sys.columns col
                ON ic.object_id = col.object_id AND ic.column_id = col.column_id
            INNER JOIN sys.tables t ON i.object_id = t.object_id
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            WHERE i.is_primary_key = 1
              AND s.name = :schema_name AND t.name = :table_name
            ORDER BY ic.key_ordinal
            """
        )

        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    sql,
                    {"schema_name": parsed.schema, "table_name": parsed.table},
                ).all()
        except SQLAlchemyError as exc:
            raise RuntimeError(f"Failed to query primary keys for {table_name}.") from exc

        return [str(r[0]) for r in rows]

    def get_foreign_keys(self, table_name: str) -> list[ForeignKeyInfo]:
        parsed = self._parse_table_name(table_name)
        sql = text(
            """
            SELECT
                pc.name AS column_name,
                rs.name AS referenced_schema,
                rt.name AS referenced_table,
                rc.name AS referenced_column
            FROM sys.foreign_key_columns fkc
            INNER JOIN sys.tables pt ON fkc.parent_object_id = pt.object_id
            INNER JOIN sys.schemas ps ON pt.schema_id = ps.schema_id
            INNER JOIN sys.columns pc
                ON fkc.parent_object_id = pc.object_id AND fkc.parent_column_id = pc.column_id
            INNER JOIN sys.tables rt ON fkc.referenced_object_id = rt.object_id
            INNER JOIN sys.schemas rs ON rt.schema_id = rs.schema_id
            INNER JOIN sys.columns rc
                ON fkc.referenced_object_id = rc.object_id AND fkc.referenced_column_id = rc.column_id
            WHERE ps.name = :schema_name AND pt.name = :table_name
            ORDER BY pc.name
            """
        )

        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    sql,
                    {"schema_name": parsed.schema, "table_name": parsed.table},
                ).mappings().all()
        except SQLAlchemyError as exc:
            raise RuntimeError(f"Failed to query foreign keys for {table_name}.") from exc

        return [
            {
                "column": str(r["column_name"]),
                "referenced_table": f"{r['referenced_schema']}.{r['referenced_table']}",
                "referenced_column": str(r["referenced_column"]),
            }
            for r in rows
        ]

    def get_indexes(self, table_name: str) -> list[IndexInfo]:
        parsed = self._parse_table_name(table_name)

        sql = text(
            """
            SELECT
                i.name AS index_name,
                i.is_unique AS is_unique,
                i.type_desc AS type_desc,
                ic.key_ordinal AS key_ordinal,
                c.name AS column_name
            FROM sys.indexes i
            INNER JOIN sys.index_columns ic
                ON i.object_id = ic.object_id AND i.index_id = ic.index_id
            INNER JOIN sys.columns c
                ON ic.object_id = c.object_id AND ic.column_id = c.column_id
            INNER JOIN sys.tables t ON i.object_id = t.object_id
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            WHERE s.name = :schema_name AND t.name = :table_name
              AND i.name IS NOT NULL
              AND i.is_hypothetical = 0
              AND ic.is_included_column = 0
            ORDER BY i.name, ic.key_ordinal
            """
        )

        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    sql,
                    {"schema_name": parsed.schema, "table_name": parsed.table},
                ).mappings().all()
        except SQLAlchemyError as exc:
            raise RuntimeError(f"Failed to query indexes for {table_name}.") from exc

        grouped: dict[str, IndexInfo] = {}
        for r in rows:
            name = str(r["index_name"])
            if name not in grouped:
                grouped[name] = {
                    "name": name,
                    "columns": [],
                    "is_unique": bool(r["is_unique"]),
                    "type_desc": str(r["type_desc"]),
                }
            grouped[name]["columns"].append(str(r["column_name"]))

        return list(grouped.values())

    def get_sample_data(self, table_name: str, limit: int = 3) -> list[dict[str, Any]]:
        parsed = self._parse_table_name(table_name)

        safe_limit = int(limit)
        if safe_limit <= 0:
            return []
        safe_limit = min(safe_limit, 100)

        query = (
            f"SELECT TOP ({safe_limit}) * FROM {self._quote_ident(parsed.schema)}.{self._quote_ident(parsed.table)}"
        )

        try:
            with self._engine.connect() as conn:
                result = conn.execute(text(query))
                rows = result.mappings().all()
        except SQLAlchemyError as exc:
            self._logger.warning(
                "Failed to fetch sample data for %s: %s", table_name, exc
            )
            return []

        return [dict(r) for r in rows]

    def get_row_count(self, table_name: str) -> int:
        parsed = self._parse_table_name(table_name)

        sql = text(
            """
            SELECT COALESCE(SUM(ps.row_count), 0) AS row_count
            FROM sys.dm_db_partition_stats ps
            INNER JOIN sys.tables t ON ps.object_id = t.object_id
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            WHERE s.name = :schema_name AND t.name = :table_name
              AND ps.index_id IN (0, 1)
            """
        )

        try:
            with self._engine.connect() as conn:
                row = conn.execute(
                    sql,
                    {"schema_name": parsed.schema, "table_name": parsed.table},
                ).mappings().first()
        except SQLAlchemyError as exc:
            raise RuntimeError(f"Failed to query row count for {table_name}.") from exc

        if not row:
            return 0
        return int(row["row_count"] or 0)

    def get_table_schema(self, table_name: str, sample_limit: int = 3) -> TableSchema:
        parsed = self._parse_table_name(table_name)
        fqtn = f"{parsed.schema}.{parsed.table}"

        columns = self.get_columns(fqtn)
        primary_keys = self.get_primary_keys(fqtn)
        foreign_keys = self.get_foreign_keys(fqtn)
        indexes = self.get_indexes(fqtn)
        row_count = self.get_row_count(fqtn)
        sample_data = self.get_sample_data(fqtn, limit=sample_limit)

        return {
            "table": fqtn,
            "schema": parsed.schema,
            "name": parsed.table,
            "columns": columns,
            "primary_keys": primary_keys,
            "foreign_keys": foreign_keys,
            "indexes": indexes,
            "sample_data": sample_data,
            "row_count": row_count,
        }

    def get_full_schema(self) -> DatabaseSchema:
        tables = self.get_tables()
        schemas: list[TableSchema] = []

        for table in tables:
            schemas.append(self.get_table_schema(table))

        return {
            "server": self.server,
            "database": self.database,
            "extracted_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "tables": schemas,
        }
