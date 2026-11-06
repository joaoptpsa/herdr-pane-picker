# Herdr Pane Picker

Focus a Herdr pane by typing the character shown directly over it.

![Herdr Pane Picker hints over a real four-pane workspace](assets/herdr-pane-picker.png)

Hints follow the visual pane order from top to bottom and left to right, using
a home-row-first Colemak-DH alphabet: `a`, `r`, `s`, `t`, then the remaining
letters. The picker does not write into pane PTYs or disturb the programs
running inside them.

The screenshot shows the picker over a live four-pane workspace (yazi, Claude
Code, lazygit, shell). For captures without personal content, a fictional demo
layout ships in `scripts/` — see Documentation screenshots below.

## Requirements

- Linux or macOS
- Herdr 0.9.2 or newer
- Python 3.9 or newer
- A Kitty-graphics-compatible outer terminal for the image badges; other
  terminals show plain letter badges

The shipped PNG badges have no runtime Python dependencies. Pillow is only
needed to rebuild them.

## Install

```sh
herdr plugin install joaoptpsa/herdr-pane-picker
```

Try it immediately:

```sh
herdr plugin action invoke joaoptpsa.pane-picker.open
```

Bind it in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+alt+p"
type = "plugin_action"
command = "joaoptpsa.pane-picker.open"
description = "pick a pane by character hint"
```

Reload the configuration:

```sh
herdr server reload-config
```

## Controls

- Type the letter shown over a pane to focus it.
- Press Escape, Ctrl-G or Ctrl-C to cancel.
- In a zoomed tab only the zoomed pane carries a badge; the legend lists every
  pane, and picking another one unzooms the tab first.

## Hint letters

Set `HERDR_PANE_PICKER_ALPHABET` in the environment Herdr's server starts with
to change the letters and their order. It accepts each letter `a`–`z` at most
once; for example, QWERTY home row first:

```sh
export HERDR_PANE_PICKER_ALPHABET=asdfghjklqwertyuiopzxcvbnm
```

## How it works

Herdr 0.9.2 removed the pane graphics API, so no plugin can draw over another
pane any more. The picker instead opens a popup that covers the whole pane
area and paints the tab into it:

- each pane's visible text, read with `pane.read` and dimmed, at the position
  the pane has on screen, along with the dividers between panes;
- a badge centred on each pane, sent as a standard Kitty graphics image with a
  plain letter underneath, so terminals without image support still show the
  hint;
- a legend box listing each hint with the pane's name, marking the focused
  pane with `*`, and moving to the top or bottom edge when the centre would
  hide a badge.

The popup's border covers the outermost row and column of the pane area, so
text in those cells does not appear in the copy.

Errors are appended to `errors.log` in the plugin's state directory
(`$HERDR_PLUGIN_STATE_DIR`).

## Local development

```sh
git clone https://github.com/joaoptpsa/herdr-pane-picker.git
cd herdr-pane-picker
./activate-local.sh
python3 -m unittest discover -s tests -v
```

To rebuild the badge assets:

```sh
python3 -m pip install Pillow
python3 pane_picker.py build-assets
```

### Documentation screenshots

Capture screenshots only from an isolated session so no personal tabs or pane
content appear. The demo creates its own `pane-picker-demo` tab and never
replaces an existing layout; it refuses to run against the default session.

```sh
herdr --session pane-picker-demo          # in a fresh terminal window
HERDR_SESSION=pane-picker-demo python3 scripts/demo_layout.py
HERDR_SESSION=pane-picker-demo python3 pane_picker.py open
```

Any keypress inside a demo pane closes it. Remove the whole demo tab with:

```sh
HERDR_SESSION=pane-picker-demo python3 scripts/demo_layout.py --close
```

## Uninstall

```sh
herdr plugin uninstall joaoptpsa.pane-picker
```

Remove the matching `[[keys.command]]` block from the Herdr configuration.

## Marketplace

Herdr automatically indexes public GitHub repositories carrying the
`herdr-plugin` topic. See the official [plugin documentation][plugins] and
[marketplace guide][marketplace].

## License

MIT

[plugins]: https://herdr.dev/docs/plugins/
[marketplace]: https://herdr.dev/docs/marketplace/
