"""Unit tests for the Smart Checklist markdown parser.

The grammar under test is Railsware's own, for Smart Checklist Cloud:
https://railsware.atlassian.net/wiki/spaces/CHK/pages/90636343/Formatting+Guide+Markdown
"""

from __future__ import annotations

import pytest

from jira_context_mcp.jira import parse_checklist_markdown
from jira_context_mcp.models import ChecklistText


def _texts(raw: str) -> list[str]:
    """Every free-text line the parser kept, across all sections."""
    return [
        entry.text
        for section in parse_checklist_markdown(raw).sections
        for entry in section.entries
        if isinstance(entry, ChecklistText)
    ]


def test_empty_string_returns_no_sections() -> None:
    cl = parse_checklist_markdown("")
    assert cl.sections == []
    assert cl.items == []
    assert cl.is_empty


# ---------- status markers ----------


@pytest.mark.parametrize(
    ("line", "expected_status"),
    [
        ("- item", "open"),
        ("+ item", "done"),
        ("~ item", "in_progress"),
        ("x item", "skipped"),
    ],
)
def test_status_marker_maps_to_status(line: str, expected_status: str) -> None:
    cl = parse_checklist_markdown(line)
    assert [(it.name, it.status) for it in cl.items] == [("item", expected_status)]


def test_checklist_with_every_item_done_is_not_empty() -> None:
    """Done items carry ``+``: reading only ``-`` lines made this look empty."""
    cl = parse_checklist_markdown("+ one\n+ two\n+ three")
    assert not cl.is_empty
    assert [it.status for it in cl.items] == ["done", "done", "done"]


def test_partly_done_checklist_keeps_the_done_items_and_their_order() -> None:
    cl = parse_checklist_markdown("+ a\n- b\n~ c\nx d\n+ e")
    assert [(it.name, it.status) for it in cl.items] == [
        ("a", "done"),
        ("b", "open"),
        ("c", "in_progress"),
        ("d", "skipped"),
        ("e", "done"),
    ]


def test_text_after_the_marker_is_kept_as_written() -> None:
    cl = parse_checklist_markdown("+ **bold** with `code`, @user and - a dash")
    assert cl.items[0].name == "**bold** with `code`, @user and - a dash"


def test_item_text_may_itself_start_with_a_dash() -> None:
    """Only the first marker counts: ``- - x`` is a to-do called ``- x``."""
    cl = parse_checklist_markdown("- - 3.2 nested looking")
    assert [(it.name, it.status) for it in cl.items] == [("- 3.2 nested looking", "open")]


@pytest.mark.parametrize(
    ("line", "name", "status"),
    [
        ("-no space", "no space", "open"),
        ("+no space", "no space", "done"),
    ],
)
def test_dash_and_plus_do_not_need_a_space(line: str, name: str, status: str) -> None:
    """Allowed since plugin v1.0.2; the release note names ``-``, ``+`` and ``#`` only."""
    cl = parse_checklist_markdown(line)
    assert [(it.name, it.status) for it in cl.items] == [(name, status)]


@pytest.mark.parametrize(
    ("line", "status"),
    [
        ("-   padded", "open"),
        ("+\tpadded", "done"),
        ("~   padded", "in_progress"),
        ("x\tpadded", "skipped"),
    ],
)
def test_extra_whitespace_after_a_marker_is_not_part_of_the_name(line: str, status: str) -> None:
    item = parse_checklist_markdown(line).items[0]
    assert (item.name, item.status) == ("padded", status)


@pytest.mark.parametrize(
    "line",
    [
        "~~struck through~~",
        "xylophone",
        "x-ray of the module",
        "~>>LT-abc",
        "X capital is not the marker",
    ],
)
def test_tilde_and_x_need_a_space_and_stay_text_otherwise(line: str) -> None:
    cl = parse_checklist_markdown(line)
    assert cl.items == []
    assert _texts(line) == [line]


@pytest.mark.parametrize("line", ["-", "+", "~", "x", "- ", "+ ", "-!", "+! ", "x!"])
def test_marker_without_text_adds_nothing(line: str) -> None:
    assert parse_checklist_markdown(line).is_empty


# ---------- mandatory items ----------


@pytest.mark.parametrize(
    ("line", "status"),
    [
        ("-! item", "open"),
        ("+! item", "done"),
        ("~! item", "in_progress"),
        ("x! item", "skipped"),
    ],
)
def test_mandatory_marker_keeps_status_and_sets_flag(line: str, status: str) -> None:
    item = parse_checklist_markdown(line).items[0]
    assert (item.name, item.status, item.mandatory) == ("item", status, True)


def test_mandatory_item_with_custom_status() -> None:
    item = parse_checklist_markdown("-! [PASSED] item").items[0]
    assert (item.name, item.status, item.custom_status, item.mandatory) == (
        "item",
        "open",
        "PASSED",
        True,
    )


def test_items_are_not_mandatory_by_default() -> None:
    assert parse_checklist_markdown("- item").items[0].mandatory is False


def test_exclamation_mark_after_the_space_is_item_text() -> None:
    item = parse_checklist_markdown("- ! item").items[0]
    assert (item.name, item.mandatory) == ("! item", False)


# ---------- custom statuses and attributes ----------


@pytest.mark.parametrize(
    ("line", "custom_status", "status"),
    [
        ("- [Failed] item", "Failed", "open"),
        ("- [IN REVIEW] item", "IN REVIEW", "open"),
        ("- [not-needed] item", "not-needed", "open"),
        ("+ [PASSED] item", "PASSED", "done"),
    ],
)
def test_custom_status_is_split_from_the_name(line: str, custom_status: str, status: str) -> None:
    item = parse_checklist_markdown(line).items[0]
    assert (item.name, item.custom_status, item.status) == ("item", custom_status, status)


@pytest.mark.parametrize(
    "line",
    [
        "- [link to google](http://google.com)",
        r"~ \[Text in brackets\]",
        '- [priority="High"] item',
        '- [dueDate="2026-06-07"][assignee="abc"] item',
        "- [ ] checkbox pasted from GitHub",
        "- [only a bracket]",
        "- [" + "n" * 51 + "] longer than a custom status may be",
    ],
)
def test_brackets_that_are_not_a_custom_status_stay_in_the_name(line: str) -> None:
    item = parse_checklist_markdown(line).items[0]
    assert item.custom_status is None
    assert item.name == line.split(maxsplit=1)[1]


def test_custom_status_next_to_attributes_keeps_the_attributes() -> None:
    before = parse_checklist_markdown('- [FAILED][priority="High"] item').items[0]
    after = parse_checklist_markdown('- [priority="High"][FAILED] item').items[0]
    assert (before.custom_status, before.name) == ("FAILED", '[priority="High"] item')
    assert (after.custom_status, after.name) == ("FAILED", '[priority="High"] item')


def test_custom_status_after_several_attributes_keeps_them_all() -> None:
    item = parse_checklist_markdown('- [priority="High"][dueDate="2026-06-09"][FAILED] x').items[0]
    assert item.custom_status == "FAILED"
    assert item.name == '[priority="High"][dueDate="2026-06-09"] x'


def test_custom_status_after_an_attribute_and_a_space_leaves_one_space_in_the_name() -> None:
    item = parse_checklist_markdown('- [priority="High"] [FAILED] item').items[0]
    assert (item.custom_status, item.name) == ("FAILED", '[priority="High"] item')


def test_custom_status_may_be_exactly_fifty_characters() -> None:
    name = "n" * 50
    item = parse_checklist_markdown(f"- [{name}] item").items[0]
    assert (item.custom_status, item.name) == (name, "item")


@pytest.mark.parametrize(
    "line", ["- [ IN REVIEW ] item", "- [IN REVIEW ] item", "- [ IN REVIEW] item"]
)
def test_whitespace_inside_the_custom_status_brackets_is_trimmed(line: str) -> None:
    assert parse_checklist_markdown(line).items[0].custom_status == "IN REVIEW"


# ---------- item details ----------


def test_detail_lines_belong_to_the_item_above() -> None:
    raw = "- first\n> piece of information\n>no space after the quote\n+ second\n> other"
    first, second = parse_checklist_markdown(raw).items
    assert first.details == ["piece of information", "no space after the quote"]
    assert second.details == ["other"]


def test_nested_detail_keeps_its_inner_quote() -> None:
    item = parse_checklist_markdown("- item\n> * bullet\n> > * nested").items[0]
    assert item.details == ["* bullet", "> * nested"]


def test_bare_quote_line_is_an_empty_detail() -> None:
    item = parse_checklist_markdown("- item\n>\n> after").items[0]
    assert item.details == ["", "after"]


def test_detail_keeps_indentation_beyond_the_first_space() -> None:
    item = parse_checklist_markdown("- item\n>   indented").items[0]
    assert item.details == ["  indented"]


def test_detail_does_not_count_as_an_item() -> None:
    assert len(parse_checklist_markdown("- item\n> detail").items) == 1


def test_detail_survives_a_blank_line() -> None:
    item = parse_checklist_markdown("- item\n\n> detail").items[0]
    assert item.details == ["detail"]


@pytest.mark.parametrize(
    "raw",
    [
        "> before any item",
        "## Header\n> under a header",
        "- item\nsome text\n> after the text",
    ],
)
def test_quote_line_with_no_item_above_is_kept_verbatim_as_text(raw: str) -> None:
    assert _texts(raw)[-1] == raw.splitlines()[-1]


# ---------- headers ----------


def test_section_header_opens_new_section() -> None:
    raw = "## Section A\n- alpha\n- beta"
    cl = parse_checklist_markdown(raw)
    assert len(cl.sections) == 1
    sec = cl.sections[0]
    assert sec.title == "Section A"
    assert sec.level == 2
    assert [it.name for it in sec.items] == ["alpha", "beta"]


def test_multiple_sections_keep_items_separate() -> None:
    raw = "## 1. First\n- a\n- b\n## 2. Second\n- c\n"
    cl = parse_checklist_markdown(raw)
    assert [s.title for s in cl.sections] == ["1. First", "2. Second"]
    assert [it.name for s in cl.sections for it in s.items] == ["a", "b", "c"]


def test_nested_headers_lose_neither_items_nor_statuses_nor_levels() -> None:
    raw = (
        "## 1. Parent one\n"
        "+ 1.1 first\n"
        "+ 1.2 second\n"
        "## 2. Parent two\n"
        "### 2a. Child\n"
        "+ 2.1 third\n"
        "### 2b. Other child\n"
        "- 2.2 fourth\n"
    )
    cl = parse_checklist_markdown(raw)
    assert [(s.title, s.level, len(s.items)) for s in cl.sections] == [
        ("1. Parent one", 2, 2),
        ("2. Parent two", 2, 0),
        ("2a. Child", 3, 1),
        ("2b. Other child", 3, 1),
    ]
    assert [it.status for it in cl.items] == ["done", "done", "done", "open"]


@pytest.mark.parametrize("level", range(1, 7))
def test_every_documented_header_level_is_recognised(level: int) -> None:
    section = parse_checklist_markdown("#" * level + " Title\n- item").sections[0]
    assert (section.title, section.level) == ("Title", level)


def test_header_does_not_need_a_space() -> None:
    section = parse_checklist_markdown("##No space\n- item").sections[0]
    assert (section.title, section.level) == ("No space", 2)


def test_seven_hashes_are_not_a_header() -> None:
    cl = parse_checklist_markdown("####### too deep\n- item")
    assert _texts("####### too deep\n- item") == ["####### too deep"]
    assert cl.sections[0].title is None


def test_empty_section_with_title_is_preserved() -> None:
    """Header followed by another header (no items) keeps the empty section."""
    raw = "## Important Note\n## 1. Real section\n- item one"
    cl = parse_checklist_markdown(raw)
    assert len(cl.sections) == 2
    assert cl.sections[0].title == "Important Note"
    assert cl.sections[0].items == []
    assert cl.sections[1].title == "1. Real section"
    assert len(cl.sections[1].items) == 1


def test_checklist_with_only_headers_is_not_empty() -> None:
    assert not parse_checklist_markdown("## A note made with a header").is_empty


def test_header_without_a_title_still_starts_a_section() -> None:
    cl = parse_checklist_markdown("## Named\n- a\n## \n- b")
    assert [s.title for s in cl.sections] == ["Named", None]
    assert [it.name for it in cl.sections[1].items] == ["b"]


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


# ---------- plugin lines with no content of their own ----------


def test_separator_is_neither_an_item_nor_text() -> None:
    cl = parse_checklist_markdown("- a\n---\n- b\n-----")
    assert [it.name for it in cl.items] == ["a", "b"]
    assert _texts("- a\n---\n- b\n-----") == []


@pytest.mark.parametrize(
    ("line", "name"),
    [("--- not a separator", "-- not a separator"), ("---x", "--x")],
)
def test_dashes_followed_by_text_are_not_a_separator(line: str, name: str) -> None:
    assert [it.name for it in parse_checklist_markdown(line).items] == [name]


@pytest.mark.parametrize("line", ["<<~LT-12 trailing", "~>>LT-12 trailing"])
def test_text_after_a_linked_template_wrapper_is_kept(line: str) -> None:
    assert _texts(line) == [line]


def test_linked_template_wrapper_is_dropped_and_its_items_are_kept() -> None:
    """The plugin's own sample from its Linked Templates page."""
    raw = (
        "- Test item 1\n"
        "<<~LT-1143\n"
        "~! Linked item 1 2024-03-05\n"
        "- Linked item 2\n"
        "+ Linked item 3\n"
        ">Please check the details below\n"
        "~>>LT-1143\n"
    )
    cl = parse_checklist_markdown(raw)
    assert [(it.name, it.status, it.mandatory) for it in cl.items] == [
        ("Test item 1", "open", False),
        ("Linked item 1 2024-03-05", "in_progress", True),
        ("Linked item 2", "open", False),
        ("Linked item 3", "done", False),
    ]
    assert cl.items[-1].details == ["Please check the details below"]
    assert _texts(raw) == []


# ---------- everything else is kept as text ----------


def test_plain_text_stays_in_place_between_items() -> None:
    raw = "- first\n**Sub heading**\n- second"
    entries = parse_checklist_markdown(raw).sections[0].entries
    assert [getattr(e, "name", None) or getattr(e, "text", None) for e in entries] == [
        "first",
        "**Sub heading**",
        "second",
    ]


def test_checklist_with_only_plain_text_is_not_empty() -> None:
    raw = "1.1 The first requirement\n1.2 The second requirement"
    cl = parse_checklist_markdown(raw)
    assert not cl.is_empty
    assert cl.items == []
    assert _texts(raw) == raw.splitlines()


@pytest.mark.parametrize(
    "line",
    [
        "plain prose",
        "**bold text**",
        "* star bullet",
        "1. numbered",
        "```",
        "  - indented bullet",
        "\t+ indented with a tab",
        "  > indented quote",
        "  ## indented header",
    ],
)
def test_lines_the_plugin_does_not_treat_as_items_stay_verbatim_text(line: str) -> None:
    """Indentation matters: the plugin's own counters leave indented lines out."""
    cl = parse_checklist_markdown(line)
    assert cl.items == []
    assert _texts(line) == [line]


def test_trailing_whitespace_is_trimmed_from_text() -> None:
    assert _texts("  1. indented number  ") == ["  1. indented number"]


def test_code_fence_does_not_hide_the_items_inside_it() -> None:
    """The plugin parses line by line, so its counters include items inside a fence."""
    raw = "```\n+ inside\n```"
    cl = parse_checklist_markdown(raw)
    assert [(it.name, it.status) for it in cl.items] == [("inside", "done")]
    assert _texts(raw) == ["```", "```"]


def test_blank_lines_are_skipped() -> None:
    raw = "- one\n\n   \n- two\n"
    cl = parse_checklist_markdown(raw)
    assert [it.name for it in cl.items] == ["one", "two"]
    assert _texts(raw) == []


def test_windows_line_endings() -> None:
    cl = parse_checklist_markdown("## Title\r\n+ done\r\n> detail\r\n")
    assert cl.sections[0].title == "Title"
    assert cl.items[0].name == "done"
    assert cl.items[0].details == ["detail"]


# ---------- task-list form: never written by the plugin, kept for compatibility ----------


@pytest.mark.parametrize(
    ("marker", "expected_status"),
    [
        (" ", "open"),
        ("x", "done"),
        ("X", "done"),
        ("-", "in_progress"),
        ("~", "skipped"),
    ],
)
def test_task_list_marker_maps_to_status(marker: str, expected_status: str) -> None:
    cl = parse_checklist_markdown(f"[{marker}] item text")
    assert len(cl.items) == 1
    assert cl.items[0].status == expected_status
    assert cl.items[0].name == "item text"


@pytest.mark.parametrize("line", ["[?] strange marker", "[1] see the spec", "  [a] indented note"])
def test_any_other_character_in_brackets_is_text(line: str) -> None:
    """Jira doesn't count such a line, so it must not inflate the count either."""
    cl = parse_checklist_markdown(line)
    assert cl.items == []
    assert _texts(line) == [line]


def test_task_list_marker_needs_a_space_before_the_text() -> None:
    line = "[x]no space"
    assert parse_checklist_markdown(line).items == []
    assert _texts(line) == [line]


def test_task_list_and_plugin_markers_in_one_blob() -> None:
    cl = parse_checklist_markdown("[x] task list done\n- to do\n+ plugin done")
    statuses = [(it.name, it.status) for it in cl.items]
    assert statuses == [
        ("task list done", "done"),
        ("to do", "open"),
        ("plugin done", "done"),
    ]


def test_whitespace_around_task_list_marker_and_name_is_stripped() -> None:
    cl = parse_checklist_markdown("   [x]    padded item   ")
    assert len(cl.items) == 1
    assert cl.items[0].name == "padded item"
    assert cl.items[0].status == "done"


def test_dash_before_a_bracket_is_a_to_do_like_in_the_plugin() -> None:
    """``- [x] y`` is the documented custom-status form, and Jira shows it as To Do."""
    item = parse_checklist_markdown("- [x] pasted from GitHub").items[0]
    assert (item.status, item.custom_status, item.name) == ("open", "x", "pasted from GitHub")


def test_text_next_to_task_list_items_is_kept() -> None:
    raw = "[ ] keep this\nplain prose kept\n- and this"
    cl = parse_checklist_markdown(raw)
    assert [it.name for it in cl.items] == ["keep this", "and this"]
    assert _texts(raw) == ["plain prose kept"]


def test_empty_bullet_line_is_skipped() -> None:
    raw = "- \n- real item"
    cl = parse_checklist_markdown(raw)
    assert [it.name for it in cl.items] == ["real item"]
    assert _texts(raw) == []


def test_empty_task_list_marker_body_is_skipped() -> None:
    raw = "[ ]\n[x]    \n[ ] real one"
    cl = parse_checklist_markdown(raw)
    assert [it.name for it in cl.items] == ["real one"]
    assert _texts(raw) == []


@pytest.mark.parametrize("line", ["[X]", "[~]", "[-]", "  [ ]"])
def test_every_task_list_marker_with_no_text_adds_nothing(line: str) -> None:
    assert parse_checklist_markdown(line).is_empty
