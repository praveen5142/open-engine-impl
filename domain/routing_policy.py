import json
import os
from .agent import AgentName, Role
from .capability import AgentCapability, QuotaStatus

class RoutingDecision:
    def __init__(self, agent: AgentName | None, reason: str, degraded: bool = False, hold: bool = False):
        self.agent = agent
        self.reason = reason
        self.degraded = degraded
        self.hold = hold

class RoutingPolicyService:
    def __init__(self, config_path: str):
        with open(config_path, "r") as f:
            self.matrix = json.load(f)
            
    def _is_available(self, capability: AgentCapability | None) -> bool:
        if not capability or not capability.installed:
            return False
        # If exhausted and still in cooldown, not available.
        # CapabilityStore should handle setting cooldown_until appropriately based on probes.
        import time
        if capability.quota_status == QuotaStatus.EXHAUSTED:
            if capability.cooldown_until and time.time() < capability.cooldown_until:
                return False
        return True

    def route(self, role: Role, get_capability) -> RoutingDecision:
        """
        get_capability is a callable: (AgentName) -> AgentCapability
        Returns a RoutingDecision
        """
        policy = self.matrix.get(role.value)
        if not policy:
            return RoutingDecision(None, "hold", hold=True)
            
        primary = AgentName(policy["primary"])
        primary_cap = get_capability(primary)
        
        if self._is_available(primary_cap):
            return RoutingDecision(primary, "primary_available")
            
        fallback = policy.get("fallback")
        if fallback:
            fallback_agent = AgentName(fallback)
            fallback_cap = get_capability(fallback_agent)
            if self._is_available(fallback_cap):
                if primary_cap and primary_cap.quota_status == QuotaStatus.EXHAUSTED:
                    return RoutingDecision(fallback_agent, "fallback_quota")
                else:
                    return RoutingDecision(fallback_agent, "fallback_unavailable")
                    
        # Both primary and fallback (if any) are unavailable
        if policy.get("degraded_allowed"):
            return RoutingDecision(None, "skipped_degraded", degraded=True)
            
        return RoutingDecision(None, "hold", hold=True)
