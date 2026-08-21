"""Stripping secrets out of anything captured automatically.

Notes an agent writes deliberately are the agent's responsibility. Transcripts
imported without anyone reading them are this module's, and the stakes are
different: a session where somebody exported an API key, or where a command
printed a `.env`, would otherwise put that key into a Markdown file, into a
search index, and eventually into another agent's context.

The rules are deliberately blunt. Redaction that is occasionally too eager
loses a word from an old transcript; redaction that is occasionally too
permissive publishes a credential. Those are not comparable costs, so this
errs heavily towards the first, and everything here is unconditional - there
is no setting that turns it off.
"""

from __future__ import annotations

import re

PLACEHOLDER = "[redacted]"

#: Sections a user explicitly marked as not to be recorded.
PRIVATE_BLOCK = re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)

#: Key material, which is unmistakable and must never survive.
_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

#: Provider-issued tokens with recognisable prefixes.
_TOKEN_SHAPES = [
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\bey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
]

#: Names that mean the value beside them is a secret, whatever it looks like.
_SECRET_NAME = (
    r"(?:pass(?:wo?rd)?|passwd|secret|token|api[_-]?key|apikey|access[_-]?key"
    r"|client[_-]?secret|private[_-]?key|credential|auth)"
)

#: `KEY=value`, as in a .env file or an export line.
_ASSIGNMENT = re.compile(
    rf"(?i)\b([A-Z0-9_]*{_SECRET_NAME}[A-Z0-9_]*)(\s*[:=]\s*)"
    r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;#\n]+)"
)

#: `"password": "..."`, as in JSON or a config dump.
_JSON_FIELD = re.compile(
    rf"(?i)(\"[^\"]*{_SECRET_NAME}[^\"]*\"\s*:\s*)(\"[^\"]*\")"
)

#: A long unbroken run of base64-ish characters. Almost never prose, almost
#: always an encoded blob, a key, or an image - none of which belong here.
_BLOB = re.compile(r"\b[A-Za-z0-9+/]{120,}={0,2}\b")

#: Values short enough to be a placeholder rather than a real secret. Leaving
#: `PASSWORD=changeme` readable keeps redacted transcripts legible.
_MIN_SECRET_LENGTH = 8


def _redact_assignment(match: re.Match) -> str:
    name, separator, value = match.group(1), match.group(2), match.group(3)
    stripped = value.strip("\"'")
    if len(stripped) < _MIN_SECRET_LENGTH:
        return match.group(0)
    quote = value[0] if value[:1] in {'"', "'"} else ""
    return f"{name}{separator}{quote}{PLACEHOLDER}{quote}"


def _redact_json_field(match: re.Match) -> str:
    value = match.group(2)
    if len(value.strip('"')) < _MIN_SECRET_LENGTH:
        return match.group(0)
    return f'{match.group(1)}"{PLACEHOLDER}"'


def redact(text: str) -> str:
    """Remove anything that looks like a credential.

    Order matters: whole key blocks go first, so their base64 body is never
    considered on its own, and named assignments go before shape-matching, so
    that a secret whose value has no recognisable shape is still caught by its
    name.
    """
    if not text:
        return text
    text = PRIVATE_BLOCK.sub(PLACEHOLDER, text)
    text = _PEM.sub(PLACEHOLDER, text)
    text = _ASSIGNMENT.sub(_redact_assignment, text)
    text = _JSON_FIELD.sub(_redact_json_field, text)
    for pattern in _TOKEN_SHAPES:
        text = pattern.sub(PLACEHOLDER, text)
    text = _BLOB.sub(PLACEHOLDER, text)
    return text


def contains_secret(text: str) -> bool:
    """Whether redaction would change anything. For tests and health checks."""
    return redact(text) != text
