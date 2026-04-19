"""Extract a bug reproducer (SQL / shell / script) from an issue body using an LLM.

Falls back to a lightweight regex scan if the LLM is unavailable or returns nothing.
"""

import logging
import os
import re

logger = logging.getLogger(__name__)

# ── Regex fallback (used when LLM is unavailable) ────────────────────────────

_REPRO_SECTION_RE = re.compile(
    r"(?:^|\n)#{1,4}\s*(?:steps?\s+to\s+reproduc|reproduc|how\s+to\s+reproduc)[^\n]*\n"
    r"(.*?)(?=\n#{1,4}\s|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_CODE_BLOCK_RE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
_EXEC_LANGS = {"sql", "mysql", "cql", "cqlsh", "bash", "sh", "shell", "python", "py", "go", ""}


def _regex_extract(body: str) -> str | None:
    for section in _REPRO_SECTION_RE.finditer(body):
        for m in _CODE_BLOCK_RE.finditer(section.group(1)):
            if m.group(1).lower() in _EXEC_LANGS:
                return m.group(2).strip()
    for m in _CODE_BLOCK_RE.finditer(body):
        if m.group(1).lower() in _EXEC_LANGS:
            return m.group(2).strip()
    return None


# ── LLM extraction ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a helpful assistant that reads bug reports and extracts the minimal \
reproducer needed to trigger the described bug.

A reproducer is a SQL query, shell command, or script that, when executed \
against the affected system, causes the bug to manifest.

Rules:
- Return ONLY the raw reproducer text — no explanation, no markdown fences, \
  no labels.
- If the issue contains multiple reproducer candidates, pick the simplest \
  self-contained one.
- If the issue does not contain any reproducer (no steps, no query, no command), \
  reply with exactly the word: NONE
"""

_USER_TEMPLATE = """\
Extract the reproducer from this bug report. \
Return only the raw command/query/script, or NONE if there is no reproducer.

---
{body}
---
"""


def _llm_extract(body: str) -> str | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        # Truncate to keep costs low — reproducers are always near the top
        truncated = body[:6000]
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _USER_TEMPLATE.format(body=truncated)}],
        )
        text = message.content[0].text.strip()
        if not text or text.upper() == "NONE":
            return None
        return text
    except Exception as e:
        logger.warning(f"[ReproducerExtractor] LLM extraction failed: {e}")
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def extract_reproducer(body: str) -> str | None:
    """Return a reproducer string from an issue body, or None if not found.

    Uses the LLM when ANTHROPIC_API_KEY is set; falls back to regex otherwise.
    """
    if not body:
        return None

    result = _llm_extract(body)
    if result:
        logger.debug(f"[ReproducerExtractor] LLM found reproducer ({len(result)} chars)")
        return result

    result = _regex_extract(body)
    if result:
        logger.debug(f"[ReproducerExtractor] Regex found reproducer ({len(result)} chars)")
    return result
