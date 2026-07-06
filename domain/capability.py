import enum
from dataclasses import dataclass
from typing import Optional

class QuotaStatus(str, enum.Enum):
    AVAILABLE = "available"
    EXHAUSTED = "exhausted"
    UNKNOWN = "unknown"

@dataclass
class QuotaSignal:
    status: QuotaStatus
    confidence: float
    evidence: Optional[str]
    retry_after: Optional[int] = None

@dataclass
class AgentCapability:
    installed: bool
    quota_status: QuotaStatus
    quota_confidence: float = 1.0
    cooldown_until: Optional[float] = None
