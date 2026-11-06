#!/usr/bin/env python3
"""WezTerm-style character hint picker for Herdr panes.

Herdr 0.9.2 removed the API that let a plugin draw over another pane, so the
picker opens a popup that covers the whole pane area and paints a dimmed copy
of every pane underneath, a hint badge over each one, and a legend box.
"""

from __future__ import annotations

import base64
import io
import itertools
import json
import os
from pathlib import Path
import shutil
import socket
import string
import sys
import termios
import time
import tty
import unicodedata
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


PLUGIN_ID = "joaoptpsa.pane-picker"
# Colemak-DH home row first, then the top and bottom rows.
DEFAULT_HINT_ALPHABET = "arstgmneioqwfpbjluyzxcdvkh"
ALPHABET_ENV = "HERDR_PANE_PICKER_ALPHABET"
BADGE_GRID_COLS = 8
BADGE_GRID_ROWS = 4
# The fallback letter sits inside the badge artwork's opaque area, so a
# terminal that renders the image hides it and one that does not shows it.
FALLBACK_BADGE_COLS = 5
# The badge artwork is authored in a fixed 64-unit coordinate space; rendered
# output is BADGE_IMAGE_SIZE so the terminal never has to upscale.
BADGE_DESIGN_SIZE = 64
BADGE_IMAGE_SIZE = 128
BADGE_SUPERSAMPLE = 4
# Kitty graphics payloads must be sent in chunks of at most 4096 bytes.
KITTY_CHUNK_SIZE = 4096
REQUEST_IDS = itertools.count(1)
SYSTEM_BADGE_FONTS = (
    "/System/Library/Fonts/SFNSRounded.ttf",
    "/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf",
)
BADGE_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "badges"
# A full-size popup covers the pane area exactly and spends one cell on its
# border, so pane-area cell (x, y) is popup cell (x - 1, y - 1).
POPUP_BORDER = 1
POPUP_SIZE = "100%"

ESC = "\x1b"
RESET = f"{ESC}[0m"
SNAPSHOT_STYLE = f"{ESC}[2;90m"
DIVIDER_STYLE = f"{ESC}[90m"
FALLBACK_BADGE_STYLE = f"{ESC}[1;97;44m"
LEGEND_STYLE = f"{ESC}[0;39;49m"
LEGEND_BORDER_STYLE = f"{ESC}[34m"
LEGEND_HINT_STYLE = f"{ESC}[1;34m"
LEGEND_DIM_STYLE = f"{ESC}[2m"
LEGEND_TITLE_STYLE = f"{ESC}[1m"

Rect = Tuple[int, int, int, int]
Request = Callable[[str, Mapping[str, Any]], Dict[str, Any]]


class HerdrApiError(RuntimeError):
    """A structured error returned by Herdr."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def herdr_socket_path() -> str:
    explicit = os.environ.get("HERDR_SOCKET_PATH")
    if explicit:
        return explicit
    session = os.environ.get("HERDR_SESSION")
    config = Path.home() / ".config" / "herdr"
    if session and session != "default":
        return str(config / "sessions" / session / "herdr.sock")
    return str(config / "herdr.sock")


def api_request(method: str, params: Mapping[str, Any]) -> Dict[str, Any]:
    """Send one newline-delimited request to Herdr's Unix socket."""

    request_id = f"pane-picker-{os.getpid()}-{next(REQUEST_IDS)}"
    payload = json.dumps(
        {"id": request_id, "method": method, "params": dict(params)},
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3.0)
            client.connect(herdr_socket_path())
            client.sendall(payload)
            with client.makefile("rb") as stream:
                line = stream.readline()
    except OSError as error:
        raise RuntimeError(f"could not reach Herdr: {error}") from error

    if not line:
        raise RuntimeError("Herdr closed the socket without a response")
    try:
        response = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Herdr returned an invalid response") from error
    if response.get("id") != request_id:
        raise RuntimeError("Herdr returned a response with the wrong request id")
    if "error" in response:
        body = response["error"]
        raise HerdrApiError(str(body.get("code", "unknown")), str(body.get("message", "")))
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Herdr returned a response without a result object")
    return result


def hint_alphabet(environ: Mapping[str, str] = os.environ) -> str:
    """The hint letters, in assignment order; badges exist for a-z only."""

    alphabet = environ.get(ALPHABET_ENV, "").strip().lower() or DEFAULT_HINT_ALPHABET
    invalid = sorted(set(alphabet) - set(string.ascii_lowercase))
    if invalid:
        raise RuntimeError(f"{ALPHABET_ENV} may only hold letters a-z, not {''.join(invalid)!r}")
    if len(set(alphabet)) != len(alphabet):
        raise RuntimeError(f"{ALPHABET_ENV} repeats a letter: {alphabet!r}")
    return alphabet


def spatial_panes(layout: Mapping[str, Any]) -> List[Dict[str, Any]]:
    panes = [dict(pane) for pane in layout.get("panes", []) if isinstance(pane, dict)]
    return sorted(
        panes,
        key=lambda pane: (
            int(pane.get("rect", {}).get("y", 0)),
            int(pane.get("rect", {}).get("x", 0)),
            str(pane.get("pane_id", "")),
        ),
    )


def assign_hints(
    layout: Mapping[str, Any],
    alphabet: str = DEFAULT_HINT_ALPHABET,
    excluded_pane_ids: Iterable[str] = (),
) -> List[Tuple[str, Dict[str, Any]]]:
    excluded = set(excluded_pane_ids)
    panes = [pane for pane in spatial_panes(layout) if pane.get("pane_id") not in excluded]
    if len(panes) > len(alphabet):
        raise RuntimeError(
            f"this tab has {len(panes)} panes, but the picker supports {len(alphabet)}"
        )
    return list(zip(alphabet, panes))


def rect_tuple(rect: Mapping[str, Any]) -> Rect:
    return (
        int(rect.get("x", 0)),
        int(rect.get("y", 0)),
        max(0, int(rect.get("width", 0))),
        max(0, int(rect.get("height", 0))),
    )


def border_style(panes: Sequence[Tuple[Rect, Optional[int]]]) -> str:
    """How Herdr frames the panes, read from the rows each one gives up.

    "boxed" frames every pane (pane_gaps). Otherwise panes share dividers, and
    "outer" adds a frame around the whole area (pane_outer_borders).
    """

    lost = [(rect, rect[3] - rows) for rect, rows in panes if rows is not None]
    if lost and all(rows >= 2 for _, rows in lost):
        return "boxed"
    if any(rect[1] == 0 and rows >= 1 for rect, rows in lost):
        return "outer"
    return "none"


def content_rect(rect: Rect, area: Rect, border: str) -> Rect:
    """Where a pane's terminal cells sit inside its layout rect.

    A shared divider belongs to the pane right of or below it, so a pane away
    from the area's left or top edge gives up that cell.
    """

    x, y, width, height = rect
    if border == "boxed":
        return (x + 1, y + 1, max(0, width - 2), max(0, height - 2))
    area_x, area_y, area_width, area_height = area
    outer = 1 if border == "outer" else 0
    left = 1 if x > area_x else outer
    top = 1 if y > area_y else outer
    right = outer if x + width >= area_x + area_width else 0
    bottom = outer if y + height >= area_y + area_height else 0
    return (x + left, y + top, max(0, width - left - right), max(0, height - top - bottom))


def to_popup(x: int, y: int) -> Tuple[int, int]:
    return x - POPUP_BORDER, y - POPUP_BORDER


def badge_rect(rect: Rect) -> Rect:
    """The badge footprint, centred on the pane and clamped to fit it."""

    x, y, width, height = rect
    cols = min(BADGE_GRID_COLS, max(1, width))
    rows = min(BADGE_GRID_ROWS, max(1, height))
    return (x + max(0, (width - cols) // 2), y + max(0, (height - rows) // 2), cols, rows)


def rects_overlap(first: Rect, second: Rect) -> bool:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def cell_width(char: str) -> int:
    if unicodedata.combining(char) or char in "‍︎️":
        return 0
    if unicodedata.east_asian_width(char) in {"W", "F"}:
        return 2
    return 1


def clip_to_width(text: str, width: int) -> str:
    """The longest prefix of text that fits in width terminal cells."""

    clipped: List[str] = []
    used = 0
    for char in text:
        if char < " " or char == "\x7f":
            char = " "
        needed = cell_width(char)
        if used + needed > width:
            break
        clipped.append(char)
        used += needed
    return "".join(clipped)


def move_to(x: int, y: int) -> str:
    """Cursor position for zero-based popup cell (x, y)."""

    return f"{ESC}[{y + 1};{x + 1}H"


class Screen:
    """Accumulates drawing for a popup of a fixed size, clipping at its edges."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.parts: List[str] = []

    def text(self, x: int, y: int, text: str, style: str = "") -> None:
        if y < 0 or y >= self.height or x >= self.width:
            return
        if x < 0:
            text = text[-x:]
            x = 0
        visible = clip_to_width(text, self.width - x)
        if visible:
            self.parts.append(f"{move_to(x, y)}{style}{visible}{RESET}")

    def raw(self, sequence: str) -> None:
        self.parts.append(sequence)

    def render(self) -> str:
        return "".join(self.parts)


def kitty_image(png: bytes, cols: int, rows: int, image_id: int) -> str:
    """Transmit and place a PNG at the cursor, scaled to cols x rows cells.

    C=1 keeps the cursor still and q=2 silences replies, so the image never
    disturbs the text layout drawn around it.
    """

    encoded = base64.b64encode(png).decode("ascii")
    chunks = [
        encoded[start : start + KITTY_CHUNK_SIZE]
        for start in range(0, len(encoded), KITTY_CHUNK_SIZE)
    ] or [""]
    sequences = []
    for index, chunk in enumerate(chunks):
        more = 1 if index < len(chunks) - 1 else 0
        if index == 0:
            control = f"a=T,f=100,t=d,q=2,C=1,i={image_id},c={cols},r={rows},m={more}"
        else:
            control = f"m={more}"
        sequences.append(f"{ESC}_G{control};{chunk}{ESC}\\")
    return "".join(sequences)


def delete_images() -> str:
    return f"{ESC}_Ga=d,d=A,q=2{ESC}\\"


def render_badge_png(char: str) -> Optional[bytes]:
    """Render the normal macOS badge through Pillow's native C-backed path."""

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    font_path = next((path for path in SYSTEM_BADGE_FONTS if Path(path).is_file()), None)
    if font_path is None:
        return None
    try:
        scale = BADGE_SUPERSAMPLE * BADGE_IMAGE_SIZE // BADGE_DESIGN_SIZE
        size = BADGE_DESIGN_SIZE * scale
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        draw.rounded_rectangle(
            (7 * scale, 8 * scale, 57 * scale, 58 * scale),
            radius=15 * scale,
            fill=(6, 10, 24, 105),
        )
        draw.rounded_rectangle(
            (5 * scale, 4 * scale, 59 * scale, 58 * scale),
            radius=17 * scale,
            fill=(202, 220, 255, 245),
        )

        surface_size = 50 * scale
        gradient = Image.new("RGBA", (surface_size, surface_size), (0, 0, 0, 0))
        gradient_draw = ImageDraw.Draw(gradient)
        top = (137, 177, 255, 255)
        bottom = (76, 112, 222, 255)
        for y in range(surface_size):
            ratio = y / max(1, surface_size - 1)
            color = tuple(
                round(top[channel] + (bottom[channel] - top[channel]) * ratio)
                for channel in range(4)
            )
            gradient_draw.line((0, y, surface_size, y), fill=color)
        surface_mask = Image.new("L", (surface_size, surface_size), 0)
        ImageDraw.Draw(surface_mask).rounded_rectangle(
            (0, 0, surface_size - 1, surface_size - 1),
            radius=15 * scale,
            fill=255,
        )
        image.paste(gradient, (7 * scale, 6 * scale), surface_mask)
        highlight = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(highlight).rounded_rectangle(
            (10 * scale, 8 * scale, 54 * scale, 23 * scale),
            radius=10 * scale,
            fill=(255, 255, 255, 24),
        )
        image = Image.alpha_composite(image, highlight)
        draw = ImageDraw.Draw(image)

        font = ImageFont.truetype(font_path, 39 * scale)
        label = char.upper()
        bounds = draw.textbbox((0, 0), label, font=font)
        glyph_width = bounds[2] - bounds[0]
        glyph_height = bounds[3] - bounds[1]
        x = (size - glyph_width) / 2 - bounds[0]
        y = (size - glyph_height) / 2 - bounds[1] - scale
        draw.text((x, y), label, font=font, fill=(250, 252, 255, 255))

        resampling = getattr(Image, "Resampling", Image).LANCZOS
        image = image.resize((BADGE_IMAGE_SIZE, BADGE_IMAGE_SIZE), resampling)
        output = io.BytesIO()
        image.save(output, format="PNG")
    except (OSError, ValueError):
        return None
    return output.getvalue()


def load_badge_png(char: str) -> Optional[bytes]:
    """Load the pre-rendered badge used by the latency-sensitive picker path."""

    try:
        payload = (BADGE_ASSET_DIR / f"{char.lower()}.png").read_bytes()
    except OSError:
        return None
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return payload


def build_badge_assets() -> int:
    """Pre-render every hint so opening the picker never initializes Pillow."""

    BADGE_ASSET_DIR.mkdir(parents=True, exist_ok=True)
    for char in string.ascii_lowercase:
        payload = render_badge_png(char)
        if payload is None:
            raise RuntimeError("Pillow and a rounded system font are required to build badges")
        (BADGE_ASSET_DIR / f"{char}.png").write_bytes(payload)
    return 0


def draw_dividers(screen: Screen, rect: Rect, content: Rect) -> None:
    """Line every edge of rect that the pane's content does not reach."""

    x, y, width, height = rect
    content_x, content_y, content_width, content_height = content
    left, top = to_popup(x, y)
    right = left + width - 1
    bottom = top + height - 1
    if content_y > y:
        screen.text(left, top, "─" * width, DIVIDER_STYLE)
    if content_y + content_height < y + height:
        screen.text(left, bottom, "─" * width, DIVIDER_STYLE)
    for row in range(top, bottom + 1):
        if content_x > x:
            screen.text(left, row, "│", DIVIDER_STYLE)
        if content_x + content_width < x + width:
            screen.text(right, row, "│", DIVIDER_STYLE)


def draw_snapshot(screen: Screen, content: Rect, lines: Sequence[str]) -> None:
    x, y, width, height = content
    popup_x, popup_y = to_popup(x, y)
    for row, line in enumerate(lines[:height]):
        screen.text(popup_x, popup_y + row, clip_to_width(line, width), SNAPSHOT_STYLE)


def draw_badge(screen: Screen, char: str, badge: Rect, image_id: int) -> None:
    x, y, cols, rows = badge
    popup_x, popup_y = to_popup(x, y)
    fallback_cols = min(FALLBACK_BADGE_COLS, cols)
    fallback = char.upper().center(fallback_cols)
    screen.text(
        popup_x + (cols - fallback_cols) // 2,
        popup_y + (rows - 1) // 2,
        fallback,
        FALLBACK_BADGE_STYLE,
    )
    png = load_badge_png(char)
    if png is None or popup_x < 0 or popup_y < 0:
        return
    screen.raw(move_to(popup_x, popup_y) + kitty_image(png, cols, rows, image_id))


def legend_lines(
    hints: Sequence[Tuple[str, str]], current_hint: Optional[str]
) -> List[Tuple[str, str, str]]:
    """Rows of (marker, hint, label) under the title, one per pane."""

    return [("*" if hint == current_hint else " ", hint, label) for hint, label in hints]


def legend_size(hints: Sequence[Tuple[str, str]], title: str, max_width: int) -> Tuple[int, int]:
    widest_row = max((6 + len(label) for _, label in hints), default=0)
    width = min(max_width, max(len(title), widest_row) + 4)
    height = len(hints) + 4
    return width, height


def legend_origin(
    size: Tuple[int, int], screen_size: Tuple[int, int], badges: Sequence[Rect]
) -> Tuple[int, int]:
    """Centre the legend unless that hides a badge; then try the top and bottom."""

    width, height = size
    screen_width, screen_height = screen_size
    x = max(0, (screen_width - width) // 2)
    candidates = [
        max(0, (screen_height - height) // 2),
        0,
        max(0, screen_height - height),
    ]
    popup_badges = [(*to_popup(bx, by), bw, bh) for bx, by, bw, bh in badges]
    for y in candidates:
        if not any(rects_overlap((x, y, width, height), badge) for badge in popup_badges):
            return x, y
    return x, candidates[0]


def draw_legend(
    screen: Screen,
    hints: Sequence[Tuple[str, str]],
    current_hint: Optional[str],
    badges: Sequence[Rect],
) -> None:
    keys = "".join(hint for hint, _ in hints)
    title = f"Pane picker  {keys}  ·  Esc/Ctrl-G cancels"
    width, height = legend_size(hints, title, screen.width)
    x, y = legend_origin((width, height), (screen.width, screen.height), badges)
    inner = width - 2

    screen.text(x, y, "╭" + "─" * inner + "╮", LEGEND_BORDER_STYLE)
    rows: List[str] = [title, ""]
    rows.extend(f"{marker} {hint}  {label}" for marker, hint, label in legend_lines(hints, current_hint))
    for offset, row in enumerate(rows, start=1):
        screen.text(x, y + offset, "│", LEGEND_BORDER_STYLE)
        screen.text(x + 1, y + offset, " " + clip_to_width(row, inner - 2).ljust(inner - 1), LEGEND_STYLE)
        screen.text(x + width - 1, y + offset, "│", LEGEND_BORDER_STYLE)
    screen.text(x, y + height - 1, "╰" + "─" * inner + "╯", LEGEND_BORDER_STYLE)

    title_keys_x = x + 2 + len("Pane picker  ")
    screen.text(x + 2, y + 1, "Pane picker", LEGEND_TITLE_STYLE)
    screen.text(title_keys_x, y + 1, keys, LEGEND_HINT_STYLE)
    for offset, (marker, hint, _) in enumerate(legend_lines(hints, current_hint), start=3):
        screen.text(x + 2, y + offset, marker, LEGEND_DIM_STYLE)
        screen.text(x + 4, y + offset, hint, LEGEND_HINT_STYLE)


def viewport_rows(info: Mapping[str, Any]) -> Optional[int]:
    scroll = info.get("scroll")
    rows = scroll.get("viewport_rows") if isinstance(scroll, dict) else None
    return rows if isinstance(rows, int) else None


def pane_display_name(info: Mapping[str, Any], pane_id: str) -> str:
    for key in ("label", "terminal_title_stripped", "cwd"):
        value = info.get(key)
        if isinstance(value, str) and value:
            return value
    return pane_id


def read_visible_lines(pane_id: str, request: Request) -> List[str]:
    try:
        result = request("pane.read", {"pane_id": pane_id, "source": "visible", "format": "text"})
    except HerdrApiError as error:
        log_error(f"could not read {pane_id}: {error.code}: {error.message}")
        return []
    read = result.get("read", {})
    text = read.get("text") if isinstance(read, dict) else None
    if not isinstance(text, str):
        text = result.get("text", "")
    return str(text).split("\n")


def compose_picker(
    layout: Mapping[str, Any],
    assignments: Sequence[Tuple[str, Mapping[str, Any]]],
    pane_info: Mapping[str, Mapping[str, Any]],
    snapshots: Mapping[str, Sequence[str]],
    screen_size: Tuple[int, int],
) -> str:
    """Every byte the popup writes to show the picker."""

    screen = Screen(*screen_size)
    screen.raw(f"{ESC}[?25l{ESC}[?7l{ESC}[2J")
    focused = layout.get("focused_pane_id")
    zoomed = bool(layout.get("zoomed"))
    area = rect_tuple(layout.get("area", {}))

    visible = [
        (char, str(pane["pane_id"]), area if zoomed else rect_tuple(pane.get("rect", {})))
        for char, pane in assignments
        if not zoomed or pane["pane_id"] == focused
    ]
    border = border_style(
        [(rect, viewport_rows(pane_info.get(pane_id, {}))) for _, pane_id, rect in visible]
    )

    badges: List[Rect] = []
    current_hint = next(
        (char for char, pane in assignments if pane["pane_id"] == focused), None
    )
    for image_id, (char, pane_id, rect) in enumerate(visible, start=1):
        content = content_rect(rect, area, border)
        draw_dividers(screen, rect, content)
        draw_snapshot(screen, content, snapshots.get(pane_id, []))
        badge = badge_rect(content)
        badges.append(badge)
        draw_badge(screen, char, badge, image_id)

    legend = [
        (char, pane_display_name(pane_info.get(str(pane["pane_id"]), {}), str(pane["pane_id"])))
        for char, pane in assignments
    ]
    draw_legend(screen, legend, current_hint, badges)
    return screen.render()


def read_selection(valid: Mapping[str, str]) -> Optional[str]:
    if not sys.stdin.isatty():
        return None
    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor)
        while True:
            raw = os.read(descriptor, 1)
            if not raw or raw in {b"\x03", b"\x07", b"\x1b"}:
                return None
            try:
                key = raw.decode("ascii").lower()
            except UnicodeDecodeError:
                continue
            if key in valid:
                return valid[key]
            sys.stdout.write("\a")
            sys.stdout.flush()
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def write_out(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def restore_popup_terminal() -> str:
    return f"{delete_images()}{ESC}[?7h{ESC}[?25h"


def show_error(message: str) -> None:
    sys.stdout.write("\x1b[2J\x1b[H\x1b[31;1mPane picker\x1b[0m\n")
    sys.stdout.write(message)
    if sys.stdin.isatty():
        sys.stdout.write(" · press any key")
        sys.stdout.flush()
        descriptor = sys.stdin.fileno()
        previous = termios.tcgetattr(descriptor)
        try:
            tty.setraw(descriptor)
            os.read(descriptor, 1)
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def state_dir() -> Path:
    configured = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if configured:
        return Path(configured)
    return Path.home() / ".config" / "herdr" / "plugin-state" / PLUGIN_ID


def log_error(message: str) -> None:
    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "errors.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}\n")
    except OSError:
        pass


def context_pane_id(environ: Mapping[str, str] = os.environ) -> Optional[str]:
    """The tiled pane under the popup, which carries no HERDR_PANE_ID itself."""

    try:
        context = json.loads(environ.get("HERDR_PLUGIN_CONTEXT_JSON", "") or "{}")
    except json.JSONDecodeError:
        return None
    pane_id = context.get("focused_pane_id") if isinstance(context, dict) else None
    return pane_id if isinstance(pane_id, str) and pane_id else None


def read_layout(request: Request, pane_id: Optional[str]) -> Dict[str, Any]:
    params = {"pane_id": pane_id} if pane_id else {}
    layout = request("pane.layout", params).get("layout")
    if not isinstance(layout, dict):
        raise RuntimeError("Herdr did not return a pane layout")
    return layout


def read_pane_info(request: Request, pane_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    wanted = set(pane_ids)
    panes = request("pane.list", {}).get("panes", [])
    return {
        str(pane["pane_id"]): dict(pane)
        for pane in panes
        if isinstance(pane, dict) and pane.get("pane_id") in wanted
    }


def focus_choice(layout: Mapping[str, Any], selected: str, request: Request) -> None:
    """Focus the chosen pane, unzooming first as tmux's select-pane does."""

    focused = layout.get("focused_pane_id")
    if layout.get("zoomed") and isinstance(focused, str) and selected != focused:
        request("pane.zoom", {"pane_id": focused, "mode": "off"})
    request("pane.focus", {"pane_id": selected})


def pick_pane(
    request: Request = api_request,
    choose: Callable[[Mapping[str, str]], Optional[str]] = read_selection,
    write: Callable[[str], None] = write_out,
    screen_size: Optional[Tuple[int, int]] = None,
    environ: Mapping[str, str] = os.environ,
) -> int:
    alphabet = hint_alphabet(environ)
    layout = read_layout(request, context_pane_id(environ))
    assignments = assign_hints(layout, alphabet)
    if len(assignments) < 2:
        show_error("This tab only has one pane.")
        return 0

    pane_ids = [str(pane["pane_id"]) for _, pane in assignments]
    pane_info = read_pane_info(request, pane_ids)
    visible_ids = [layout.get("focused_pane_id")] if layout.get("zoomed") else pane_ids
    snapshots = {
        pane_id: read_visible_lines(pane_id, request)
        for pane_id in visible_ids
        if isinstance(pane_id, str)
    }
    if screen_size is None:
        terminal = shutil.get_terminal_size()
        screen_size = (terminal.columns, terminal.lines)

    targets = {char: str(pane["pane_id"]) for char, pane in assignments}
    try:
        write(compose_picker(layout, assignments, pane_info, snapshots, screen_size))
        selected = choose(targets)
    finally:
        write(restore_popup_terminal())
    if selected:
        focus_choice(layout, selected, request)
    return 0


def open_picker(request: Request = api_request) -> int:
    request(
        "plugin.pane.open",
        {
            "plugin_id": PLUGIN_ID,
            "entrypoint": "picker",
            "placement": "popup",
            "width": POPUP_SIZE,
            "height": POPUP_SIZE,
            "focus": True,
        },
    )
    return 0


def main(argv: Sequence[str]) -> int:
    command = argv[1] if len(argv) > 1 else "pick"
    if command == "build-assets":
        try:
            return build_badge_assets()
        except RuntimeError as error:
            print(f"pane-picker: {error}", file=sys.stderr)
            return 1
    if command == "open":
        try:
            return open_picker()
        except (HerdrApiError, RuntimeError) as error:
            log_error(str(error))
            print(f"pane-picker: {error}", file=sys.stderr)
            return 1
    if command == "pick":
        try:
            return pick_pane()
        except HerdrApiError as error:
            log_error(f"{error.code}: {error.message}")
            show_error(f"Herdr error: {error.message}")
            return 1
        except RuntimeError as error:
            log_error(str(error))
            show_error(str(error))
            return 1
    print(f"usage: {Path(argv[0]).name} [build-assets|open|pick]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
