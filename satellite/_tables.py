#  Copyright (c) University College London Hospitals NHS Foundation Trust
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
# limitations under the License.
import git
import networkx as nx

from typing import List, Generator, Optional, Any, Dict
from pathlib import Path

from satellite._utils import camel_to_snake_case
from satellite._settings import EnvVar
from satellite._log import logger
from satellite._column import Column
from satellite._fake import fake


class _TableChunk:
    def __init__(self, name: str):
        self.name = str(name)
        self.n_rows = 0
        self._data: Dict[Column, list] = dict()

    def __getitem__(self, key: Column) -> Any:
        return self._data[key]

    def __setitem__(self, key: Column, value: Any):
        assert isinstance(value, list) or isinstance(value, tuple)
        self._data[key] = list(value)

    @property
    def columns(self) -> List[Column]:
        """All columns"""
        return [column for column in self._data.keys()]

    @property
    def non_pk_columns(self) -> List[Column]:
        """Columns that are not primary keys"""
        return [column for column in self._data.keys() if not column.is_primary_key]

    @property
    def data_columns(self) -> List[Column]:
        return [
            column
            for column in self.columns
            if not column.is_primary_key and not column.is_foreign_key
        ]

    @property
    def pk_column(self) -> Column:
        """Primary key column"""
        return next(column for column in self.columns if column.is_primary_key)

    @property
    def has_override_faker_method(self) -> bool:
        """Does faker have a method suitable to generate a whole row of this table?"""
        return hasattr(fake, self.name)

    def _override_columns(self) -> None:
        """Add data to this table with a table-specific method by generating rows"""

        faker_method = getattr(fake, self.name)
        rows = [faker_method() for _ in range(self.n_rows)]

        for column in self.columns:
            if column.name in rows[0]:
                self[column] = [row[column.name] for row in rows]

    def add_fake_data(self, skip_foreign_keys: bool = False) -> None:
        logger.debug(f"Adding fake data to {self.name}")

        for column in self.data_columns if skip_foreign_keys else self.non_pk_columns:
            logger.debug(f"Creating {self.n_rows} row(s) of data to {column.name}")

            function = column.faker_method

            if self.n_rows == 1:
                self[column] = function()
            else:
                values = self[column]
                for _ in range(self.n_rows):
                    values.append(function())

        if self.has_override_faker_method and self.n_rows > 0:
            self._override_columns()

        return None


class Row(_TableChunk):
    def __init__(self, table_name: str, columns: List[Column]):
        super().__init__(name=table_name)
        self.n_rows = 1
        self._data = {column: [] for column in columns}

    @property
    def id(self) -> Optional[int]:
        """Primary key of this row"""
        return self[self.pk_column]

    @id.setter
    def id(self, value: int):
        self[self.pk_column] = value

    @property
    def table_name(self) -> str:
        return self.name

    @classmethod
    def with_fake_values(cls, table_name: str, columns: List[Column]) -> Any:
        row = cls(table_name=table_name, columns=columns)
        row.add_fake_data()
        return row

    def __getitem__(self, key: Column) -> Optional[Any]:
        value = self._data[key]
        return value[0] if len(value) == 1 else None

    def __setitem__(self, key: Column, value: Any):
        if not (isinstance(value, list) or isinstance(value, tuple)):
            value = [value]
        self._data[key] = list(value)


class NewRow(Row):
    """Row with no primary key, used for inserts"""


class ExistingRow(Row):
    def __init__(
        self, table_name: str, columns: List[Column], primary_key_id: Optional[int] = 0
    ):
        super().__init__(table_name=table_name, columns=columns)
        self.id = primary_key_id


class Table(_TableChunk):
    """Single table in a Star schema"""

    def __init__(self, name: str):
        super().__init__(name=name)
        self._extended_tables: List[str] = []
        self.n_rows = int(EnvVar("N_TABLE_ROWS").or_default())

    @classmethod
    def from_java_file(cls, filepath: Path) -> "Table":
        logger.info(f"Creating table from {filepath.name}")

        self = Table(name=camel_to_snake_case(filepath.stem))

        passed_class_definition = False

        # If a line includes any of these substrings it will be skipped
        # note all Lists are e.g. one<->many relationships
        excluded_substrings = ("@", "*", "(", "List")

        # Strings that define if a class attribute is being defined
        delc_strings = ("private", "public", "protected")
        depth = 0

        for line in open(filepath, "r"):

            if "{" in line:
                depth += 1

            if "}" in line:
                depth -= 1

            if f"class {filepath.stem}" in line:
                if "extends TemporalCore" in line:
                    self._extended_tables.append("temporal_core")
                elif "extends AuditCore" in line:
                    self._extended_tables += ["temporal_core", "audit_core"]

                passed_class_definition = True
                continue

            if (
                not passed_class_definition
                or depth != 1
                or any(s in line for s in excluded_substrings)
                or not any(s in line for s in delc_strings)
            ):
                continue

            # e.g. line = "private Instant storedFrom;" or "private Boolean x = false;"
            items = line.strip().rstrip(";").split()
            idx = -2 if "=" not in line else -4
            java_type, attr_name = items[idx], items[idx + 1]

            # All attributes that end with Id are foreign keys, thus just ints
            if attr_name.endswith("Id"):
                java_type = "Long"

            column = Column(
                name=camel_to_snake_case(attr_name),
                java_type=java_type,
                parent_table_name=self.name,
            )

            self._data[column] = []

        logger.info(f"Created {self}")
        return self

    def fake_row(self) -> NewRow:
        return NewRow.with_fake_values(table_name=self.name, columns=self.columns)

    def random_existing_row(self) -> ExistingRow:
        return ExistingRow(
            table_name=self.name,
            columns=self.columns,
            primary_key_id=None if self.n_rows == 0 else fake.pyint(1, self.n_rows),
        )

    def randomised_existing_row(self) -> ExistingRow:
        row = self.random_existing_row()
        row.add_fake_data(skip_foreign_keys=True)
        return row

    def add_columns_from(self, table: "Table") -> None:
        """Add a set of columns to this table from another table"""
        for column in table.columns:
            self._data[column] = []

    def assign_foreign_keys(self, tables: "Tables") -> None:
        """
        Given the columns present in this table determine those that are
        foreign keys
        """
        for column in self.columns:
            try:
                column.table_reference = next(
                    table for table in tables if f"{table.name}_id" == column.name
                )
            except StopIteration:
                continue  # Not a foreign key referencing another tables PK

    @property
    def primary_key_name(self) -> str:
        return f"{self.name}_id"

    @property
    def extended_table_names(self) -> list[str]:
        """Tables which are inherited by this one i.e. their columns added"""
        return self._extended_tables

    def __repr__(self):
        return f"Table({self.name}, columns = {self.columns}, n_rows = {self.n_rows})"


class Tables(list):
    """List of tables present in a star schema"""

    @classmethod
    def from_repo(cls, repo_url: str, branch_name: str) -> "Tables":
        """Create a list of tables by traversing files from a cloned git repo"""
        excluded_suffixes = ["Core.java", "info.java", "TemporalFrom.java"]
        repo_path = Path("star_repo")

        if not repo_path.exists():
            logger.info(f"Cloning {repo_path}")
            _ = git.Repo.clone_from(url=repo_url, to_path=repo_path, branch=branch_name)

        self = cls()
        superclasses = {}

        for path in Path("star_repo/emap-star/emap-star/src/main").rglob("**/*.java"):

            if path.name.endswith("Core.java"):
                table = Table.from_java_file(path)
                superclasses[table.name] = table
                continue

            if any(path.name.endswith(suffix) for suffix in excluded_suffixes):
                continue

            self.append(Table.from_java_file(path))

        for table in self:
            table.assign_foreign_keys(self)
            for extend_table_name in table.extended_table_names:
                table.add_columns_from(superclasses[extend_table_name])

        logger.info(f"Created {len(self)} tables from repo")
        return self

    def topologically_sorted(self) -> Generator:
        """Tables in topologically sorted order given the foreign key references"""
        logger.info("Sorting directed acyclic graph into topological order")

        dag = nx.DiGraph()
        dag.add_nodes_from(range(len(self)))

        for i, table in enumerate(self):
            for column in [col for col in table.columns if col.is_foreign_key]:
                logger.info(
                    f"{column.name:30s} is foreign key -> {column.table_reference.name}"
                )
                dag.add_edge(i, self.index(column.table_reference))

        for node in reversed(list(nx.topological_sort(dag))):
            yield self[int(node)]
