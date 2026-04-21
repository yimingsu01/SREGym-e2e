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


# ── Prose detector ───────────────────────────────────────────────────────────

_SQL_KEYWORDS = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|USE|SET|WITH|EXPLAIN|SHOW|CALL|LOAD)\b",
    re.IGNORECASE,
)
_PROSE_STEP_RE = re.compile(r"^\s*\d+\.", re.MULTILINE)


def _is_prose(text: str) -> bool:
    """Return True if text looks like numbered prose steps rather than executable code."""
    step_matches = len(_PROSE_STEP_RE.findall(text))
    sql_matches = len(_SQL_KEYWORDS.findall(text))
    # If there are more numbered steps than SQL keywords, it's prose.
    return step_matches >= 2 and step_matches > sql_matches


# ── LLM extraction ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a helpful assistant that reads database bug reports and produces a minimal reproducer.

Bug reports commonly contain MULTIPLE code blocks that serve different purposes — the buggy
reproducer, the expected/correct output, a workaround, a "this-also-works" contrast case, or
even the fix itself. You must extract ONLY the block that triggers the bug against a live,
unfixed binary — never a workaround, contrast case, expected-output display, or fix patch.

Extract or synthesize these fields:

1. reproducer — a minimal script/command that TRIGGERS the bug on a live database.
   Decision process:
     a. If ONE code block is present, extract it verbatim (assuming it's executable).
     b. If MULTIPLE code blocks are present, pick the one whose surrounding prose identifies it
        as producing the WRONG / INCORRECT / BUGGY / UNEXPECTED output. SKIP any block whose
        surrounding text says it:
          - "returns correctly" / "works correctly" / "gives the correct result"
          - "this is not affected" / "the workaround is" / "changed ... produces correct results"
          - labels it as "expected" output (vs "actual" output)
          - shows the fix / patch / suggested change
     c. If only prose steps are given, SYNTHESIZE a minimal script following those steps.
        Use mongosh JavaScript for MongoDB (db.collection.insertMany, db.collection.aggregate,
        db.aggregate([{$documents: [...]}, ...]) for collection-less pipelines).
        Use SQL for TiDB/MySQL (CREATE TABLE, INSERT, SELECT).
     d. Return NONE for: startup/bootstrap crashes; bugs needing external tools (BR, Lightning,
        PITR, TiFlash, physical backup/restore, network faults); bugs needing a sharded cluster;
        or when no executable block exists.

2. buggy_output — the LITERAL output the REPRODUCER produces on an UNFIXED binary, per the
   description. Copy verbatim from the "actual" / "we get" / "returns" / "got instead" block.
   Preserve mongosh formatting (single quotes, ISODate(...), ObjectId(...), cursor arrays).
   null if the description doesn't spell out the wrong output.

3. correct_output — the LITERAL output the reproducer SHOULD produce on a FIXED binary.
   Copy verbatim from "expected" / "should return" / "correct answer" / "expected answer".
   null if not spelled out.

4. expected_output — the value the readiness probe will grep for.
   For MongoDB wrong-result bugs: copy buggy_output verbatim (probe matches on buggy binary).
   For SQL wrong-result bugs: use `mysql --batch --skip-column-names` format — tab-separated
   columns, newline-separated rows, no headers, no prose.
   null for crash/panic bugs or when not determinable.

5. crash_on_startup — true only if the bug causes the process to fail on startup. false otherwise.

Return a JSON object with EXACTLY these five keys:
  {
    "reproducer": "<raw SQL or shell script, or NONE>",
    "buggy_output": "<verbatim actual-output from description, or null>",
    "correct_output": "<verbatim expected-output from description, or null>",
    "expected_output": "<probe-grep value, or null>",
    "crash_on_startup": false
  }

Rules:
- reproducer: raw executable text only. No markdown fences. No numbered-step prose.
- NEVER return a code block whose surrounding text identifies it as a workaround, contrast
  case, expected-output display, or the fix — only the triggering code.
- buggy_output / correct_output: copied verbatim — do not rephrase, summarize, or reformat.
- Return valid JSON and nothing else.
"""

_USER_TEMPLATE = """\
Extract the reproducer, expected output, and crash_on_startup flag \
from this bug report. Return only a JSON object.

For expected_output: write the LITERAL rows the query should return \
(as mysql --batch --skip-column-names prints them: no headers, one row per line, \
tab-separated values). Do NOT write a description like "3 rows of 0" — write "0\\n0\\n0".

---
{body}
---
"""


def _llm_extract(body: str) -> tuple[str | None, str | None, str | None, str | None, bool]:
    """Return (reproducer, expected_output, buggy_output, correct_output, crash_on_startup) via LLM."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None, None, None, None, False

    try:
        import json as _json
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1536,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _USER_TEMPLATE.format(body=body[:6000])}],
        )
        raw = message.content[0].text.strip()
        logger.warning(f"[ReproducerExtractor] LLM raw response: {repr(raw[:500])}")
        # Strip markdown fences if the model wraps JSON in them
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = _json.loads(raw)
        reproducer = data.get("reproducer") or None
        if reproducer and reproducer.strip().upper() == "NONE":
            reproducer = None
        if reproducer and _is_prose(reproducer):
            logger.warning("[ReproducerExtractor] LLM returned prose steps — discarding")
            reproducer = None
        expected = data.get("expected_output") or None
        buggy_output = data.get("buggy_output") or None
        correct_output = data.get("correct_output") or None
        crash_on_startup = bool(data.get("crash_on_startup", False))
        if not reproducer and not crash_on_startup:
            logger.warning(f"[ReproducerExtractor] LLM returned no reproducer. Raw: {raw[:300]}")
        return reproducer, expected, buggy_output, correct_output, crash_on_startup
    except Exception as e:
        logger.warning(f"[ReproducerExtractor] LLM extraction failed: {e}")
        return None, None, None, None, False


# ── LLM repair ────────────────────────────────────────────────────────────────

_REPAIR_SYSTEM_PROMPT = """\
You previously extracted a reproducer from a database bug report, but running it against
a live unfixed binary did NOT reproduce the bug. Produce a corrected reproducer.

Common failure modes to consider:
- The issue text uses legacy-shell syntax that modern mongosh routes differently (e.g.
  `db.coll.insert(...)` is deprecated and goes through a bulk-write envelope that can
  inflate payloads past the 16 MB BSON limit — use `insertOne` / `insertMany` instead).
- You picked a contrast / workaround / expected-output block instead of the buggy one.
- The script needs additional setup (collection creation, seed docs) to reach the buggy
  code path.
- Statements run out of order, or in a shell that doesn't support the chosen syntax.

Return JSON with a single key:
  { "reproducer": "<corrected executable text, or NONE if no fix is possible>" }

Rules:
- Preserve the intent of the original: same bug, same trigger, same surface.
- Raw executable text only. No markdown fences. No numbered-step prose.
- If the original was already correct and the failure is environmental, return NONE.
"""

_REPAIR_USER_TEMPLATE = """\
Original issue body:
---
{body}
---

Original reproducer (did NOT trigger the bug):
---
{reproducer}
---

Expected buggy output (what the description says an unfixed binary prints):
---
{buggy_output}
---

Expected correct output (what a fixed binary would print):
---
{correct_output}
---

Actual output observed when running the original reproducer:
---
{actual_output}
---

Return JSON: {{"reproducer": "<corrected text, or NONE>"}}
"""


def repair_reproducer(
    body: str,
    reproducer: str,
    actual_output: str,
    buggy_output: str | None,
    correct_output: str | None,
) -> str | None:
    """Ask the LLM to rewrite a reproducer that failed validation. Returns None if no repair is possible or the LLM is unavailable."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("[ReproducerExtractor] repair skipped: no ANTHROPIC_API_KEY")
        return None

    try:
        import json as _json
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=_REPAIR_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _REPAIR_USER_TEMPLATE.format(
                body=body[:6000],
                reproducer=reproducer,
                buggy_output=buggy_output or "(not extracted)",
                correct_output=correct_output or "(not extracted)",
                actual_output=actual_output[:2000],
            )}],
        )
        raw = message.content[0].text.strip()
        logger.warning(f"[ReproducerExtractor] repair raw response: {repr(raw[:500])}")
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = _json.loads(raw)
        repaired = data.get("reproducer") or None
        if repaired and repaired.strip().upper() == "NONE":
            return None
        if repaired and _is_prose(repaired):
            logger.warning("[ReproducerExtractor] repair returned prose — discarding")
            return None
        return repaired
    except Exception as e:
        logger.warning(f"[ReproducerExtractor] repair failed: {e}")
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def extract_reproducer(body: str) -> tuple[str | None, str | None, bool]:
    """Return (reproducer, expected_output, crash_on_startup).

    Back-compat shim for callers that only need the three-field tuple.
    Prefer ``extract_reproducer_full`` when validation outputs are needed.
    """
    r, e, _b, _c, c = extract_reproducer_full(body)
    return r, e, c


def extract_reproducer_full(
    body: str,
) -> tuple[str | None, str | None, str | None, str | None, bool]:
    """Return (reproducer, expected_output, buggy_output, correct_output, crash_on_startup).

    ``buggy_output`` and ``correct_output`` are the verbatim output snippets the
    description claims the reproducer produces on an unfixed vs. fixed binary.
    They are used by ``reproducer_validator`` to verify the extracted reproducer
    actually triggers the bug — they do NOT flow into the generated problem file.
    """
    if not body:
        return None, None, None, None, False

    reproducer, expected_output, buggy_output, correct_output, crash_on_startup = _llm_extract(body)
    if reproducer or crash_on_startup:
        logger.debug(
            f"[ReproducerExtractor] LLM: reproducer={len(reproducer) if reproducer else 0}chars "
            f"expected_output={expected_output!r} buggy_output={buggy_output!r} "
            f"correct_output={correct_output!r} crash_on_startup={crash_on_startup}"
        )
        return reproducer, expected_output, buggy_output, correct_output, crash_on_startup

    reproducer = _regex_extract(body)
    if reproducer:
        logger.debug(f"[ReproducerExtractor] Regex found reproducer ({len(reproducer)} chars)")
    return reproducer, None, None, None, False
