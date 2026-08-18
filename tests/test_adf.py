"""Unit tests for the ADF -> markdown walker."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from jira_context_mcp.adf import (
    adf_to_markdown,
    collect_media_filenames,
    normalise_attachment_filename,
)
from jira_context_mcp.models import Attachment


def _att(att_id: str, filename: str) -> Attachment:
    """Build an Attachment with sane defaults for ADF resolution tests."""
    return Attachment(
        id=att_id,
        filename=filename,
        mime_type="image/png",
        size=1024,
        created=datetime(2026, 1, 1, tzinfo=UTC),
        author="Jane",
        content_url=f"https://x.atlassian.net/rest/api/3/attachment/content/{att_id}",
    )


def doc(*content: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal ADF doc with the given block content."""
    return {"type": "doc", "content": list(content)}


def para(*inline: dict[str, Any]) -> dict[str, Any]:
    return {"type": "paragraph", "content": list(inline)}


def text(s: str, marks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": s}
    if marks is not None:
        node["marks"] = marks
    return node


def heading(level: int, *inline: dict[str, Any]) -> dict[str, Any]:
    return {"type": "heading", "attrs": {"level": level}, "content": list(inline)}


def bullet_list(*items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "bulletList", "content": list(items)}


def ordered_list(*items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "orderedList", "content": list(items)}


def list_item(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "listItem", "content": list(content)}


def table(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"type": "table", "content": list(rows)}


def table_row(*cells: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tableRow", "content": list(cells)}


def table_cell(*content: dict[str, Any], attrs: dict[str, Any] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "tableCell", "content": list(content)}
    if attrs is not None:
        node["attrs"] = attrs
    return node


def table_header(*content: dict[str, Any], attrs: dict[str, Any] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "tableHeader", "content": list(content)}
    if attrs is not None:
        node["attrs"] = attrs
    return node


# ---------- basics ----------


def test_none_input_returns_none() -> None:
    assert adf_to_markdown(None) is None


def test_non_dict_input_returns_none() -> None:
    assert adf_to_markdown("plain string") is None
    assert adf_to_markdown(42) is None
    assert adf_to_markdown(["list"]) is None


def test_non_doc_input_returns_none() -> None:
    assert adf_to_markdown({"type": "paragraph"}) is None


def test_empty_doc_returns_none() -> None:
    assert adf_to_markdown({"type": "doc", "content": []}) is None
    assert adf_to_markdown({"type": "doc"}) is None


def test_paragraph_plain_text() -> None:
    assert adf_to_markdown(doc(para(text("hello world")))) == "hello world"


def test_mark_strong() -> None:
    out = adf_to_markdown(doc(para(text("bold", [{"type": "strong"}]))))
    assert out == "**bold**"


def test_mark_em() -> None:
    out = adf_to_markdown(doc(para(text("italic", [{"type": "em"}]))))
    assert out == "*italic*"


def test_mark_code() -> None:
    out = adf_to_markdown(doc(para(text("snippet", [{"type": "code"}]))))
    assert out == "`snippet`"


def test_mark_strike() -> None:
    out = adf_to_markdown(doc(para(text("nope", [{"type": "strike"}]))))
    assert out == "~~nope~~"


def test_mark_link() -> None:
    out = adf_to_markdown(
        doc(para(text("click", [{"type": "link", "attrs": {"href": "https://x.y"}}])))
    )
    assert out == "[click](https://x.y)"


def test_marks_stack() -> None:
    out = adf_to_markdown(doc(para(text("x", [{"type": "strong"}, {"type": "em"}]))))
    assert out == "***x***"


def test_unknown_mark_drops_decoration_keeps_text() -> None:
    out = adf_to_markdown(
        doc(para(text("plain", [{"type": "textColor", "attrs": {"color": "#f00"}}])))
    )
    assert out == "plain"


# ---------- structure ----------


@pytest.mark.parametrize("level", [1, 2, 3, 4, 5, 6])
def test_heading_levels(level: int) -> None:
    out = adf_to_markdown(doc(heading(level, text("H"))))
    assert out == f"{'#' * level} H"


@pytest.mark.parametrize("level", [0, 9, -1, "bad"])
def test_heading_invalid_level_clamps_to_one(level: Any) -> None:
    node = {"type": "heading", "attrs": {"level": level}, "content": [text("H")]}
    assert adf_to_markdown(doc(node)) == "# H"


def test_bullet_list_flat() -> None:
    out = adf_to_markdown(
        doc(
            bullet_list(
                list_item(para(text("a"))),
                list_item(para(text("b"))),
            )
        )
    )
    assert out == "- a\n- b"


def test_ordered_list_flat() -> None:
    out = adf_to_markdown(
        doc(
            ordered_list(
                list_item(para(text("a"))),
                list_item(para(text("b"))),
            )
        )
    )
    assert out == "1. a\n2. b"


def test_nested_bullet_in_bullet() -> None:
    out = adf_to_markdown(
        doc(
            bullet_list(
                list_item(
                    para(text("outer")),
                    bullet_list(list_item(para(text("inner")))),
                )
            )
        )
    )
    assert "- outer" in out
    assert "  - inner" in out


def test_nested_ordered_in_bullet() -> None:
    out = adf_to_markdown(
        doc(
            bullet_list(
                list_item(
                    para(text("outer")),
                    ordered_list(list_item(para(text("inner")))),
                )
            )
        )
    )
    assert "- outer" in out
    assert "  1. inner" in out


def test_code_block_with_language() -> None:
    node = {
        "type": "codeBlock",
        "attrs": {"language": "python"},
        "content": [text("def x():\n    pass")],
    }
    assert adf_to_markdown(doc(node)) == "```python\ndef x():\n    pass\n```"


def test_code_block_without_language() -> None:
    node = {"type": "codeBlock", "content": [text("raw")]}
    assert adf_to_markdown(doc(node)) == "```\nraw\n```"


def test_blockquote_single_paragraph() -> None:
    node = {"type": "blockquote", "content": [para(text("quoted"))]}
    assert adf_to_markdown(doc(node)) == "> quoted"


def test_blockquote_multi_paragraph_keeps_quote_continuation() -> None:
    node = {
        "type": "blockquote",
        "content": [para(text("first")), para(text("second"))],
    }
    out = adf_to_markdown(doc(node))
    assert out == "> first\n>\n> second"


def test_blockquote_with_bullet_list() -> None:
    node = {
        "type": "blockquote",
        "content": [
            para(text("intro")),
            bullet_list(list_item(para(text("a"))), list_item(para(text("b")))),
        ],
    }
    out = adf_to_markdown(doc(node))
    assert "> intro" in out
    assert "> - a" in out
    assert "> - b" in out


# ---------- inline ----------


def test_hard_break_in_paragraph() -> None:
    out = adf_to_markdown(doc(para(text("a"), {"type": "hardBreak"}, text("b"))))
    assert out == "a  \nb"


def test_mention_with_text() -> None:
    out = adf_to_markdown(doc(para({"type": "mention", "attrs": {"id": "abc", "text": "@Alice"}})))
    assert out == "@Alice"


def test_mention_with_id_only() -> None:
    out = adf_to_markdown(doc(para({"type": "mention", "attrs": {"id": "557:xyz"}})))
    assert out == "@user:557:xyz"


def test_mention_empty_attrs() -> None:
    out = adf_to_markdown(doc(para({"type": "mention", "attrs": {}})))
    assert out == "@user"


def test_emoji_with_text() -> None:
    out = adf_to_markdown(
        doc(para({"type": "emoji", "attrs": {"shortName": ":fire:", "text": "🔥"}}))
    )
    assert out == "🔥"


def test_emoji_with_short_name_only() -> None:
    out = adf_to_markdown(doc(para({"type": "emoji", "attrs": {"shortName": ":fire:"}})))
    assert out == ":fire:"


def test_emoji_empty() -> None:
    out = adf_to_markdown(doc(para(text("x"), {"type": "emoji", "attrs": {}})))
    assert out == "x"


def test_inline_card_with_url() -> None:
    out = adf_to_markdown(doc(para({"type": "inlineCard", "attrs": {"url": "https://ex.com/1"}})))
    assert out == "https://ex.com/1"


def test_inline_card_without_url() -> None:
    out = adf_to_markdown(doc(para({"type": "inlineCard", "attrs": {}})))
    assert out == "[link]"


# ---------- media + rule ----------


def test_media_single_block_renders_image_placeholder() -> None:
    assert adf_to_markdown(doc({"type": "mediaSingle", "content": []})) == "[image]"


def test_media_group_block_renders_image_placeholder() -> None:
    assert adf_to_markdown(doc({"type": "mediaGroup", "content": []})) == "[image]"


def test_media_inline_renders_image_placeholder_inline() -> None:
    out = adf_to_markdown(doc(para(text("see "), {"type": "mediaInline"})))
    assert out == "see [image]"


# ---------- media + attachment resolution ----------


def test_media_with_alt_matching_filename_resolves_by_filename() -> None:
    """The normal path: ADF carries Atlassian Media UUID in attrs.id and the
    real filename in attrs.alt. Lookup happens via alt -> attachment.id."""
    media_block = {
        "type": "mediaSingle",
        "content": [
            {
                "type": "media",
                "attrs": {
                    "id": "076ea081-250e-4214-bca3-e6adebf268ed",
                    "type": "file",
                    "alt": "mockup.png",
                },
            }
        ],
    }
    out = adf_to_markdown(doc(media_block), attachments=[_att("44416", "mockup.png")])
    assert out == "[attachment: mockup.png (id=44416)]"


def test_media_with_id_matching_jira_id_resolves_by_id_fallback() -> None:
    """Defensive fallback: when attrs.alt is missing but attrs.id happens to
    match a Jira numeric id (rare, but possible on older instances)."""
    media_block = {
        "type": "mediaSingle",
        "content": [{"type": "media", "attrs": {"id": "uuid-1", "type": "file"}}],
    }
    out = adf_to_markdown(doc(media_block), attachments=[_att("uuid-1", "mockup.png")])
    assert out == "[attachment: mockup.png (id=uuid-1)]"


def test_bare_media_with_alt_resolves_to_filename() -> None:
    media_block = {
        "type": "media",
        "attrs": {"id": "ms-uuid", "alt": "design.jpg", "type": "file"},
    }
    out = adf_to_markdown(doc(media_block), attachments=[_att("9999", "design.jpg")])
    assert out == "[attachment: design.jpg (id=9999)]"


def test_media_inline_with_alt_resolves_inline() -> None:
    inline = {"type": "mediaInline", "attrs": {"id": "ms-uuid", "alt": "screenshot.png"}}
    out = adf_to_markdown(
        doc(para(text("see "), inline)),
        attachments=[_att("123", "screenshot.png")],
    )
    assert out == "see [attachment: screenshot.png (id=123)]"


def test_media_with_unknown_alt_and_id_falls_back_to_image_placeholder() -> None:
    media_block = {
        "type": "mediaSingle",
        "content": [{"type": "media", "attrs": {"id": "ms-missing", "alt": "missing.png"}}],
    }
    out = adf_to_markdown(doc(media_block), attachments=[_att("999", "other.png")])
    assert out == "[image]"


def test_media_group_with_multiple_children_resolves_each_by_alt() -> None:
    media_group = {
        "type": "mediaGroup",
        "content": [
            {"type": "media", "attrs": {"id": "ms-a", "alt": "first.png"}},
            {"type": "media", "attrs": {"id": "ms-b", "alt": "second.png"}},
        ],
    }
    out = adf_to_markdown(
        doc(media_group),
        attachments=[_att("11", "first.png"), _att("22", "second.png")],
    )
    assert out == "[attachment: first.png (id=11)]\n[attachment: second.png (id=22)]"


def test_media_without_attachments_arg_falls_back_to_image() -> None:
    media_block = {
        "type": "mediaSingle",
        "content": [{"type": "media", "attrs": {"id": "ms", "alt": "any.png"}}],
    }
    assert adf_to_markdown(doc(media_block)) == "[image]"


def test_media_with_no_id_and_no_alt_falls_back_to_image() -> None:
    media_block = {
        "type": "mediaSingle",
        "content": [{"type": "media", "attrs": {}}],
    }
    out = adf_to_markdown(doc(media_block), attachments=[_att("any", "x.png")])
    assert out == "[image]"


def test_jira_uuid_duplicate_suffix_in_attachment_filename_still_resolves() -> None:
    """Jira appends ``(uuid)`` to duplicate filenames; ADF alt stays clean.

    Real-world case from RDGA-7217: the attachment filename was
    ``"Communications - Search (c56f4a5c-e320-4a89-a130-66c61b01f5f5).png"``
    but ``attrs.alt`` was ``"Communications - Search.png"``. The resolver
    must normalise the suffix so the lookup recovers the id.
    """
    media_block = {
        "type": "mediaSingle",
        "content": [
            {
                "type": "media",
                "attrs": {"id": "ms-uuid", "alt": "Communications - Search.png"},
            }
        ],
    }
    out = adf_to_markdown(
        doc(media_block),
        attachments=[
            _att(
                "44415",
                "Communications - Search (c56f4a5c-e320-4a89-a130-66c61b01f5f5).png",
            )
        ],
    )
    assert out == "[attachment: Communications - Search.png (id=44415)]"


# ---------- collect_media_filenames / normalise_attachment_filename ----------


def test_collect_media_filenames_finds_alt_in_nested_media() -> None:
    adf = doc(
        para(text("intro")),
        {
            "type": "mediaSingle",
            "content": [{"type": "media", "attrs": {"id": "ms-1", "alt": "a.png"}}],
        },
        {
            "type": "mediaGroup",
            "content": [
                {"type": "media", "attrs": {"id": "ms-2", "alt": "b.png"}},
                {"type": "media", "attrs": {"id": "ms-3", "alt": "c.png"}},
            ],
        },
        para(text("see "), {"type": "mediaInline", "attrs": {"alt": "d.png"}}),
    )
    assert collect_media_filenames(adf) == {"a.png", "b.png", "c.png", "d.png"}


def test_collect_media_filenames_returns_empty_for_none_and_non_dict() -> None:
    assert collect_media_filenames(None) == set()
    assert collect_media_filenames("string") == set()
    assert collect_media_filenames(42) == set()


def test_collect_media_filenames_skips_media_without_alt() -> None:
    adf = doc({"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "ms"}}]})
    assert collect_media_filenames(adf) == set()


def test_normalise_attachment_filename_strips_uuid_suffix() -> None:
    raw = "design (c56f4a5c-e320-4a89-a130-66c61b01f5f5).png"
    assert normalise_attachment_filename(raw) == "design.png"


def test_normalise_attachment_filename_passthrough_when_no_suffix() -> None:
    assert normalise_attachment_filename("design.png") == "design.png"
    assert normalise_attachment_filename("report (v2).pdf") == "report (v2).pdf"


def test_filename_with_parentheses_but_no_uuid_is_not_stripped() -> None:
    """``"report (v2).pdf"`` must not be normalised — the pattern requires
    a full UUID v4 inside the parentheses."""
    media_block = {
        "type": "mediaSingle",
        "content": [{"type": "media", "attrs": {"id": "ms", "alt": "report.pdf"}}],
    }
    out = adf_to_markdown(
        doc(media_block),
        attachments=[_att("1", "report (v2).pdf")],
    )
    assert out == "[image]"  # no match -> fallback


def test_alt_takes_precedence_over_id_when_both_match() -> None:
    """If attrs.alt matches one attachment and attrs.id matches another,
    alt wins — it's the more specific, content-aware signal."""
    media_block = {
        "type": "mediaSingle",
        "content": [{"type": "media", "attrs": {"id": "1", "alt": "from-alt.png"}}],
    }
    out = adf_to_markdown(
        doc(media_block),
        attachments=[
            _att("1", "from-id.png"),  # id-matched would point here
            _att("99", "from-alt.png"),  # alt-matched should win
        ],
    )
    assert out == "[attachment: from-alt.png (id=99)]"


def test_rule_renders_horizontal_divider() -> None:
    out = adf_to_markdown(doc(para(text("before")), {"type": "rule"}, para(text("after"))))
    assert out == "before\n\n---\n\nafter"


# ---------- heading offset ----------


def test_heading_offset_zero_is_default() -> None:
    out = adf_to_markdown(doc(heading(1, text("H"))))
    assert out == "# H"


def test_heading_offset_shifts_levels() -> None:
    d = doc(heading(1, text("h1")), heading(2, text("h2")), heading(3, text("h3")))
    out = adf_to_markdown(d, heading_offset=3)
    assert out == "#### h1\n\n##### h2\n\n###### h3"


def test_heading_offset_clamps_to_six() -> None:
    out = adf_to_markdown(doc(heading(3, text("deep"))), heading_offset=5)
    assert out == "###### deep"


def test_heading_offset_clamps_to_one_for_negative() -> None:
    out = adf_to_markdown(doc(heading(2, text("up"))), heading_offset=-3)
    assert out == "# up"


def test_heading_offset_propagates_through_blockquote() -> None:
    node = {"type": "blockquote", "content": [heading(1, text("inside"))]}
    out = adf_to_markdown(doc(node), heading_offset=3)
    assert out == "> #### inside"


def test_heading_offset_propagates_through_list_item() -> None:
    node = bullet_list(list_item(heading(1, text("h"))))
    out = adf_to_markdown(doc(node), heading_offset=2)
    assert "- ### h" in out


# ---------- tables ----------


def test_table_with_header_row() -> None:
    d = doc(
        table(
            table_row(table_header(para(text("A"))), table_header(para(text("B")))),
            table_row(table_cell(para(text("a1"))), table_cell(para(text("b1")))),
            table_row(table_cell(para(text("a2"))), table_cell(para(text("b2")))),
        )
    )
    assert adf_to_markdown(d) == "| A | B |\n| --- | --- |\n| a1 | b1 |\n| a2 | b2 |"


def test_table_without_header_row_gets_blank_header() -> None:
    d = doc(table(table_row(table_cell(para(text("a"))), table_cell(para(text("b"))))))
    assert adf_to_markdown(d) == "|  |  |\n| --- | --- |\n| a | b |"


def test_table_first_column_header_cells_render_bold() -> None:
    d = doc(
        table(
            table_row(table_header(para(text("k1"))), table_cell(para(text("v1")))),
            table_row(table_header(para(text("k2"))), table_cell(para(text("v2")))),
        )
    )
    assert adf_to_markdown(d) == "|  |  |\n| --- | --- |\n| **k1** | v1 |\n| **k2** | v2 |"


def test_table_all_header_row_mid_table_renders_bold() -> None:
    d = doc(
        table(
            table_row(table_header(para(text("A"))), table_header(para(text("B")))),
            table_row(table_header(para(text("S1"))), table_header(para(text("S2")))),
            table_row(table_cell(para(text("a"))), table_cell(para(text("b")))),
        )
    )
    assert adf_to_markdown(d) == "| A | B |\n| --- | --- |\n| **S1** | **S2** |\n| a | b |"


def test_table_only_header_row_renders_header_and_separator() -> None:
    d = doc(table(table_row(table_header(para(text("A"))), table_header(para(text("B"))))))
    assert adf_to_markdown(d) == "| A | B |\n| --- | --- |"


def test_table_cell_multi_block_joined_with_br() -> None:
    d = doc(table(table_row(table_cell(para(text("a")), para(text("b"))))))
    assert adf_to_markdown(d) == "|  |\n| --- |\n| a<br>b |"


def test_table_cell_hard_break_becomes_br() -> None:
    d = doc(table(table_row(table_cell(para(text("a"), {"type": "hardBreak"}, text("b"))))))
    assert adf_to_markdown(d) == "|  |\n| --- |\n| a<br>b |"


def test_table_cell_bullet_list_flattens() -> None:
    d = doc(
        table(
            table_row(
                table_cell(bullet_list(list_item(para(text("a"))), list_item(para(text("b")))))
            )
        )
    )
    assert adf_to_markdown(d) == "|  |\n| --- |\n| - a<br>- b |"


def test_table_cell_code_block_keeps_fences_and_indent() -> None:
    code = {
        "type": "codeBlock",
        "attrs": {"language": "python"},
        "content": [text("def x():\n    pass")],
    }
    d = doc(table(table_row(table_cell(code))))
    assert adf_to_markdown(d) == "|  |\n| --- |\n| ```python<br>def x():<br>    pass<br>``` |"


def test_table_cell_pipe_escaped() -> None:
    d = doc(table(table_row(table_cell(para(text("a|b"))))))
    assert adf_to_markdown(d) == "|  |\n| --- |\n| a\\|b |"


def test_table_cell_media_resolves_attachment() -> None:
    media_block = {
        "type": "mediaSingle",
        "content": [{"type": "media", "attrs": {"id": "ms", "alt": "mockup.png"}}],
    }
    d = doc(table(table_row(table_cell(media_block))))
    out = adf_to_markdown(d, attachments=[_att("44416", "mockup.png")])
    assert out == "|  |\n| --- |\n| [attachment: mockup.png (id=44416)] |"


def test_heading_offset_applies_inside_table_cell() -> None:
    d = doc(table(table_row(table_cell(heading(1, text("h"))))))
    out = adf_to_markdown(d, heading_offset=3)
    assert out == "|  |\n| --- |\n| #### h |"


def test_empty_table_returns_none() -> None:
    assert adf_to_markdown(doc(table())) is None
    assert adf_to_markdown(doc(table(table_row()))) is None


def test_empty_cell_renders_blank_column() -> None:
    d = doc(
        table(table_row(table_cell(para(text("a"))), table_cell(), table_cell(para(text("c")))))
    )
    assert adf_to_markdown(d) == "|  |  |  |\n| --- | --- | --- |\n| a |  | c |"


def test_row_of_all_empty_cells_still_emitted() -> None:
    d = doc(
        table(
            table_row(table_cell(para(text("a"))), table_cell(para(text("b")))),
            table_row(table_cell(), table_cell()),
        )
    )
    assert adf_to_markdown(d) == "|  |  |\n| --- | --- |\n| a | b |\n|  |  |"


def test_ragged_rows_size_the_header_not_every_row() -> None:
    """The header and delimiter carry the table's full width; a short body row
    is left short, which GFM fills in. Padding every row would make the emitted
    cell count ``rows x widest row``."""
    d = doc(
        table(
            table_row(
                table_cell(para(text("a"))),
                table_cell(para(text("b"))),
                table_cell(para(text("c"))),
            ),
            table_row(table_cell(para(text("x"))), table_cell(para(text("y")))),
        )
    )
    assert adf_to_markdown(d) == "|  |  |  |\n| --- | --- | --- |\n| a | b | c |\n| x | y |"


def test_colspan_expands_to_filler_cells() -> None:
    d = doc(
        table(
            table_row(
                table_cell(para(text("span")), attrs={"colspan": 2}),
                table_cell(para(text("c"))),
            ),
            table_row(
                table_cell(para(text("a"))),
                table_cell(para(text("b"))),
                table_cell(para(text("d"))),
            ),
        )
    )
    assert adf_to_markdown(d) == "|  |  |  |\n| --- | --- | --- |\n| span |  | c |\n| a | b | d |"


def test_rowspan_fills_covered_rows() -> None:
    """The misalignment regression test: the second row's only cell must land
    in column 1 (its true grid position), not column 0."""
    d = doc(
        table(
            table_row(
                table_cell(para(text("merged")), attrs={"rowspan": 2}),
                table_cell(para(text("b1"))),
            ),
            table_row(table_cell(para(text("b2")))),
        )
    )
    assert adf_to_markdown(d) == "|  |  |\n| --- | --- |\n| merged | b1 |\n|  | b2 |"


def test_row_fully_covered_by_rowspan_still_emitted() -> None:
    d = doc(
        table(
            table_row(table_cell(para(text("tall")), attrs={"rowspan": 2})),
            table_row(),
        )
    )
    assert adf_to_markdown(d) == "|  |\n| --- |\n| tall |\n|  |"


@pytest.mark.parametrize("span", ["x", 0, -1, True, None, 2.5])
def test_invalid_span_values_treated_as_one(span: Any) -> None:
    d = doc(
        table(
            table_row(
                table_cell(para(text("a")), attrs={"colspan": span, "rowspan": span}),
                table_cell(para(text("b"))),
            ),
            table_row(table_cell(para(text("x"))), table_cell(para(text("y")))),
        )
    )
    assert adf_to_markdown(d) == "|  |  |\n| --- | --- |\n| a | b |\n| x | y |"


def test_table_inside_blockquote_prefixes_every_line() -> None:
    node = {
        "type": "blockquote",
        "content": [
            table(
                table_row(table_header(para(text("A")))),
                table_row(table_cell(para(text("a")))),
            )
        ],
    }
    assert adf_to_markdown(doc(node)) == "> | A |\n> | --- |\n> | a |"


def test_table_non_row_and_non_dict_children_skipped() -> None:
    row = {
        "type": "tableRow",
        "content": ["junk", para(text("stray")), table_cell(para(text("a")))],
    }
    d = doc({"type": "table", "content": ["junk", 42, para(text("stray")), row]})
    assert adf_to_markdown(d) == "|  |\n| --- |\n| a |"


def test_colspan_and_rowspan_on_same_cell_reserve_full_block() -> None:
    """A cell merged in both directions reserves its whole rectangle, so the
    next row's only cell lands past the merged block rather than inside it.
    Pins the ``range(col, col + colspan)`` carry footprint at adf.py:_render_table
    — every other span test sets colspan or rowspan, never both."""
    d = doc(
        table(
            table_row(
                table_cell(para(text("big")), attrs={"colspan": 2, "rowspan": 2}),
                table_cell(para(text("c1"))),
            ),
            table_row(table_cell(para(text("c2")))),
            table_row(
                table_cell(para(text("a3"))),
                table_cell(para(text("b3"))),
                table_cell(para(text("c3"))),
            ),
        )
    )
    assert adf_to_markdown(d) == (
        "|  |  |  |\n| --- | --- | --- |\n| big |  | c1 |\n|  |  | c2 |\n| a3 | b3 | c3 |"
    )


def test_rowspan_stops_covering_after_it_expires() -> None:
    """The carry must return to zero: the row below an expired rowspan starts
    at column 0 again instead of being pushed right by a stale carry."""
    d = doc(
        table(
            table_row(
                table_cell(para(text("tall")), attrs={"rowspan": 2}),
                table_cell(para(text("b1"))),
            ),
            table_row(table_cell(para(text("b2")))),
            table_row(table_cell(para(text("a3"))), table_cell(para(text("b3")))),
        )
    )
    assert adf_to_markdown(d) == "|  |  |\n| --- | --- |\n| tall | b1 |\n|  | b2 |\n| a3 | b3 |"


def test_rowspan_three_decrements_once_per_row() -> None:
    """Guards the ``rowspan - 1`` off-by-one across more than one covered row."""
    d = doc(
        table(
            table_row(
                table_cell(para(text("tall")), attrs={"rowspan": 3}),
                table_cell(para(text("b1"))),
            ),
            table_row(table_cell(para(text("b2")))),
            table_row(table_cell(para(text("b3")))),
            table_row(table_cell(para(text("a4"))), table_cell(para(text("b4")))),
        )
    )
    assert adf_to_markdown(d) == (
        "|  |  |\n| --- | --- |\n| tall | b1 |\n|  | b2 |\n|  | b3 |\n| a4 | b4 |"
    )


def test_row_covered_only_in_a_later_column_pads_the_gap() -> None:
    """A row whose only coverage sits in column 1 still emits column 0 as an
    empty cell, so the carried column keeps its true position."""
    d = doc(
        table(
            table_row(
                table_cell(para(text("a"))),
                table_cell(para(text("tall")), attrs={"rowspan": 2}),
            ),
            table_row(),
        )
    )
    assert adf_to_markdown(d) == "|  |  |\n| --- | --- |\n| a | tall |\n|  |  |"


def test_oversized_colspan_clamps_to_max_span() -> None:
    """A corrupt colspan clamps to _MAX_TABLE_SPAN rather than collapsing to 1:
    the filler-cell ceiling is preserved without shifting the rest of the row."""
    d = doc(table(table_row(table_cell(para(text("a")), attrs={"colspan": 10**6}))))
    assert adf_to_markdown(d) == "\n".join(
        [
            "| " + " | ".join([""] * 20) + " |",
            "| " + " | ".join(["---"] * 20) + " |",
            "| " + " | ".join(["a", *[""] * 19]) + " |",
        ]
    )


def test_short_first_row_is_not_promoted_and_stays_short() -> None:
    """An unpromoted first row is a body row: it is left short like its
    siblings, and the synthesized header alone carries the full width."""
    d = doc(
        table(
            table_row(table_cell(para(text("a")))),
            table_row(
                table_cell(para(text("b"))),
                table_cell(para(text("c"))),
                table_cell(para(text("d"))),
            ),
        )
    )
    assert adf_to_markdown(d) == "|  |  |  |\n| --- | --- | --- |\n| a |\n| b | c | d |"


def test_short_header_row_is_padded_to_the_widest_row() -> None:
    """A promoted header row IS padded — GFM needs it to match the delimiter."""
    d = doc(
        table(
            table_row(table_header(para(text("A")))),
            table_row(table_cell(para(text("a"))), table_cell(para(text("b")))),
        )
    )
    assert adf_to_markdown(d) == "| A |  |\n| --- | --- |\n| a | b |"


def test_oversized_rowspan_clamps_to_row_count_and_keeps_column() -> None:
    """A rowspan past _MAX_TABLE_SPAN must not collapse to 1 — that would slide
    every row below the merge one column left, under the wrong header."""
    rows = [
        table_row(table_header(para(text("Group"))), table_header(para(text("Req")))),
        table_row(
            table_cell(para(text("Auth")), attrs={"rowspan": 25}),
            table_cell(para(text("R1"))),
        ),
    ]
    rows += [table_row(table_cell(para(text(f"R{i}")))) for i in range(2, 26)]
    out = adf_to_markdown(doc(table(*rows)))
    assert out is not None
    lines = out.splitlines()
    assert lines[0] == "| Group | Req |"
    assert lines[2] == "| Auth | R1 |"
    assert lines[3] == "|  | R2 |"
    assert lines[-1] == "|  | R25 |"


# ---------- graceful fallback ----------


def test_unknown_block_type_renders_placeholder() -> None:
    assert adf_to_markdown(doc({"type": "panel", "content": []})) == "[unsupported: panel]"


def test_unknown_inline_type_renders_placeholder() -> None:
    out = adf_to_markdown(doc(para({"type": "weirdInline"})))
    assert out == "[unsupported: weirdInline]"


def test_non_dict_children_skipped() -> None:
    d = {
        "type": "doc",
        "content": ["not a dict", para(text("real")), 42],
    }
    assert adf_to_markdown(d) == "real"
