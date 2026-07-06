from abc import ABC, abstractmethod
from typing import Optional
from domain.agent import AgentName
from domain.capability import AgentCapability

class CapabilityStorePort(ABC):
    @abstractmethod
    def get_capability(self, agent: AgentName) -> Optional[AgentCapability]:
        pass
        
    @abstractmethod
    def save_capability(self, agent: AgentName, capability: AgentCapability) -> None:
        pass
