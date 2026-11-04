"""PostgreSQL-backed local knowledge services for the editorial agent."""

from .models import KnowledgeDocument

__all__ = [
    "KnowledgeDocument",
    "KnowledgeStore",
    "knowledge_context",
    "prepare_local_knowledge_snapshot",
]


def __getattr__(name):
    if name == "KnowledgeStore":
        from .store import KnowledgeStore
        return KnowledgeStore
    if name in {"knowledge_context", "prepare_local_knowledge_snapshot"}:
        from . import service
        return getattr(service, name)
    raise AttributeError(name)
