"""Thin, load-by-path bridges into standalone (non-git-tracked-in-tree)
plugins living under ``~/.hermes/plugins/<name>/``.

A bridge module never vendors/duplicates the plugin's logic — it locates the
plugin's own module by absolute file path (the plugin is not installed as an
importable Python package) and calls straight into it, so the plugin stays
the single source of truth for its own behaviour.
"""

from __future__ import annotations
