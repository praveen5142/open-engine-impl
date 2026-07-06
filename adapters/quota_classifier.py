import re
from domain.capability import QuotaSignal, QuotaStatus

QUOTA_PATTERNS = [
    r"usage limit reached",
    r"usage_limit_reached",
    r"You've hit your usage limit",
    r"rate_limit_exceeded",
    r"429 Too Many Requests",
    r"exceeded your current quota",
    r"Claude AI usage limit reached",
]

QUOTA_DEFAULT_COOLDOWN_SECONDS = 1800

def _extract_retry_after(text: str) -> float | None:
    import time
    match = re.search(r"try again in (?:(\d+)h)?\s*(?:(\d+)m)?", text, re.I)
    if match:
        h = int(match.group(1) or 0)
        m = int(match.group(2) or 0)
        if h > 0 or m > 0:
            return time.time() + (h * 3600) + (m * 60)
    return time.time() + QUOTA_DEFAULT_COOLDOWN_SECONDS  # Default 30 min cooldown

class RegexQuotaClassifier:
    def classify(self, exit_code: int, stdout: str, stderr: str) -> QuotaSignal:
        text = f"{stdout}\n{stderr}"
        for pat in QUOTA_PATTERNS:
            if re.search(pat, text, re.I):
                return QuotaSignal(
                    status=QuotaStatus.EXHAUSTED,
                    confidence=0.8,
                    evidence=pat,
                    retry_after=_extract_retry_after(text)
                )
        if exit_code != 0:
            return QuotaSignal(
                status=QuotaStatus.UNKNOWN,
                confidence=0.3,
                evidence=text[:200]
            )
        return QuotaSignal(
            status=QuotaStatus.AVAILABLE,
            confidence=1.0,
            evidence=None
        )
