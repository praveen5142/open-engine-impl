import sqlite3
from contextlib import closing
from typing import Optional
from domain.agent import AgentName
from domain.capability import AgentCapability, QuotaStatus
from ports.capability_store import CapabilityStorePort

class SQLiteCapabilityStore(CapabilityStorePort):
    def __init__(self, db_path: str):
        self.db_path = db_path

    def get_capability(self, agent: AgentName) -> Optional[AgentCapability]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT available, quota_status, quota_confidence, cooldown_until FROM capability_probe WHERE tool_name = ? ORDER BY id DESC LIMIT 1",
                (agent.value,)
            ).fetchone()
        
        if not row:
            return None
            
        return AgentCapability(
            installed=bool(row[0]),
            quota_status=QuotaStatus(row[1]) if row[1] else QuotaStatus.UNKNOWN,
            quota_confidence=row[2] if row[2] is not None else 1.0,
            cooldown_until=row[3]
        )
        
    def save_capability(self, agent: AgentName, capability: AgentCapability) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """UPDATE capability_probe 
                   SET available = ?, quota_status = ?, quota_confidence = ?, cooldown_until = ?
                   WHERE tool_name = ? AND id = (SELECT MAX(id) FROM capability_probe WHERE tool_name = ?)""",
                (1 if capability.installed else 0, capability.quota_status.value, capability.quota_confidence, capability.cooldown_until, agent.value, agent.value)
            )
            if conn.total_changes == 0:
                conn.execute(
                    """INSERT INTO capability_probe (tool_name, available, quota_status, quota_confidence, cooldown_until)
                       VALUES (?, ?, ?, ?, ?)""",
                    (agent.value, 1 if capability.installed else 0, capability.quota_status.value, capability.quota_confidence, capability.cooldown_until)
                )
            conn.commit()
