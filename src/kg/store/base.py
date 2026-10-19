"""GraphStore interface. SQLiteStore (sqlite_nx.py) implements it; a Neo4jStore can implement it later."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable


class GraphStore(ABC):
    @abstractmethod
    def upsert_node(self, node_id: str, type: str, props: dict | None = None,
                    valid_from: str | None = None, valid_to: str | None = None) -> None: ...

    @abstractmethod
    def upsert_edge(self, src: str, dst: str, type: str, props: dict | None = None,
                    valid_from: str | None = None, valid_to: str | None = None,
                    edge_id: str | None = None) -> str: ...

    @abstractmethod
    def get_node(self, node_id: str) -> dict | None: ...

    @abstractmethod
    def neighbors(self, node_id: str, edge_type: str | None = None, direction: str = "out",
                  as_of: str | None = None) -> list[dict]:
        """Returns [{'edge': {...}, 'node': {...}}]. direction: out | in | both."""

    @abstractmethod
    def nodes(self, type: str | None = None, as_of: str | None = None, **filters: Any) -> list[dict]: ...

    @abstractmethod
    def as_of(self, date: str):
        """A filtered NetworkX MultiDiGraph with the nodes/edges valid on ``date`` (ISO)."""

    @abstractmethod
    def subgraph(self, ids: Iterable[str]):
        """NetworkX MultiDiGraph induced on ``ids`` (all dates)."""
