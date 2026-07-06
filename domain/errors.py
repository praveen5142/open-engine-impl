class AgentUnavailableError(Exception):
    """
    Raised when an agent's CLI cannot be reached at all (not installed, not on
    PATH, or the process could not be started) as opposed to reachable-but-
    quota-exhausted. Carries an optional `context` dict so adapters can attach
    recovery information (e.g. a manual hand-off inbox path) that the
    orchestrator/HTTP layer can surface to a human instead of just failing.
    """
    def __init__(self, message, context: dict | None = None):
        super().__init__(message)
        self.context = context or {}

class AgentQuotaExhaustedError(Exception):
    pass

class AgentAuthError(Exception):
    pass
