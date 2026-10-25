"""Conservative, token-aware extraction of per-participant reward limits."""
import math
import re

NUMBER = r"((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
TOKEN = r"([A-Z][A-Z0-9]{1,14})\b"
AMOUNT = NUMBER + r"\s*" + TOKEN
USER = r"(?:each|per|a|any|every)\s+(?:eligible\s+)?(?:user|participant|account)"
PATTERNS = [
    rf"{USER}[^\n;.!?]{{0,100}}?(?:up to|maximum of|capped at|cap of|not exceed|no more than|limited to)\s*{AMOUNT}",
    rf"(?:maximum|max\.?|cap(?:ped)?)\s+(?:reward\s+)?(?:for\s+|of\s+)?{USER}[^\n;.!?]{{0,50}}?{AMOUNT}",
    rf"(?:reward|rewards)[^\n;.!?]{{0,30}}?{USER}[^\n;.!?]{{0,50}}?(?:capped at|up to|not exceed|maximum of|limited to)\s*{AMOUNT}",
    rf"(?:maximum|max\.?)\s+(?:reward\s+)?(?:of\s+)?{AMOUNT}\s*(?:in rewards\s*)?(?:for\s+)?{USER}",
    rf"(?:reward\s+)?cap\s*(?:of\s+|:\s*)?{AMOUNT}\s*(?:for\s+)?{USER}",
]

def extract_reward_cap(text: str, pool_token: str) -> tuple[float | None, str | None]:
    """Accept one unambiguous cap in the pool token; never infer a currency conversion."""
    candidates = set()
    for pattern in PATTERNS:
        for match in re.finditer(pattern, text or "", re.I):
            context = match.group(0)
            if not re.search(r"\b(?:rewards?|receiv\w*|earn\w*)\b", context, re.I):
                continue
            amount, token = match.groups()
            value = float(amount.replace(",", ""))
            if token.upper() == pool_token.upper() and math.isfinite(value) and value > 0:
                candidates.add((value, token.upper()))
    if len(candidates) == 1:
        return candidates.pop()
    return None, None


def effective_reward_cap(ev: dict, fallback: float, fallback_token: str):
    token = (ev.get("pool_token") or "BNB").upper()
    value = ev.get("reward_cap")
    if value is not None and (ev.get("reward_cap_token") or "").upper() == token:
        value = float(value)
        if math.isfinite(value) and value > 0:
            return value, "announcement"
    if token == fallback_token.upper():
        return fallback, "fallback"
    return None, "unavailable"
