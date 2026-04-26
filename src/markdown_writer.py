from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class MarkdownWriter:
    base_dir: Path
    database_name: str

    def __post_init__(self) -> None:
        self._logger = logging.getLogger(self.__class__.__name__)
        self.base_dir = Path(self.base_dir)

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        # Windows-forbidden: < > : " / \ | ? *
        return re.sub(r'[<>:"/\\|?*]', "_", name)

    def _db_dir(self) -> Path:
        return self.base_dir / self.database_name

    def write_table_doc(self, table_name: str, content: str) -> Path:
        self._db_dir().mkdir(parents=True, exist_ok=True)
        filename = self._sanitize_filename(f"{table_name}.md")
        path = self._db_dir() / filename
        path.write_text(content, encoding="utf-8")
        return path

    def write_database_index(self, tables_list: list[str]) -> Path:
        self._db_dir().mkdir(parents=True, exist_ok=True)
        index_path = self._db_dir() / "README.md"

        lines: list[str] = [f"# {self.database_name}", "", "## Tables", ""]
        for table in sorted(tables_list):
            filename = self._sanitize_filename(f"{table}.md")
            lines.append(f"- [{table}]({filename})")

        index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return index_path

    def write_master_index(self, databases_list: list[str]) -> Path:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        index_path = self.base_dir / "README.md"

        lines: list[str] = ["# Schema Documentation", "", "## Databases", ""]
        for db in sorted(databases_list):
            lines.append(f"- [{db}]({db}/README.md)")

        index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return index_path
