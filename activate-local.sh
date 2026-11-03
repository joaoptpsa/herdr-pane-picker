#!/bin/sh
set -eu

plugin_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
config_root=${XDG_CONFIG_HOME:-"$HOME/.config"}
config_path=${HERDR_CONFIG_PATH:-"$config_root/herdr/config.toml"}
herdr_bin=${HERDR_BIN_PATH:-$(command -v herdr)}
plugin_id="ugurtarlig.pane-picker"

python3 - "$config_path" <<'PY'
from datetime import datetime
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

config_path = Path(sys.argv[1]).expanduser().resolve()
text = config_path.read_text(encoding="utf-8")
original = text

plugin_action_commands = {
    'command = "ugurtarlig.pane-picker.open"',
    'command = "ugurtarlig.pane-picker.show"',
}
binding_blocks = list(
    re.finditer(
        r"(?ms)^\[\[keys\.command\]\][ \t]*\n.*?(?=^\[\[keys\.command\]\]|^\[[^[]|\Z)",
        text,
    )
)
picker_block = next(
    (
        match
        for match in binding_blocks
        if any(command in match.group(0) for command in plugin_action_commands)
        or "pane_picker.py" in match.group(0)
        and (" pick" in match.group(0) or " show" in match.group(0))
    ),
    None,
)
binding_body = (
    "[[keys.command]]\n"
    'key = "prefix+alt+p"\n'
    'type = "plugin_action"\n'
    'command = "ugurtarlig.pane-picker.open"\n'
    'description = "pick a pane by character hint"\n'
    "\n"
)
if picker_block is not None:
    text = text[: picker_block.start()] + binding_body + text[picker_block.end() :]
else:
    binding = (
        "# Pane picker: type the letter shown over a pane to focus it.\n"
        + binding_body
    )
    ui_match = re.search(r"(?m)^\[ui\][ \t]*$", text)
    insertion = ui_match.start() if ui_match else len(text)
    text = text[:insertion] + binding + text[insertion:]

if text == original:
    print(f"config already active: {config_path}")
    raise SystemExit(0)

stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
backup_path = config_path.with_name(f"{config_path.name}.pane-picker-backup-{stamp}")
shutil.copy2(config_path, backup_path)

descriptor, temporary_name = tempfile.mkstemp(
    prefix=f".{config_path.name}.pane-picker-", dir=config_path.parent
)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary_name, config_path)
finally:
    try:
        os.unlink(temporary_name)
    except FileNotFoundError:
        pass

print(f"updated config: {config_path}")
print(f"backup: {backup_path}")
PY

"$herdr_bin" config check

"$herdr_bin" plugin link "$plugin_root"

"$herdr_bin" server reload-config
"$herdr_bin" plugin action list --plugin "$plugin_id"

echo "pane picker active: press prefix+alt+p"
