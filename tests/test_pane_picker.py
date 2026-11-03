from __future__ import annotations

import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import threading
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "pane_picker.py"
SPEC = importlib.util.spec_from_file_location("pane_picker", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
pane_picker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pane_picker)

ANSI = re.compile(r"\x1b(?:\[[0-9;?]*[A-Za-z]|_G[^\x1b]*\x1b\\)")


def pane(pane_id: str, x: int, y: int, width: int = 40, height: int = 20):
    return {
        "pane_id": pane_id,
        "focused": False,
        "rect": {"x": x, "y": y, "width": width, "height": height},
    }


def info(pane_id: str, viewport_rows: int, title: str = ""):
    return {
        "pane_id": pane_id,
        "terminal_title_stripped": title,
        "scroll": {"viewport_rows": viewport_rows},
    }


def paint(output: str, width: int, height: int):
    """Replay cursor moves and text onto a grid, the way a terminal would."""

    grid = [[" "] * width for _ in range(height)]
    row = column = 0
    position = 0
    for match in ANSI.finditer(output):
        for char in output[position : match.start()]:
            if 0 <= row < height and 0 <= column < width:
                grid[row][column] = char
            column += 1
        sequence = match.group(0)
        if sequence.endswith("H") and sequence.startswith("\x1b["):
            parts = sequence[2:-1].split(";")
            row, column = int(parts[0]) - 1, int(parts[1]) - 1
        position = match.end()
    return ["".join(line) for line in grid]


class HintTests(unittest.TestCase):
    def test_assigns_colemak_home_row_hints_in_spatial_order(self):
        layout = {
            "panes": [
                pane("bottom-right", 40, 20),
                pane("top-right", 40, 0),
                pane("top-left", 0, 0),
                pane("bottom-left", 0, 20),
            ]
        }

        assignments = pane_picker.assign_hints(layout)

        self.assertEqual(
            [(hint, item["pane_id"]) for hint, item in assignments],
            [
                ("a", "top-left"),
                ("r", "top-right"),
                ("s", "bottom-left"),
                ("t", "bottom-right"),
            ],
        )

    def test_rejects_more_panes_than_hints(self):
        layout = {"panes": [pane(str(index), index, 0) for index in range(3)]}
        with self.assertRaisesRegex(RuntimeError, "supports 2"):
            pane_picker.assign_hints(layout, alphabet="as")

    def test_alphabet_override_from_environment(self):
        alphabet = pane_picker.hint_alphabet({"HERDR_PANE_PICKER_ALPHABET": "ASDF"})
        self.assertEqual(alphabet, "asdf")

    def test_alphabet_defaults_when_unset_or_blank(self):
        self.assertEqual(pane_picker.hint_alphabet({}), pane_picker.DEFAULT_HINT_ALPHABET)
        self.assertEqual(
            pane_picker.hint_alphabet({"HERDR_PANE_PICKER_ALPHABET": "  "}),
            pane_picker.DEFAULT_HINT_ALPHABET,
        )

    def test_alphabet_rejects_letters_without_badges_and_repeats(self):
        with self.assertRaisesRegex(RuntimeError, "a-z"):
            pane_picker.hint_alphabet({"HERDR_PANE_PICKER_ALPHABET": "as1"})
        with self.assertRaisesRegex(RuntimeError, "repeats"):
            pane_picker.hint_alphabet({"HERDR_PANE_PICKER_ALPHABET": "asa"})


class GeometryTests(unittest.TestCase):
    AREA = (0, 0, 94, 39)

    def test_shared_dividers_belong_to_the_right_and_lower_pane(self):
        panes = [((0, 0, 47, 39), 39), ((47, 0, 47, 20), 20), ((47, 20, 47, 19), 18)]
        border = pane_picker.border_style(panes)

        self.assertEqual(border, "none")
        self.assertEqual(pane_picker.content_rect((0, 0, 47, 39), self.AREA, border), (0, 0, 47, 39))
        self.assertEqual(pane_picker.content_rect((47, 0, 47, 20), self.AREA, border), (48, 0, 46, 20))
        self.assertEqual(pane_picker.content_rect((47, 20, 47, 19), self.AREA, border), (48, 21, 46, 18))

    def test_outer_frame_insets_only_the_area_edges(self):
        panes = [((0, 0, 47, 39), 37), ((47, 0, 47, 20), 19), ((47, 20, 47, 19), 17)]
        border = pane_picker.border_style(panes)

        self.assertEqual(border, "outer")
        self.assertEqual(pane_picker.content_rect((0, 0, 47, 39), self.AREA, border), (1, 1, 46, 37))
        self.assertEqual(pane_picker.content_rect((47, 0, 47, 20), self.AREA, border), (48, 1, 45, 19))
        self.assertEqual(pane_picker.content_rect((47, 20, 47, 19), self.AREA, border), (48, 21, 45, 17))

    def test_gaps_frame_every_pane(self):
        panes = [((0, 0, 47, 39), 37), ((47, 0, 47, 20), 18), ((47, 20, 47, 19), 17)]
        border = pane_picker.border_style(panes)

        self.assertEqual(border, "boxed")
        self.assertEqual(pane_picker.content_rect((47, 0, 47, 20), self.AREA, border), (48, 1, 45, 18))

    def test_centers_badge_and_clamps_it_to_small_panes(self):
        self.assertEqual(pane_picker.badge_rect((0, 0, 81, 31)), (36, 13, 8, 4))
        self.assertEqual(pane_picker.badge_rect((10, 5, 5, 2)), (10, 5, 5, 2))

    def test_pane_area_maps_inside_the_popup_border(self):
        self.assertEqual(pane_picker.to_popup(1, 1), (0, 0))
        self.assertEqual(pane_picker.to_popup(48, 21), (47, 20))

    def test_legend_moves_off_a_centred_badge(self):
        badges = [(41, 17, 8, 4)]
        x, y = pane_picker.legend_origin((40, 7), (91, 37), badges)
        self.assertEqual((x, y), (25, 0))

    def test_legend_stays_centred_when_clear(self):
        badges = [(10, 2, 8, 4)]
        self.assertEqual(pane_picker.legend_origin((40, 7), (91, 37), badges), (25, 15))


class TextTests(unittest.TestCase):
    def test_clips_wide_characters_by_cell_width(self):
        self.assertEqual(pane_picker.clip_to_width("日本語", 5), "日本")
        self.assertEqual(pane_picker.clip_to_width("abc", 5), "abc")

    def test_replaces_control_characters(self):
        self.assertEqual(pane_picker.clip_to_width("a\tb\x1bc", 10), "a b c")

    def test_screen_clips_text_at_its_edges(self):
        screen = pane_picker.Screen(4, 2)
        screen.text(-1, 0, "abcdef")
        screen.text(0, 5, "hidden")
        self.assertEqual(paint(screen.render(), 4, 2), ["bcde", "    "])


class KittyGraphicsTests(unittest.TestCase):
    def test_chunks_payload_and_places_it_on_the_cell_grid(self):
        png = bytes(range(256)) * 20
        output = pane_picker.kitty_image(png, 8, 4, 3)

        chunks = re.findall(r"\x1b_G([^;]*);([^\x1b]*)\x1b\\", output)
        self.assertGreater(len(chunks), 1)
        first_control = dict(item.split("=") for item in chunks[0][0].split(","))
        self.assertEqual(
            {key: first_control[key] for key in ("a", "f", "c", "r", "i", "C", "q", "m")},
            {"a": "T", "f": "100", "c": "8", "r": "4", "i": "3", "C": "1", "q": "2", "m": "1"},
        )
        self.assertEqual(chunks[-1][0], "m=0")
        self.assertTrue(all(len(data) <= pane_picker.KITTY_CHUNK_SIZE for _, data in chunks))
        self.assertEqual(base64.b64decode("".join(data for _, data in chunks)), png)

    def test_prebuilt_badges_exist_for_every_letter(self):
        for char in "abcdefghijklmnopqrstuvwxyz":
            payload = pane_picker.load_badge_png(char)
            self.assertIsNotNone(payload, char)
            self.assertTrue(payload.startswith(b"\x89PNG"))


class ComposeTests(unittest.TestCase):
    def layout(self, zoomed: bool = False):
        return {
            "area": {"x": 0, "y": 0, "width": 94, "height": 39},
            "focused_pane_id": "p1",
            "zoomed": zoomed,
            "panes": [pane("p1", 0, 0, 47, 39), pane("p2", 47, 0, 47, 20), pane("p3", 47, 20, 47, 19)],
        }

    def compose(self, layout):
        assignments = pane_picker.assign_hints(layout)
        pane_info = {
            "p1": info("p1", 39, "editor"),
            "p2": info("p2", 20, "server"),
            "p3": info("p3", 18, "shell"),
        }
        snapshots = {
            "p1": ["", "left text"],
            "p2": [""] * 10 + ["right text"],
            "p3": ["lower text"],
        }
        output = pane_picker.compose_picker(layout, assignments, pane_info, snapshots, (91, 37))
        return output, paint(output, 91, 37)

    def test_draws_snapshots_badges_and_legend_where_the_panes_are(self):
        output, grid = self.compose(self.layout())

        # The area's first row and column sit under the popup border.
        self.assertEqual(grid[0][0:8], "eft text")
        self.assertEqual(grid[9][47:57], "right text")
        self.assertEqual(grid[20][47:57], "lower text")
        self.assertEqual(grid[19][47:52], "─────")
        self.assertIn("A", grid[17][19:27])
        self.assertIn("R", grid[8][66:74])
        self.assertIn("S", grid[28][66:74])
        legend = "\n".join(grid)
        self.assertIn("* a  editor", legend)
        self.assertIn("  r  server", legend)
        self.assertEqual(output.count("a=T,"), 3)

    def test_zoomed_tab_hints_only_the_visible_pane(self):
        output, grid = self.compose(self.layout(zoomed=True))

        self.assertEqual(output.count("a=T,"), 1)
        self.assertNotIn("right text", "\n".join(grid))
        self.assertIn("  s  shell", "\n".join(grid))


class PickTests(unittest.TestCase):
    def fake_herdr(self, layout, calls):
        def request(method, params):
            calls.append((method, dict(params)))
            if method == "pane.layout":
                return {"layout": layout}
            if method == "pane.list":
                return {"panes": [info(item["pane_id"], 20) for item in layout["panes"]]}
            if method == "pane.read":
                return {"read": {"text": "hello"}}
            return {"type": "ok"}

        return request

    def run_pick(self, layout, choice):
        calls = []
        written = []
        environ = {
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"focused_pane_id": layout["focused_pane_id"]})
        }
        result = pane_picker.pick_pane(
            request=self.fake_herdr(layout, calls),
            choose=lambda targets: targets.get(choice),
            write=written.append,
            screen_size=(80, 24),
            environ=environ,
        )
        return result, calls, "".join(written)

    def layout(self, zoomed=False):
        return {
            "area": {"x": 0, "y": 0, "width": 80, "height": 24},
            "focused_pane_id": "p1",
            "zoomed": zoomed,
            "panes": [pane("p1", 0, 0, 40, 24), pane("p2", 40, 0, 40, 24)],
        }

    def test_focuses_the_chosen_pane_and_cleans_up_images(self):
        result, calls, written = self.run_pick(self.layout(), "r")

        self.assertEqual(result, 0)
        self.assertEqual(calls[0], ("pane.layout", {"pane_id": "p1"}))
        self.assertEqual(calls[-1], ("pane.focus", {"pane_id": "p2"}))
        self.assertTrue(written.endswith(pane_picker.restore_popup_terminal()))

    def test_unzooms_before_focusing_a_hidden_pane(self):
        _, calls, _ = self.run_pick(self.layout(zoomed=True), "r")

        self.assertEqual(
            calls[-2:],
            [
                ("pane.zoom", {"pane_id": "p1", "mode": "off"}),
                ("pane.focus", {"pane_id": "p2"}),
            ],
        )
        self.assertEqual([params for method, params in calls if method == "pane.read"], [
            {"pane_id": "p1", "source": "visible", "format": "text"}
        ])

    def test_cancel_focuses_nothing(self):
        _, calls, written = self.run_pick(self.layout(), None)

        self.assertNotIn("pane.focus", [method for method, _ in calls])
        self.assertTrue(written.endswith(pane_picker.restore_popup_terminal()))


class SocketClientTests(unittest.TestCase):
    def run_server(self, server_socket, response_factory):
        def serve():
            with server_socket, server_socket.makefile("rb") as stream:
                request = json.loads(stream.readline())
                response = response_factory(request)
                server_socket.sendall(json.dumps(response).encode("utf-8") + b"\n")

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return thread

    def patched_client(self, client_socket):
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.__exit__.side_effect = lambda *_: client_socket.close()
        client.settimeout.side_effect = client_socket.settimeout
        client.connect.return_value = None
        client.sendall.side_effect = client_socket.sendall
        client.makefile.side_effect = client_socket.makefile
        return mock.patch.object(pane_picker.socket, "socket", return_value=client)

    def test_round_trips_one_ndjson_request(self):
        client_socket, server_socket = socket.socketpair()

        def respond(request):
            self.assertEqual(request["method"], "pane.layout")
            return {"id": request["id"], "result": {"type": "pane_layout", "layout": {}}}

        thread = self.run_server(server_socket, respond)
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/test/herdr.sock"}):
            with self.patched_client(client_socket):
                result = pane_picker.api_request("pane.layout", {})
        thread.join(timeout=2)

        self.assertEqual(result["type"], "pane_layout")

    def test_surfaces_structured_api_error(self):
        client_socket, server_socket = socket.socketpair()

        def respond(request):
            return {
                "id": request["id"],
                "error": {"code": "unknown_method", "message": "unknown method: pane.nope"},
            }

        thread = self.run_server(server_socket, respond)
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/test/herdr.sock"}):
            with self.patched_client(client_socket):
                with self.assertRaises(pane_picker.HerdrApiError) as caught:
                    pane_picker.api_request("pane.nope", {})
        thread.join(timeout=2)

        self.assertEqual(caught.exception.code, "unknown_method")


class ActionTests(unittest.TestCase):
    def test_open_action_launches_a_full_size_popup(self):
        calls = []

        def request(method, params):
            calls.append((method, dict(params)))
            return {"type": "ok"}

        result = pane_picker.open_picker(request)

        self.assertEqual(result, 0)
        self.assertEqual(
            calls,
            [
                (
                    "plugin.pane.open",
                    {
                        "plugin_id": "ugurtarlig.pane-picker",
                        "entrypoint": "picker",
                        "placement": "popup",
                        "width": "100%",
                        "height": "100%",
                        "focus": True,
                    },
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
