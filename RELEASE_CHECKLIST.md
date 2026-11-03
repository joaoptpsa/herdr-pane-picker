---
title: "Release checklist"
date: 2026-07-18
created: "2026-07-18 Sat 00:47"
---

# Release checklist

Maintainer routine for publishing a new version. Consumers: see the README.

## Prepare

- [ ] Bump `version` in `herdr-plugin.toml`.
- [ ] Move the `[Unreleased]` changelog entries into a new `[X.Y.Z]` section
      with today's date and update the link references at the bottom.
- [ ] Update the `--ref vX.Y.Z` install command in `README.md`.
- [ ] If badge artwork changed: `python3 pane_picker.py build-assets`.

## Verify

- [ ] `python3 -m unittest discover -s tests -v`
- [ ] `python3 -m compileall -q pane_picker.py scripts tests`
- [ ] Exercise the portable popup action: select and cancel.
- [ ] Exercise the WezTerm action: select, cancel, and native `PaneSelect`
      fallback outside Herdr.
- [ ] Review every documentation image for content that should not be public
      (secrets, credentials, work data, third-party names). Personal workspace
      content is acceptable when deliberately chosen; the fictional demo layout
      (`scripts/demo_layout.py`) remains available for fully impersonal captures.
- [ ] Secret scan over tracked files:
      `git ls-files | grep -iE '\.env$|\.pem$|\.key$|(^|/)secrets|id_rsa|id_ed25519|credentials'`
      must return nothing.

## Publish

```sh
git push origin main
git tag -a vX.Y.Z -m "Herdr Pane Picker X.Y.Z"
git push origin vX.Y.Z
gh release create vX.Y.Z --title "Herdr Pane Picker X.Y.Z" --notes "<highlights>"
```

## Verify the release

- [ ] CI is green for the tagged commit.
- [ ] `herdr plugin install ugurtarlig/herdr-pane-picker --ref vX.Y.Z --yes`
      (unlink a local dev copy first: `herdr plugin unlink ugurtarlig.pane-picker`).
- [ ] `herdr plugin action invoke ugurtarlig.pane-picker.open` draws hints.
- [ ] The listing on https://herdr.dev/plugins/ shows the new version
      (index refreshes roughly every 30 minutes).
