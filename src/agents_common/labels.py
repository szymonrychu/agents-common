"""Shared preset label rules for agents services.

A label is trimmed and lowercased, 1 to 40 characters of [a-z0-9-_.:]; a list is deduplicated
keeping first-seen order and holds at most 20 labels. The orchestrator and agents-taskboard keep
their own copies for now (switching them is the labels epic).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

LABEL_MAX_LEN = 40
LABELS_MAX = 20
_LABEL = re.compile(r"[a-z0-9\-_.:]{1,40}")

PROJECT_PREFIX = "project:"
PROJECT_SLUG_MAX = 32
_PROJECT_SLUG = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")


def normalize_label(raw: str) -> str:
    label = raw.strip().lower()
    if not _LABEL.fullmatch(label):
        raise ValueError(
            f"invalid label {raw!r}: use 1 to {LABEL_MAX_LEN} characters of a-z, 0-9, - _ . :"
        )
    return label


def normalize_labels(raw: Iterable[str]) -> list[str]:
    """Normalised labels, deduplicated in first-seen order; ValueError on an invalid label or
    more than 20 distinct labels."""
    labels = list(dict.fromkeys(normalize_label(r) for r in raw))
    if len(labels) > LABELS_MAX:
        raise ValueError(f"at most {LABELS_MAX} labels are allowed")
    return labels


def is_project_label(label: str) -> bool:
    """True for `project:<slug>` with a slug of 1 to 32 characters. Apply to normalised labels:
    a label that starts with `project:` and is not a project label is invalid."""
    if not label.startswith(PROJECT_PREFIX):
        return False
    slug = label[len(PROJECT_PREFIX) :]
    return 1 <= len(slug) <= PROJECT_SLUG_MAX and _PROJECT_SLUG.fullmatch(slug) is not None


def labels_match(entry_labels: Iterable[str], target_labels: Iterable[str]) -> bool:
    """True when the entry is not restricted to other projects. Only `project:` labels restrict:
    an entry without any `project:` label (unlabelled, or tags only) matches every target; an
    entry with `project:` labels matches only when the target holds at least one of them. All
    other labels are descriptive tags and never decide. Inputs are already normalised."""
    scope = {label for label in entry_labels if label.startswith(PROJECT_PREFIX)}
    return not scope or not scope.isdisjoint(target_labels)
