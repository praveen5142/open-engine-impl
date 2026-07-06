from abc import ABC, abstractmethod
from typing import Any

class AgentInvocationPort(ABC):
    @abstractmethod
    def invoke(self, task_id: int, role: str, db_path: str, payload: dict) -> Any:
        pass
