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
