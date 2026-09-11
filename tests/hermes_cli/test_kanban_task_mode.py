"""Behaviour-contract tests for Rule 2's task-mode classifier.

INVARIANT/contract style per AGENTS.md: assert relationships between
title/body content and the resulting mode, never a frozen snapshot of a
specific classification. Written FIRST (RED) before
``hermes_cli/kanban_task_mode.py`` exists.
"""

from __future__ import annotations

from hermes_cli.kanban_task_mode import classify_task_mode


def test_trivial_typo_fix_classifies_as_autocomplete():
    mode = classify_task_mode(
        "Fix typo in README",
        "Change 'recieve' to 'receive' on line 42 of README.md.",
    )
    assert mode == "autocomplete"


def test_one_line_rename_classifies_as_autocomplete():
    mode = classify_task_mode(
        "Rename variable foo to bar",
        "In utils.py, rename the local variable `foo` to `bar` for clarity. "
        "Single-file, single-line change.",
    )
    assert mode == "autocomplete"


def test_answer_a_question_classifies_as_chat():
    mode = classify_task_mode(
        "What does the retry logic do here?",
        "Can someone explain how kanban_db_dispatch.py decides when to retry "
        "a failed task? No code change needed, just an explanation.",
    )
    assert mode == "chat"


def test_clarify_a_design_choice_classifies_as_chat():
    mode = classify_task_mode(
        "Clarify: should retries reset the failure counter?",
        "Question for the team: when a task is manually retried, should "
        "consecutive_failures reset to zero? Just need a decision, not code.",
    )
    assert mode == "chat"


def test_multi_file_architecture_change_classifies_as_agent():
    mode = classify_task_mode(
        "Implement the new plugin discovery pipeline",
        "Design and build a new plugin discovery + loading pipeline spanning "
        "tools/registry.py, plugins/loader.py, and hermes_cli/plugins_cmd.py. "
        "Needs a new ABC, migration of existing plugins, and tests across "
        "several modules. This is a multi-step architectural change.",
    )
    assert mode == "agent"


def test_build_a_new_feature_classifies_as_agent():
    mode = classify_task_mode(
        "Build the new kanban dispatch retry gate",
        "Implement a new dispatch-side gate in kanban_db_dispatch.py that "
        "checks decision_hud before retrying a blocked task. Requires a new "
        "schema migration, a new bridge function, and updated tests across "
        "the dispatcher module and its test suite.",
    )
    assert mode == "agent"


def test_agent_mode_signal_outweighs_short_body():
    # A short body naming multiple files/an architecture keyword should still
    # win agent-mode over a naive "short body -> autocomplete" heuristic.
    mode = classify_task_mode(
        "Refactor the auth module",
        "Split hermes_cli/auth.py into hermes_cli/auth_*.py siblings.",
    )
    assert mode == "agent"


def test_autocomplete_and_chat_are_distinct_for_their_examples():
    autocomplete = classify_task_mode(
        "Fix typo in comment",
        "Change 'teh' to 'the' in a comment in cli.py.",
    )
    chat = classify_task_mode(
        "Why does this test fail intermittently?",
        "Can you explain why test_foo is flaky? No fix needed yet, just "
        "want to understand the failure mode.",
    )
    assert autocomplete != chat


def test_classifier_only_returns_known_modes():
    for title, body in [
        ("Fix typo", "one word change"),
        ("What is this for?", "explain please"),
        ("Build a new subsystem", "large multi-file architectural change"),
    ]:
        assert classify_task_mode(title, body) in ("autocomplete", "chat", "agent")
