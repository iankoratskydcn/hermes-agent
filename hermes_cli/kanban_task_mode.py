"""Rule 2 — task-mode (verification-rigor) classification.

Consulted by ``kanban_decompose.py`` at task-creation time to populate
``Task.task_mode``. Per lane_rules_2_4_6.json's ``rule2-enforcement``
decision this is a small standalone module (not folded into
``kanban_db_dispatch.py``) because it is a pure classification concern
read at multiple points (decompose-time here; a future review-time
consultation by the sdlc-review skill is explicitly OUT of scope for
this change — see the module docstring in ``kanban_decompose.py``).

OWNER DEFAULT (open question in lane_rules_2_4_6.json, "should task_mode
be PO-assigned or automatically inferred?" — still unresolved upstream):
this module makes task_mode a **best-effort, non-blocking heuristic
field** set automatically by the decomposer, not a PO-assigned or
PO-reviewable-mandatory field. Rationale: (1) the classifier grading its
own homework risk flagged in the plan is a real concern for
*enforcement* (deciding how much scrutiny a task gets), but Rule 2's
current scope is schema-population ONLY — no dispatch/gate/enforcement
logic reads ``task_mode`` yet (confirmed Wave2/1a-schema-only), so a
misclassification here has no security/gating consequence today; (2) a
mandatory PO round-trip on every single created task (including trivial
one-liners) would be a heavier synchronous cost than the field's current
blast radius justifies. When ``task_mode`` is later wired into
enforcement, this default should be revisited by the owner (flagged
loudly in the Rule 2 commit message).

Heuristic (not an LLM call) — chosen over a second aux-LLM round-trip
per unit because: (a) it composes with existing structured signals
(title/body keyword classes, scope breadth) that are cheap and stable to
match on; (b) it runs at every task-creation call site synchronously
with no network/latency cost or LLM non-determinism; (c) the decompose
aux-LLM call already produces title/body text for each child — adding a
second LLM round-trip just to re-read that same text back is redundant
work for a field with no downstream gate. If task_mode classification
accuracy becomes gating-relevant later, an LLM-assisted classifier can
be added behind the same function signature without touching call
sites.
"""

from __future__ import annotations

import re
from typing import Literal

TaskMode = Literal["autocomplete", "chat", "agent"]

# Rule 2 modes, in the order lane_rules_2_4_6.json lists them
# (Autocomplete/Chat/Agent): least to most verification rigor.
_VALID_MODES: tuple[TaskMode, ...] = ("autocomplete", "chat", "agent")

# --- Chat signals: the task asks a question / wants an explanation or
# decision, not a code change. Checked first — a task that is purely a
# question should never be misread as "agent" just because it mentions an
# architecture-sounding noun in passing.
_CHAT_PATTERNS = (
    re.compile(r"\bwhat\s+(does|is|are|should)\b", re.I),
    re.compile(r"\bwhy\s+(does|is|do|did)\b", re.I),
    re.compile(r"\bhow\s+does\b", re.I),
    re.compile(r"\bcan\s+(you|someone)\s+explain\b", re.I),
    re.compile(r"\bexplain\b", re.I),
    re.compile(r"\bclarify\b", re.I),
    re.compile(r"\?\s*$"),  # a title/body ending in a question mark
    re.compile(r"\bno\s+(code\s+change|fix)\s+needed\b", re.I),
    re.compile(r"\bjust\s+(need|want)\b.*\b(explanation|decision|answer)\b", re.I),
)

# --- Agent signals: multi-file / architectural / new-subsystem work that
# needs a full agentic loop (planning, multiple edits, its own tests).
_AGENT_KEYWORDS = (
    "architecture", "architectural", "migration", "migrate", "pipeline",
    "subsystem", "refactor", "redesign", "new module", "new abc",
    "schema migration", "design and build", "spanning", "multi-file",
    "multi-step", "several modules", "across the", "across several",
)
_AGENT_PATTERNS = tuple(re.compile(re.escape(kw), re.I) for kw in _AGENT_KEYWORDS)

# A body naming 2+ distinct file/module paths (word.py, word/word.py, ...)
# is a strong multi-file-scope signal independent of keywords.
_FILE_PATH_RE = re.compile(r"\b[\w/]+\.[a-zA-Z]{1,4}\b")

# --- Autocomplete signals: small, mechanical, single-location edits.
_AUTOCOMPLETE_KEYWORDS = (
    "typo", "rename", "one-line", "one line", "single-line", "single line",
    "single-file", "single file",
)
_AUTOCOMPLETE_PATTERNS = tuple(re.compile(re.escape(kw), re.I) for kw in _AUTOCOMPLETE_KEYWORDS)


def _distinct_file_mentions(text: str) -> int:
    return len(set(_FILE_PATH_RE.findall(text)))


def classify_task_mode(title: str, body: str | None) -> TaskMode:
    """Best-effort heuristic classification of a task's verification-rigor
    mode. Never raises; unrecognised/ambiguous input degrades to the
    safest middle ground (``"chat"``) rather than guessing an extreme.

    This is NOT consulted by any dispatch/gate/enforcement logic — see
    module docstring. Callers should treat a wrong answer as low-stakes.
    """
    title_text = title or ""
    body_text = body or ""
    combined = f"{title_text}\n{body_text}"

    if any(p.search(combined) for p in _CHAT_PATTERNS):
        return "chat"

    agent_hit = any(p.search(combined) for p in _AGENT_PATTERNS)
    multi_file = _distinct_file_mentions(combined) >= 2
    if agent_hit or multi_file:
        return "agent"

    if any(p.search(combined) for p in _AUTOCOMPLETE_PATTERNS):
        return "autocomplete"

    # Short, single-sentence bodies with no other signal read as small,
    # mechanical work.
    if len(body_text.strip()) < 80 and _distinct_file_mentions(combined) <= 1:
        return "autocomplete"

    return "chat"
