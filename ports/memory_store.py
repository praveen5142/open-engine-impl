from abc import ABC, abstractmethod


class MemoryStorePort(ABC):
    @abstractmethod
    def ingest(self, path: str, kind: str = "rule") -> dict:
        """Read a file, upsert it as a knowledge_documents row.

        Returns {'status': 'created'|'updated'|'unchanged', 'id': int}.
        Never raises on a readable file — returns {'status': 'error', 'error': str}
        if the file cannot be read.
        """

    @abstractmethod
    def search(self, query: str, k: int = 5) -> list[dict]:
        """Return up to k {id, title, content, kind, score} snippets relevant to query.

        Never raises for an empty/no-match query — returns [].
        Never raises on FTS5 syntax errors in the query — returns [] and logs to stderr.
        """

    @abstractmethod
    def write_wisdom(self, task_id: int, text: str) -> None:
        """Record a completed task's outcome as a kind='wisdom' document
        so future searches can retrieve it.

        Idempotent: if a wisdom document already exists for task_id, this
        is a no-op (checked by caller via dedup guard, but also safe here).
        """
