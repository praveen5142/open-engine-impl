import enum

class AgentName(str, enum.Enum):
    CLAUDE = "claude"
    ANTIGRAVITY = "antigravity"
    ENGINE = "engine"   # The orchestrator doing a direct memory query — not an LLM/CLI

class Role(str, enum.Enum):
    RESEARCH = "RESEARCH"   # Memory store search — no LLM call
    SPEC = "SPEC"           # Claude-authored acceptance criteria grounded in research
    PLANNING = "PLANNING"
    EXECUTION = "EXECUTION"
    REVIEW = "REVIEW"
