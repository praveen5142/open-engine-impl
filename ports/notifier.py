from abc import ABC, abstractmethod

class NotifierPort(ABC):
    @abstractmethod
    def notify_hold(self, task_id: int, role: str, reason: str) -> None:
        pass
