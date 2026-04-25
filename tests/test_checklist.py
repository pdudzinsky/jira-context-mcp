"""Unit tests for the Smart Checklist markdown parser."""

from __future__ import annotations

import logging

import pytest

from jira_context_mcp.jira import parse_checklist_markdown


def test_empty_string_returns_no_sections() -> None:
    cl = parse_checklist_markdown("")
    assert cl.sections == []
    assert cl.items == []


# ---------- legacy task-list markers ----------


@pytest.mark.parametrize(
    "marker,expected_status",
    [
        (" ", "open"),
        ("x", "done"),
        ("X", "done"),
        ("-", "in_progress"),
        ("~", "skipped"),
    ],
)
def test_legacy_marker_maps_to_status(marker: str, expected_status: str) -> None:
    cl = parse_checklist_markdown(f"[{marker}] item text")
    assert len(cl.items) == 1
    assert cl.items[0].status == expected_status
    assert cl.items[0].name == "item text"


def test_unknown_legacy_marker_falls_back_to_open_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="jira_context_mcp.jira"):
        cl = parse_checklist_markdown("[?] strange marker")
    assert len(cl.items) == 1
    assert cl.items[0].status == "open"
    assert any(
        "unknown Smart Checklist status marker" in rec.message for rec in caplog.records
    )


# ---------- modern bullet format ----------


def test_bullet_item_default_open_status() -> None:
    cl = parse_checklist_markdown("- plain bullet text")
    assert len(cl.items) == 1
    assert cl.items[0].status == "open"
    assert cl.items[0].name == "plain bullet text"


def test_mixed_legacy_and_bullet_in_one_blob() -> None:
    cl = parse_checklist_markdown("[x] legacy done\n- modern bullet")
    statuses = [(it.name, it.status) for it in cl.items]
    assert statuses == [("legacy done", "done"), ("modern bullet", "open")]


# ---------- section grouping ----------


def test_section_header_opens_new_section() -> None:
    raw = "## Section A\n- alpha\n- beta"
    cl = parse_checklist_markdown(raw)
    assert len(cl.sections) == 1
    sec = cl.sections[0]
    assert sec.title == "Section A"
    assert [it.name for it in sec.items] == ["alpha", "beta"]


def test_multiple_sections_keep_items_separate() -> None:
    raw = (
        "## 1. First\n"
        "- a\n"
        "- b\n"
        "## 2. Second\n"
        "- c\n"
    )
    cl = parse_checklist_markdown(raw)
    assert [s.title for s in cl.sections] == ["1. First", "2. Second"]
    assert [it.name for s in cl.sections for it in s.items] == ["a", "b", "c"]


def test_empty_section_with_title_is_preserved() -> None:
    """Header followed by another header (no items) keeps the empty section."""
    raw = "## Important Note\n## 1. Real section\n- item one"
    cl = parse_checklist_markdown(raw)
    assert len(cl.sections) == 2
    assert cl.sections[0].title == "Important Note"
    assert cl.sections[0].items == []
    assert cl.sections[1].title == "1. Real section"
    assert len(cl.sections[1].items) == 1


def test_orphan_items_before_first_header_get_titleless_section() -> None:
    raw = "- orphan one\n- orphan two\n## Section\n- under header"
    cl = parse_checklist_markdown(raw)
    assert cl.sections[0].title is None
    assert [it.name for it in cl.sections[0].items] == ["orphan one", "orphan two"]
    assert cl.sections[1].title == "Section"


def test_items_property_is_flat_across_sections() -> None:
    raw = "## A\n- x\n- y\n## B\n- z"
    cl = parse_checklist_markdown(raw)
    assert [it.name for it in cl.items] == ["x", "y", "z"]


# ---------- whitespace and skipping ----------


def test_whitespace_around_marker_and_name_is_stripped() -> None:
    cl = parse_checklist_markdown("   [x]    padded item   ")
    assert len(cl.items) == 1
    assert cl.items[0].name == "padded item"
    assert cl.items[0].status == "done"


def test_blank_lines_skipped() -> None:
    raw = "[ ] one\n\n\n[ ] two\n"
    cl = parse_checklist_markdown(raw)
    assert [it.name for it in cl.items] == ["one", "two"]


def test_line_without_brackets_or_bullet_is_skipped() -> None:
    raw = "[ ] keep this\nplain prose ignored\n- and this"
    cl = parse_checklist_markdown(raw)
    assert [it.name for it in cl.items] == ["keep this", "and this"]


def test_empty_bullet_line_is_skipped() -> None:
    raw = "- \n- real item"
    cl = parse_checklist_markdown(raw)
    assert [it.name for it in cl.items] == ["real item"]


def test_empty_legacy_marker_body_is_skipped() -> None:
    raw = "[ ]\n[x]    \n[ ] real one"
    cl = parse_checklist_markdown(raw)
    assert [it.name for it in cl.items] == ["real one"]
