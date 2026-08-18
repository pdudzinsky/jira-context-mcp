"""Atlassian Document Format (ADF) to markdown conversion.

Minimal walker that handles the node types commonly used in Jira issue
descriptions and comments: ``paragraph``, ``heading``, ``bulletList``,
``orderedList``, ``listItem``, ``codeBlock``, ``blockquote``, ``rule``,
``table`` / ``tableRow`` / ``tableHeader`` / ``tableCell``,
``text`` (with ``strong``, ``em``, ``code``, ``strike``, ``link`` marks),
``hardBreak``, ``mention``, ``emoji``, ``inlineCard``, and ``mediaSingle`` /
``mediaGroup`` / ``media`` / ``mediaInline``.

Tables render as GitHub-flavored pipe tables; cell content is flattened
onto one line (``<br>`` for line breaks, literal pipes escaped). Header
detection and ``colspan`` / ``rowspan`` handling are documented on
:func:`_render_table`.

Media nodes are resolved against the surrounding ticket's attachment list
so the LLM sees the filename of every embedded image and the id it needs
to fetch the binary via ``get_ticket_attachment``. The resolution path is:

1. ``attrs.alt`` against attachment filenames — the normal path, because
   ADF media nodes carry the Atlassian Media Services UUID in ``attrs.id``,
   which is *not* the same as the Jira attachment id; Jira fills ``alt``
   with the original filename when the user pastes an image.
2. ``attrs.id`` against Jira attachment ids — defensive fallback for the
   rare case where the two identifiers coincide (some older instances).

When neither resolves (orphan media, or an attachment that was deleted
after the body was edited), the marker degrades to plain ``[image]``.

Unknown block or inline nodes are preserved as ``[unsupported: <type>]``
markers so that missing content is visible rather than silently dropped;
unknown marks are silently ignored (the underlying text is still rendered).

Callers can pass ``heading_offset`` to shift every ``heading`` level by a
fixed amount — the ticket-context renderer uses this to nest ADF headings
beneath its own document structure (description blocks live under a level-3
``### Description`` section, so passing ``heading_offset=3`` keeps the
hierarchy consistent and prevents user-authored ``# Story`` headers from
appearing above the document title).

Pure stdlib — no runtime dependencies.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from .models import Attachment

_AdfNode = dict[str, Any]

# When two attachments on the same ticket share a filename, Jira appends a
# UUID v4 in parentheses to the duplicate: ``"foo.png"`` becomes
# ``"foo (c56f4a5c-e320-4a89-a130-66c61b01f5f5).png"``. ADF's ``attrs.alt``
# preserves the original name, so a strict equality match misses the file.
# We normalise the attachment filename back to its pre-duplication form so
# the alt-based lookup can recover it. The pattern is intentionally locked
# to the full UUID v4 shape to avoid false positives on legitimate filenames
# like ``"report (2024-12-01).pdf"`` or ``"design (v2).png"``.
_FILENAME_UUID_SUFFIX_RE: Final = re.compile(
    r"^(.+?) \([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\)(\.[^.]+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _RenderContext:
    """Per-call rendering state threaded through every walker function.

    Grouping ``heading_offset`` and the attachment lookups into a single
    immutable object avoids plumbing several unrelated keyword arguments
    through every helper signature. The two lookups are stored separately
    because media nodes are resolved primarily by filename (via
    ``attrs.alt``) with id-based matching as a defensive fallback.
    """

    heading_offset: int = 0
    attachments_by_filename: dict[str, str] = field(default_factory=dict)
    attachments_by_id: dict[str, str] = field(default_factory=dict)


def adf_to_markdown(
    adf: Any,
    *,
    heading_offset: int = 0,
    attachments: Sequence[Attachment] | None = None,
) -> str | None:
    """Render an ADF document tree as markdown.

    Returns ``None`` when ``adf`` is not an ADF ``doc`` node, when its content
    is empty, or when rendering produces only whitespace.

    ``heading_offset`` shifts every emitted heading by that many levels and
    clamps the result to ``[1..6]``. Defaults to ``0`` so the standalone
    behaviour (e.g. tests, ad-hoc conversions) is unchanged.

    ``attachments`` is the list of files on the surrounding ticket; when
    provided, embedded media nodes are resolved by filename / id to a
    filename-bearing placeholder. The argument is typed as ``Sequence``
    (not ``Iterable``) because the lookup tables are built in two passes
    over the same collection — a single-shot generator would be exhausted
    after the first pass. When omitted, media nodes degrade to the plain
    ``[image]`` placeholder.
    """
    if not isinstance(adf, dict) or adf.get("type") != "doc":
        return None
    if attachments:
        # ``a.id`` is the Jira numeric id — the key callers need for
        # ``get_ticket_attachment``. Both lookup maps point at it so the
        # rendered placeholder always carries the actionable id, regardless
        # of whether the media node was resolved by filename or by id.
        attachments_by_filename: dict[str, str] = {}
        for a in attachments:
            if not a.filename:
                continue
            # Exact filename always wins so it takes priority over
            # normalised entries — the inserts below only fill gaps.
            attachments_by_filename[a.filename] = a.id
        for a in attachments:
            if not a.filename:
                continue
            normalised = normalise_attachment_filename(a.filename)
            if normalised != a.filename:
                attachments_by_filename.setdefault(normalised, a.id)
        attachments_by_id = {a.id: a.filename for a in attachments}
    else:
        attachments_by_filename = {}
        attachments_by_id = {}
    ctx = _RenderContext(
        heading_offset=heading_offset,
        attachments_by_filename=attachments_by_filename,
        attachments_by_id=attachments_by_id,
    )
    blocks = [b for b in _render_blocks(adf.get("content") or [], ctx) if b]
    result = "\n\n".join(blocks).strip()
    return result or None


def _render_blocks(nodes: list[_AdfNode], ctx: _RenderContext) -> list[str]:
    rendered: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        text = _render_block(node, ctx)
        if text:
            rendered.append(text)
    return rendered


def _render_block(node: _AdfNode, ctx: _RenderContext) -> str:
    node_type = node.get("type")
    content = node.get("content") or []
    attrs = node.get("attrs") or {}

    if node_type == "paragraph":
        return _render_inline(content, ctx)
    if node_type == "heading":
        level = attrs.get("level", 1)
        if not isinstance(level, int) or not 1 <= level <= 6:
            level = 1
        adjusted = max(1, min(level + ctx.heading_offset, 6))
        inline = _render_inline(content, ctx)
        return f"{'#' * adjusted} {inline}" if inline else ""
    if node_type == "bulletList":
        return _render_list(content, bullet=True, ctx=ctx)
    if node_type == "orderedList":
        return _render_list(content, bullet=False, ctx=ctx)
    if node_type == "codeBlock":
        lang = attrs.get("language", "") or ""
        text = "".join(
            child.get("text", "")
            for child in content
            if isinstance(child, dict) and child.get("type") == "text"
        )
        return f"```{lang}\n{text}\n```"
    if node_type == "blockquote":
        inner = _render_blocks(content, ctx)
        if not inner:
            return ""
        body = "\n\n".join(inner)
        return "\n".join(f"> {line}" if line else ">" for line in body.splitlines())
    if node_type == "listItem":
        # listItem is normally rendered by _render_list; reaching it here means
        # it appeared at the top level, which we render as its block children.
        return "\n\n".join(_render_blocks(content, ctx))
    if node_type == "rule":
        return "---"
    if node_type == "table":
        return _render_table(content, ctx)
    if node_type in ("mediaSingle", "mediaGroup"):
        # Wrappers — descend into inner media node(s) so we can resolve attrs.id
        # against the attachment list. mediaGroup may carry multiple files;
        # render each as its own placeholder line.
        parts = [
            _format_media(child, ctx)
            for child in content
            if isinstance(child, dict) and child.get("type") in ("media", "mediaInline")
        ]
        return "\n".join(parts) if parts else "[image]"
    if node_type in ("media", "mediaInline"):
        return _format_media(node, ctx)
    return f"[unsupported: {node_type}]"


def _render_list(items: list[_AdfNode], *, bullet: bool, ctx: _RenderContext) -> str:
    """Render a bulletList or orderedList.

    ``attrs.order`` on ``orderedList`` is intentionally ignored: markdown
    renderers renumber regardless, and lists beginning at a non-1 start are
    rare enough that the complexity is not worth it in v0.1.
    """
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict) or item.get("type") != "listItem":
            continue
        prefix = "- " if bullet else f"{index}. "
        item_content = item.get("content") or []
        rendered_blocks = _render_blocks(item_content, ctx)
        if not rendered_blocks:
            lines.append(prefix.rstrip())
            continue
        body = "\n\n".join(rendered_blocks)
        first, *rest = body.splitlines()
        lines.append(f"{prefix}{first}")
        for line in rest:
            lines.append(f"  {line}" if line else "")
    return "\n".join(lines)


# Upper bound for honored ``colspan``. Real Jira tables stay in single digits;
# a corrupt attr with a six-digit span must not emit a million filler cells, so
# oversized values clamp to this bound. Clamping rather than degrading to 1
# matters: a cell that shrinks to a single column drags every later cell in the
# row out of position — exactly the misalignment the carry grid exists to
# prevent. A rowspan needs no constant of its own; it lives in the carry map
# and is consumed one row at a time, materialising nothing, so the length of
# the table's own content is a sufficient bound.
_MAX_TABLE_SPAN: Final = 20


def _render_table(rows: list[_AdfNode], ctx: _RenderContext) -> str:
    """Render a ``table`` node as a GitHub-flavored pipe table.

    Header rule: the first emitted grid row becomes the GFM header iff every
    cell in it is a ``tableHeader``; otherwise a blank header row is
    synthesized and every ADF row renders as data — promoting a data row into
    the header slot would misrepresent the table's schema. ``tableHeader``
    cells outside the header row (row-scoped / first-column headers) degrade
    to ``**bold**`` cell content; cells in the real header row are not
    bolded (GFM headers already render emphasized).

    ``colspan`` / ``rowspan`` cannot be expressed in GFM, but ignoring them
    would shift later cells into the wrong columns, because ADF omits the
    covered cells entirely. A carry grid keeps every cell at its true
    column: merged content appears once, at its top-left grid position, and
    the covered slots become empty filler cells. Spans below 2, or of the
    wrong type, count as 1; oversized spans clamp rather than collapse —
    ``colspan`` to :data:`_MAX_TABLE_SPAN`, ``rowspan`` to the length of the
    table's ``content`` — an upper bound on its rows, and a rowspan can never
    outlive the table.

    Only the row that becomes the header is padded out to the widest row.
    GFM requires just the header and its delimiter to agree in cell count and
    fills short body rows in itself, whereas padding every row would make the
    emitted cell count ``rows x widest row`` — one abnormally wide row would
    then multiply across the whole table. Presentation attrs are intentionally
    ignored, same policy as ``attrs.order`` on ``orderedList``: ``layout``,
    ``width`` and ``isNumberColumnEnabled`` on the table, ``colwidth`` and
    ``background`` on cells, ``localId`` everywhere — none of them carry
    content, and markdown renderers lay tables out themselves.
    """
    grid: list[list[tuple[str, bool]]] = []
    carry: dict[int, int] = {}  # column index -> rows still covered by a rowspan
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "tableRow":
            continue
        rendered: list[tuple[str, bool]] = []
        new_spans: dict[int, int] = {}
        own_cells = 0
        covered = False
        col = 0
        for cell in row.get("content") or []:
            if not isinstance(cell, dict) or cell.get("type") not in (
                "tableHeader",
                "tableCell",
            ):
                continue
            while carry.get(col, 0) > 0:
                rendered.append(("", False))
                carry[col] -= 1
                covered = True
                col += 1
            is_header = cell.get("type") == "tableHeader"
            cell_attrs = cell.get("attrs") or {}
            colspan = _clamp_table_span(cell_attrs.get("colspan"))
            # A rowspan can never outlive the table, so the content length
            # bounds it. Non-row children inflate that bound harmlessly — it
            # only has to be finite, unlike the filler-cell ceiling a colspan
            # needs.
            rowspan = _clamp_table_span(cell_attrs.get("rowspan"), cap=len(rows))
            rendered.append((_render_table_cell(cell, ctx), is_header))
            rendered.extend(("", is_header) for _ in range(colspan - 1))
            if rowspan > 1:
                for spanned in range(col, col + colspan):
                    new_spans[spanned] = rowspan - 1
            own_cells += 1
            col += colspan
        # Consume carry that extends past the last own cell so a row fully
        # covered by a rowspan from above still emits at its true width.
        max_carry_col = max((c for c, n in carry.items() if n > 0), default=-1)
        while col <= max_carry_col:
            if carry.get(col, 0) > 0:
                carry[col] -= 1
                covered = True
            rendered.append(("", False))
            col += 1
        # New spans register only after the row — a span must not cover
        # its own origin row.
        for spanned, remaining in new_spans.items():
            carry[spanned] = remaining
        if own_cells or covered:
            grid.append(rendered)
    if not grid:
        # No emitted rows -> empty string -> the node vanishes upstream in
        # _render_blocks, same as an empty heading or blockquote. Markers
        # are for lost data; an empty table has none.
        return ""

    promote = all(is_header for _, is_header in grid[0])
    width = max(len(cells) for cells in grid)
    if promote:
        # Only the row that becomes the header needs the table's full width.
        # A synthesized header is built at ``width`` already, so an unpromoted
        # first row stays a body row and is left short like its siblings.
        grid[0] = grid[0] + [("", False)] * (width - len(grid[0]))

    def _pipe_row(cells: list[str]) -> str:
        return "| " + " | ".join(cells) + " |"

    header = [cell_text for cell_text, _ in grid[0]] if promote else [""] * width
    body = grid[1:] if promote else grid
    lines = [_pipe_row(header), _pipe_row(["---"] * width)]
    lines.extend(
        _pipe_row(
            [
                f"**{cell_text}**" if is_header and cell_text else cell_text
                for cell_text, is_header in cells
            ]
        )
        for cells in body
    )
    return "\n".join(lines)


def _render_table_cell(cell: _AdfNode, ctx: _RenderContext) -> str:
    """Flatten a table cell's block content onto one physical line.

    GFM allows only inline content in a cell, while ADF cells hold full
    blocks. Blocks render through the normal walker — so marks, media
    resolution, and ``heading_offset`` behave exactly as outside tables —
    then every line break that separates content, within and between
    blocks, degrades to ``<br>``. Lines are ``rstrip``-ed (drops the
    two-space hardBreak artifact) but keep leading indentation so flattened
    code blocks and nested lists stay readable. Whitespace-only lines are
    dropped rather than emitted as a second ``<br>`` — once a cell is a
    single physical line a run of breaks carries no information, so a blank
    line inside a flattened code block does not survive. Pipes are escaped
    last, on the whole flattened string, so the row structure survives any
    cell content.
    """
    lines = [
        stripped
        for block in _render_blocks(cell.get("content") or [], ctx)
        for line in block.splitlines()
        if (stripped := line.rstrip())
    ]
    return "<br>".join(lines).replace("|", "\\|")


def _clamp_table_span(value: Any, cap: int = _MAX_TABLE_SPAN) -> int:
    """Coerce a raw ``colspan`` / ``rowspan`` attr to a usable span.

    Anything that is not a plain ``int`` counts as 1 — including ``bool``,
    which is an ``int`` subclass and would otherwise slip ``True`` through —
    as does any value below 2. Oversized values clamp to ``cap`` rather than
    degrading to 1: a merged cell that collapses to a single column displaces
    every later cell in its row, which is the misalignment the carry grid
    exists to prevent. Callers pass the ``cap`` that suits the axis — see
    :data:`_MAX_TABLE_SPAN` for why the two differ.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 1
    return min(value, cap) if value >= 2 else 1


def _render_inline(content: list[_AdfNode], ctx: _RenderContext) -> str:
    parts: list[str] = []
    for node in content:
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        if node_type == "text":
            text = node.get("text", "")
            parts.append(_apply_marks(text, node.get("marks") or []))
        elif node_type == "hardBreak":
            parts.append("  \n")
        elif node_type == "mention":
            attrs = node.get("attrs") or {}
            text = attrs.get("text")
            if text:
                parts.append(text)
            else:
                user_id = attrs.get("id", "")
                parts.append(f"@user:{user_id}" if user_id else "@user")
        elif node_type == "emoji":
            attrs = node.get("attrs") or {}
            parts.append(attrs.get("text") or attrs.get("shortName") or "")
        elif node_type == "inlineCard":
            # Smart Links pulled into Jira show as inlineCard with the URL
            # in attrs; surfacing the URL keeps the link recoverable for the
            # LLM (and any human reading the markdown).
            attrs = node.get("attrs") or {}
            url = attrs.get("url", "")
            parts.append(url if url else "[link]")
        elif node_type in ("mediaInline", "media"):
            parts.append(_format_media(node, ctx))
        else:
            parts.append(f"[unsupported: {node_type}]")
    return "".join(parts)


def normalise_attachment_filename(filename: str) -> str:
    """Strip Jira's ``(uuid)`` duplicate-suffix from an attachment filename.

    Returns the input unchanged when the suffix isn't present. See
    :data:`_FILENAME_UUID_SUFFIX_RE` for the exact pattern and rationale.
    Exported for the Jira-client layer, which needs to compare attachment
    filenames against ADF ``attrs.alt`` strings when marking attachments
    as embedded vs orphan.
    """
    match = _FILENAME_UUID_SUFFIX_RE.match(filename)
    if match:
        return match.group(1) + match.group(2)
    return filename


def collect_media_filenames(adf: Any) -> set[str]:
    """Walk an ADF doc tree and collect every ``attrs.alt`` from media nodes.

    Used to decide which attachments on the surrounding ticket are
    "embedded" (referenced from inside the description body) vs "orphan"
    (uploaded but never mentioned). Returns an empty set when ``adf`` is
    not a dict tree — the caller doesn't need to special-case ``None``
    descriptions.
    """
    found: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") in ("media", "mediaInline"):
                alt = (node.get("attrs") or {}).get("alt")
                if alt and isinstance(alt, str):
                    found.add(alt)
            for child in node.get("content") or []:
                _walk(child)
        elif isinstance(node, list):
            for child in node:
                _walk(child)

    _walk(adf)
    return found


def _format_media(node: _AdfNode, ctx: _RenderContext) -> str:
    """Format a ``media`` / ``mediaInline`` node as a markdown placeholder.

    Tries to resolve the node to a real attachment, in this order:

    1. ``attrs.alt`` against attachment filenames — the normal path,
       because Jira fills ``alt`` with the original filename when an
       image is pasted into the description.
    2. ``attrs.id`` against Jira attachment ids — defensive, for the rare
       case where the ADF id happens to match the Jira id.

    The emitted placeholder always carries the **Jira numeric id**, which
    is what callers pass to ``get_ticket_attachment``. Unknown / missing
    media falls back to ``[image]`` so we never silently drop the marker.
    """
    attrs = node.get("attrs") or {}
    alt = attrs.get("alt")
    if alt and isinstance(alt, str):
        jira_id = ctx.attachments_by_filename.get(alt)
        if jira_id:
            return f"[attachment: {alt} (id={jira_id})]"
    media_id = attrs.get("id")
    if media_id and isinstance(media_id, str):
        filename = ctx.attachments_by_id.get(media_id)
        if filename:
            return f"[attachment: {filename} (id={media_id})]"
    return "[image]"


def _apply_marks(text: str, marks: list[_AdfNode]) -> str:
    for mark in marks:
        if not isinstance(mark, dict):
            continue
        mark_type = mark.get("type")
        if mark_type == "strong":
            text = f"**{text}**"
        elif mark_type == "em":
            text = f"*{text}*"
        elif mark_type == "code":
            text = f"`{text}`"
        elif mark_type == "strike":
            text = f"~~{text}~~"
        elif mark_type == "link":
            href = (mark.get("attrs") or {}).get("href", "")
            text = f"[{text}]({href})"
        # Unknown marks: keep underlying text, drop the decoration.
    return text
