from abc import ABC, abstractmethod
from domain.capability import QuotaSignal

class QuotaProbePort(ABC):
    @abstractmethod
    def classify(self, exit_code: int, stdout: str, stderr: str) -> QuotaSignal:
        pass
