"""Secret redaction applied to every piece of output before it is stored
or returned to the model. Best-effort: it catches common key and password
shapes, not every possible secret. The sandbox also hides secret files."""
import re

_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bsk-(?:or-v1-|ant-|proj-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/=]{12,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
]
_KV = re.compile(
    r"(?i)(\b[A-Za-z0-9_]*(?:password|passwd|secret|token|api[_-]?key|apikey|"
    r"access[_-]?key|private[_-]?key|credential|psk|passphrase|wep-key[0-9])[A-Za-z0-9_.-]*\b\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|\S+)")

MARK = "[REDACTED]"


def redact(text: str) -> str:
    if not text:
        return text
    for p in _PATTERNS:
        text = p.sub(MARK, text)
    return _KV.sub(lambda m: m.group(1) + MARK, text)
