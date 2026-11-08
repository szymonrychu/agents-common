"""Shared label rules: normalisation vectors, project prefix and matching."""

from __future__ import annotations

import pytest

from agents_common.labels import is_project_label, labels_match, normalize_label, normalize_labels


def test_normalize_trims_lowercases_dedupes():
    assert normalize_labels(["  Foo ", "bar", "FOO", "Bar", "a:b_c.d-e"]) == [
        "foo",
        "bar",
        "a:b_c.d-e",
    ]


def test_normalize_empty_and_order():
    assert normalize_labels([]) == []
    assert normalize_labels(["b", "a", "b"]) == ["b", "a"]


def test_normalize_accepts_bounds():
    assert normalize_labels(["x"]) == ["x"]
    assert normalize_labels(["a" * 40]) == ["a" * 40]
    twenty = [f"l{i}" for i in range(20)]
    assert normalize_labels(twenty) == twenty


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "a" * 41, "has space", "slash/no", "unié", "semi;colon", "a,b", "x\ny"],
)
def test_invalid_label_rejected(bad):
    with pytest.raises(ValueError):
        normalize_labels([bad])


def test_more_than_twenty_labels_rejected():
    with pytest.raises(ValueError):
        normalize_labels([f"l{i}" for i in range(21)])


def test_cap_counts_after_dedupe():
    assert len(normalize_labels([f"l{i}" for i in range(20)] + ["L0", " l1 "])) == 20


def test_normalize_label_single():
    assert normalize_label(" Foo ") == "foo"
    with pytest.raises(ValueError):
        normalize_label("bad label")


@pytest.mark.parametrize(
    "label", ["project:alpha", "project:a", "project:a-1", "project:" + "a" * 32]
)
def test_is_project_label_true(label):
    assert is_project_label(label)


@pytest.mark.parametrize(
    "label",
    [
        "project:",
        "project:-a",
        "project:a-",
        "project:a_b",
        "project:a.b",
        "project:a:b",
        "project:" + "a" * 33,
        "alpha",
        "projects:a",
    ],
)
def test_is_project_label_false(label):
    assert not is_project_label(label)


@pytest.mark.parametrize(
    ("entry", "target", "expected"),
    [
        ([], [], True),
        ([], ["x"], True),
        (["a"], [], False),
        (["a"], ["b"], False),
        (["a", "b"], ["b"], True),
        (["project:x"], ["project:x"], True),
    ],
)
def test_labels_match(entry, target, expected):
    assert labels_match(entry, target) is expected
