import enum

class AgentName(str, enum.Enum):
    CLAUDE = "claude"
    ANTIGRAVITY = "antigravity"

class Role(str, enum.Enum):
    PLANNING = "PLANNING"
    EXECUTION = "EXECUTION"
    REVIEW = "REVIEW"
