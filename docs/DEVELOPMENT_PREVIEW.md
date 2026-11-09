# Reusing the synthetic WebUI preview

The preview fixture is designed to survive repeated development sessions without
checking mutable runtime state into Git. The reusable scenario generator is
tracked at `scripts/seed_preview.py`; it creates synthetic MotoGP and Formula 1
library folders, activity, grabber state, and settings from the calendars in
this checkout. It does not configure credentials or connect to Plex, Prowlarr,
or SABnzbd.

## Create or reuse the preview data

From the repository root:

```bash
python scripts/seed_preview.py
```

By default, the generated files live at `.preview-data/runtime/`. If the marked
preview directory already exists, the command leaves it untouched, so edits and
state accumulated in the same workspace remain available to the next session.
On a fresh checkout, it builds the same representative scenario. `.preview-data/`
is ignored by Git and Docker; only the safe generator and this guide are tracked.

The script refuses to overwrite an existing non-empty directory unless it has
this preview's marker. To deliberately recreate a marked preview (discarding
changes made inside that synthetic runtime):

```bash
python scripts/seed_preview.py --reset
```

## Run the WebUI against it

```bash
CONFIG_DIR="$PWD/.preview-data/runtime/config" \
AUTH_MODE=none .venv/bin/python motogp_webui.py
```

The application listens on port `5679`. `AUTH_MODE=none` is appropriate only
for an isolated local/sandbox preview: the WebUI has no authentication in that
mode. Never point this command at a real `/config`, library, downloads folder,
or expose it to an untrusted network.
