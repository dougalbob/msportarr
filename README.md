# MotoGParr

Docker container that automates MotoGP / Moto2 / Moto3 session retrieval into a
Plex library: it watches the race calendar, searches Prowlarr, sends releases to
SABnzbd, sorts the finished download into a Plex-friendly folder structure,
applies artwork and tags, and exposes a small web UI.

**Container image:** `ghcr.io/dougalbob/motogparr`

---

## Releases and updating

Images are built and published by GitHub Actions (`.github/workflows/docker-publish.yml`).
**Nothing needs to be built locally.**

| What you do | What you get |
|---|---|
| Publish a GitHub release tagged `vX.Y.Z` | `:X.Y.Z`, `:X.Y`, `:latest` + generated release notes |
| Merge a PR to `main` | `:edge` (unreleased, for testing) |
| Run the workflow manually | tags for the ref you pick |

`:latest` only moves when a version tag is published, so a half-finished commit
on `main` never reaches a container that is force-updated.

Release notes are generated from merged pull requests by
`.github/workflows/release.yml`, categorised by label via `.github/release.yml`.
They used to be written by hand, which took an hour a release and drifted from
what the commits did. **Label your pull requests** — CI enforces this
(`.github/workflows/pr-label-check.yml` fails a PR to `main` with no labels),
so an unlabelled one still appears under "Other changes" but is caught before
merge.

Tags must be `vX.Y.Z`. `V.2.2.1` did not match the container workflow's semver
patterns and shipped images without a `:2.2` tag; the release workflow now
refuses any other format.

Releasing is a sequence, and CI enforces the order: the Unraid template's pinned
`TemplateURL` must name the tag *before* it is created, so bump it, merge, then
tag that merge commit. `release.yml` refuses a tag whose tree pins something
else, and waits for both `:X.Y.Z` and `:latest` to publish before reporting the
release as done. `scripts/check_release_readiness.py --version vX.Y.Z` runs the
same checks locally.

### One-time GHCR setup (only if a build fails with `permission_denied`)

Making the *repository* public does **not** change the visibility of a package
that already existed - they are separate settings. If the workflow fails with:

```
denied: permission_denied: read_package
```

the existing package is private and not linked to this repository, so the
workflow token cannot read it and therefore cannot push to it. Fix once:

1. Open `https://github.com/users/dougalbob/packages/container/motogparr/settings`
2. **Danger Zone → Change visibility → Public**
3. Recommended: **Manage Actions access → add this repository** with the
   `Write` role, so the workflow owns the package it publishes

Then re-run the failed job. The workflow prints these instructions itself on
that failure.

## Installing on Unraid

There is a container template at [`unraid/motogparr.xml`](unraid/motogparr.xml).
It is not in Community Applications, so add it by URL:

**Docker → Add Container**, then paste this into the **Template** field:

```
https://raw.githubusercontent.com/dougalbob/motogparr/main/unraid/motogparr.xml
```

Everything else can be left at its default until you know your own paths. The
template asks for four things that matter:

| Setting | What it is |
|---|---|
| `WebUI Port` | 5679. Open `http://<server>:5679` to finish setup. |
| `/config` | Appdata folder. Holds `settings.yml` and your calendars - keep it. |
| `/media` | Your media share, so the library can be read and written. |
| `/downloads` | Your downloads share, so completed downloads can be picked up. |

Prowlarr, SABnzbd and Plex details can be typed into the WebUI instead of the
template, which is why the template marks them optional - but if you do set them
there, they take priority over `settings.yml` (that is how the app is designed,
and it is easy to blame the wrong one when a setting "will not save").

The template's path fields ship **blank** on purpose. A non-blank default would
silently beat `settings.yml`, so editing a path in one place could be overridden
by the other. Blank means "use `settings.yml`"; fill them in only to override.

Paths are the easy thing to get wrong, and they come in two halves: the volume
mapping (`/media` in the table above) and the path *inside* it
(`library.motogp_path`). The second must exist beneath the first. Set it in
`settings.yml` and leave the template's path fields blank; see
[Configuring paths](#configuring-paths), which also covers the Plex path mapping
you will need if Plex runs as its own container.

On first start the container creates `/config/settings.yml` from a commented
template and prints which settings are still missing.

In Unraid: **Docker → your container → Force Update** after a new release.

```bash
docker pull ghcr.io/dougalbob/motogparr:latest
```

The repository is public, so once the package itself is public it pulls without
credentials.

To confirm what is actually running, the container prints its version at
startup:

```
  MotoGParr - MotoGP Automated Recording
  Version: 2.3.0
```

## Configuring paths

Environment variables are the canonical configuration, because that is what a
container launcher can set. `settings.yml` is the fallback, and the precedence is:

```
environment variable  >  settings.yml  >  built-in default
```

An environment variable that is **set but empty** counts as not set. That is
deliberate: the Unraid template ships its path fields blank, so `settings.yml`
stays authoritative and there is one place to change a path.

| Variable | Overrides |
|---|---|
| `MOTOGP_LIBRARY_PATH` | `library.motogp_path` |
| `MOTOGP_DOWNLOADS_PATH` | `library.downloads_path` |
| `PLEX_PATH_CONTAINER` | `plex.path_mapping.container` |
| `PLEX_PATH_PLEX` | `plex.path_mapping.plex` |
| `MOTOGP_PLEX_LIBRARY_ID`, `MOTOGP_PLEX_LIBRARY` | `plex.library_id`, `plex.library_name` |
| `PROWLARR_URL`, `PROWLARR_API_KEY` | `prowlarr.*` |
| `SABNZBD_URL`, `SABNZBD_API_KEY` | `sabnzbd.*` |
| `PLEX_URL`, `PLEX_TOKEN` | `plex.*` |
| `DISCORD_WEBHOOK_URL` | `discord.webhook_url` |
| `TZ` | `timezone` |
| `AUTH_ISSUER`, `AUTH_AUDIENCE`, `AUTH_ALLOWED_EMAILS`, `AUTH_CERTS_URL`, `AUTH_MODE` | not in `settings.yml` — see [Access and authentication](#access-and-authentication) |

A few variables are read by `entrypoint.sh` rather than by the Python code —
`MOTOGPARR_CALENDAR_UPDATE` and `MOTOGPARR_SKIP_PATH_CHECK`. They are listed
separately, because claiming the app reads them would be untrue.

The full list is generated from the code, so it cannot drift:

```bash
python -m utils.config_loader --env-help
```

### The Plex path mapping matters more than it looks

Matching a file in Plex is an exact string comparison against what Plex reports.
If Plex runs as its own container, it sees `/data/media/Motorsport/MotoGP/...`
while MotoGParr sees `/media/Motorsport/MotoGP/...`, and those will never be
equal. Without a mapping the tagger retries, fails, and files arrive in Plex with
no labels, genres or collections — looking like "tagging stopped working".

```yaml
plex:
  path_mapping:
    container: /media      # how MotoGParr sees it
    plex: /data/media      # how Plex sees it
```

or `PLEX_PATH_CONTAINER=/media` and `PLEX_PATH_PLEX=/data/media` on the
container. Both halves are required; one on its own does nothing.

### Startup checks

At every start the container prints each path, says **where the value came from**
(an env var set by the template beats `settings.yml`, which surprises people),
and whether the path exists:

```
  Paths:
    Library path    /media/Motorsport/MotoGP  [settings.yml, ok]
    Downloads path  /downloads/complete/motogp  [env via MOTOGP_DOWNLOADS_PATH, ok]
    Plex mapping    /media -> /data/media
```

A missing **library** path stops the container: nothing can be sorted, and the
alternative is a container that looks healthy while doing nothing for hours. A
missing **downloads** path is only a warning — that folder is often absent on a
fresh install or after a share is reorganised, and refusing to boot the WebUI
over it would remove the very thing you would use to fix it.

Override the library check with `MOTOGPARR_SKIP_PATH_CHECK=1` if you really mean
it.

### If the WebUI dies

The WebUI runs under supervision. If it exits, it is restarted (up to five times)
and the death is logged:

```
  ❌ WebUI exited (code 1) after 42s.
     Restarting it (attempt 1/5);
     the scheduler is unaffected.
```

A process that dies within seconds of starting is treated as a startup fault
rather than a crash and is **not** retried — the usual cause is port 5679 still
being held by an older container, and the log says so. The scheduler keeps
running in that case, so the container still does its job.

If the scheduler itself exits, the container stops rather than pretending to
work, so Docker's restart policy (or Unraid's) can act.

The image also declares a `HEALTHCHECK` that requests `/` on 5679, so a container
whose UI has gone reports **unhealthy** instead of "Up". Check it with:

```bash
docker inspect MotoGParr --format '{{.State.Health.Status}}'
```

## Permissions (PUID / PGID)

The container starts as root only long enough to drop privileges, then runs
everything as **`PUID`:`PGID`** — default **99:100**, which is what Unraid calls
`nobody:users`.

| Variable | Default | |
|---|---|---|
| `PUID` | `99` | User id the app runs as |
| `PGID` | `100` | Group id the app runs as |
| `UMASK` | `0022` | Permission mask for new files (`0002` if a group must share writes) |

These are runtime variables rather than a `USER` line in the image, because the
right id is per-installation and an image with a fixed uid cannot be adjusted
without a rebuild.

At startup the container checks that it can actually write to `/config`, the
library path and the downloads path, and refuses to start if it cannot — naming
the folder and the command that fixes it:

```
  Permissions (uid=99 gid=100):
    ✅ config (root) /config
    ✅ library      /media/Motorsport/MotoGP
    ❌ config (logs) /config/logs [NOT WRITABLE]

     This is almost always ownership. The container runs as 99:100, so those
     folders must be owned (or writable) by it. The paths above are inside the
     container; on the host, chown the folder you mounted at each one:

       config (logs) /config/logs (container)
           chown -R 99:100 '/mnt/user/appdata/motogparr/config/logs'   (host)

     or set PUID/PGID on the container to the owner of those folders
     (Unraid shares are normally 99:100).

     Set MOTOGPARR_HOST_MAP="/media=/mnt/user/data/media,/downloads=/mnt/user/data/downloads"
     in the container (Unraid template: "Host path map") and the hint prints
     the exact host path for each mounted folder instead of the generic form.
```

The point of failing here is that the alternative is invisible: an unwritable
folder shows up as settings that revert, downloads that never sort, or a
`PermissionError` in a subprocess hours later. `MOTOGPARR_SKIP_PATH_CHECK=1`
starts anyway.

## Access and authentication

The WebUI has no login of its own. Identity comes from **Cloudflare Zero Trust
(Access)**, and the container verifies it rather than trusting it.

```
browser ──▶ Cloudflare Access (email policy)
              │  adds Cf-Access-Jwt-Assertion
              ▼
        MotoGParr WebUI ──▶ verifies signature, issuer, audience, expiry
                             └─▶ requires the email in AUTH_ALLOWED_EMAILS
```

**Why the app checks the token itself.** Anything on your LAN, or another
container on the same Docker network, can reach port 5679 without going anywhere
near Cloudflare. A tampered header is trivial to send. So the assertion is
verified against Cloudflare's published keys: a request that did not genuinely
pass through Access is refused.

There is deliberately **no user management**. One household, one account: the
allowlist *is* the permission model, and a verified address that is not on it is
denied. No sign-up, no roster, no roles.

### Configuration

Three variables, all required together:

| Variable | Value |
|---|---|
| `AUTH_ISSUER` | `https://<your-team>.cloudflareaccess.com` (Zero Trust → Settings → Custom Pages, or the Access application) |
| `AUTH_AUDIENCE` | The Access application's **AUD tag** (Access → Applications → your app → Overview) |
| `AUTH_ALLOWED_EMAILS` | `you@example.com` (comma-separated) |

Optional:

| Variable | Default | Purpose |
|---|---|---|
| `AUTH_CERTS_URL` | `$AUTH_ISSUER/cdn-cgi/access/certs` | Override where the signing keys are fetched from |
| `AUTH_MODE` | `cloudflare` when `AUTH_ISSUER` is set | `cloudflare` forces a token even if the rest is misconfigured (fails closed); `none` disables auth, logs a warning and shows a banner in the UI |

Set **all three or none**. Two out of three is a configuration error, and the
container fails *closed*: it starts, refuses every request, and prints why. It
never falls back to open, because a forgotten variable would then silently expose
your Plex token.

Nothing is stored in `settings.yml`. An allowlist in a file the UI can rewrite is
not a security control.

### Running without Cloudflare

Leave `AUTH_ISSUER` blank and the WebUI is unauthenticated, as it was before
v2.5.0. That is the right setting for a container reachable only from your LAN,
and the startup log says so plainly:

```
  Authentication:
    ⚠️  OFF - the WebUI has no authentication.
       Anyone who can reach port 5679 can read your Plex token and
       API keys and trigger downloads.
```

The UI shows the same warning as a banner. Do not port-forward a container in
this mode.

### What stays public

`/healthz` (the Docker `HEALTHCHECK`), `/static/*`, `/assets/*`, `/flags/*`,
`/manifest.json` and `/sw.js`. A probe that needed credentials would make every
container report unhealthy, which is the failure the probe exists to catch.
Everything else — including every API route that can reveal a secret or start a
download — requires a verified identity, and a test enumerates the route table so
a new route cannot be born unauthenticated by accident.

### If you see "Not authorised"

The page names the reason. The usual ones:

| Message | Meaning |
|---|---|
| `No Cf-Access-Jwt-Assertion header` | The request did not come through Cloudflare — a LAN address, a bookmark to the IP, or another container |
| `Token audience does not match` | `AUTH_AUDIENCE` is from a different Access application |
| `Token issuer does not match` | `AUTH_ISSUER` is for a different Cloudflare team |
| `Token has expired` | Reload; Access usually re-issues |
| `not assigned to this installation` | Cloudflare let it through but the address is not in `AUTH_ALLOWED_EMAILS` |

## When the wrong release is downloaded

The matcher is a set of rules, and occasionally it accepts a release whose
contents are the wrong session. Deleting it and asking for the session again does
not help: the same rules see the same candidates and pick the same release.

**Download tab → find the session → 🚫** opens a dialog that shows:

- the release name the grabber actually selected, and its SABnzbd job id;
- **why the matcher accepted it** - every check it ran, in order, with the value
  that matched (which venue alias, which round pattern, which session it
  resolved, and the score that made it win);
- anything already blocked for that session.

Marking it does two things:

| Action | Effect |
|---|---|
| **Mark as mismatch** | The title is excluded from every future search for that session, so a replacement picks a different release. ⏹ Global scope excludes it for every session instead. |
| **Move the existing folder out of Plex** (on by default) | The wrong file is renamed into `<library>/.mismatched/<season>/`. Plex stops showing it, and a replacement sorts cleanly into the same session folder instead of being nested inside the old one. |

Nothing is deleted. The quarantined folder is hidden from Plex by the leading
dot; delete it yourself once the replacement looks right. **Unblock** on the
[mismatch log](/mismatch) reverses a wrong verdict.

The dialog also offers **Copy report**, which produces the release name and the
full check list as text — that is the material for a pattern fix, and it can be
pasted straight into an issue or a pull request. Everything is stored in
`/config/release_mismatches.json`.

> The block is per session by default because that is usually the real scope: a
> Race release misfiled as Practice is wrong *for Practice*. Use global scope for
> a title that is wrong wherever it appears, such as an off-by-one round number.

## Scheduling and uncertain submissions

During a meeting, MotoGParr searches when a wanted session reaches its end time
plus your configured search delay. Missing releases continue to be retried, and
jobs that have been sent to SABnzbd continue to be reconciled. Once every wanted
session has aired and has a video in the library, scheduled searches pause. The
scheduler still wakes periodically to notice settings, calendar or library
changes, and the download watcher continues running for manual backfills.
Home shows the scheduler's decision separately from the loop and watcher health.

Only one scheduled or manual grabber can run at a time. If another is already
running, the new request reports that it is busy; retry the manual request when
the first run has finished. The scheduler will retry on its next pass.

Submissions are reserved on disk before SABnzbd is contacted. If a container
restart or timeout leaves an uncertain result, MotoGParr tries to find the job
in SABnzbd using the unique `mgp-...` marker in its temporary download name.
The library's final names are unchanged. Recovery is attempted on startup and
periodically, including between meetings, without launching an indexer search.
If the job cannot be identified (for example history was cleared or SABnzbd is unreachable), Home shows a pending
submission and stops automatic retries for the affected sessions. **Check both
the SABnzbd queue and history before using “allow retry”**: clearing the warning
when a job was accepted can result in a duplicate. The marker may be absent if
SABnzbd renamed the job; absence alone does not prove it was never accepted.

A forced replacement also refuses an earlier job whose status is still active
or unknown. For older records without an identifiable SABnzbd job, inspect the
queue/history first; if it is safe, clear that session's grabber state using the
`clear --session` command reported in the log, then request the replacement.

## Checking your configuration

Two classes of problem are invisible in normal use, so both are reported
explicitly rather than left to be discovered.

### Unknown or misspelled settings

A key the app does not recognise is **silently ignored** — the loader falls back
to its default and behaves as if the line were never written. That is how a
typo can cost an evening, so it is now reported at startup and in the WebUI:

```
  Settings file:
    ⚠️  prowlarr.api_kye: unknown key; nothing reads it (did you mean prowlarr.api_key?)
    ⚠️  librari: unknown section; nothing reads it (did you mean library?)
    These keys are ignored by the app. Correct or remove them.
```

Check any time:

```bash
docker exec -it motogparr /opt/venv/bin/python3 -m utils.settings_schema /config/settings.yml
```

It also catches values that parse but cannot work — `required_quality: 1440p`,
`max_grabs_per_run: "22"` — and it never refuses to start over one. A warning is
not worth an outage.

### "Is Plex actually scanning this folder?"

The highest-value check in the app. If the Plex library does not include the
folder this app writes to, everything succeeds — download, sort, file on disk —
and the session **never appears in Plex**. Nothing in MotoGParr is wrong, so
nothing in its logs looks wrong.

`/diagnostics` (Settings → Admin → 🩺 Diagnostics) compares the configured
library path with what the Plex library really lists, translating through
`plex.path_mapping` first:

```
✅ Plex is scanning this folder (via /data/media/Motorsport/MotoGP).

⚠️ Plex is NOT scanning this folder. Sessions will download, sort correctly,
   and then never appear in Plex.
   App path:        /media/Motorsport/MotoGP
   As Plex sees it: /data/media/Motorsport/MotoGP
   Plex folders:    /data/media/Unrelated
```

It diagnoses the common causes specifically — a mapping that is inverted, a
`library_name` that matches no section — and says "unknown" rather than guessing
when Plex is unreachable. It never triggers a scan.

The same panel reports **where each credential came from** (`env` or
`settings.yml`). That matters because an environment variable in the container
template silently beats a key typed into the WebUI: the page says "saved", and
the app carries on using the old value.

## Calendars

Season calendars live in `config/<series>_<year>.json` and are baked into the
image. On first start they are copied into `/config`, which is the volume the
app actually reads.

There is one file per series - `motogp_2026.json` and `f1_2026.json` - rather
than one combined file. Two championships number their rounds from 1
independently, and share eight venue names in 2026, so a merged file cannot say
which "Round 10 Austria" a release belongs to. The separate prefixes also mean a
refresh of one series can never touch the other's calendar.

**Because `/config` is persistent, a new image does not by itself update a
calendar you already have.** That matters when a race is cancelled, postponed or
rescheduled. At startup the container therefore compares each calendar in
`/config` with the one in the image and either refreshes it or warns:

```
  Calendars (auto):
    = motogp_2025.json: up to date
    ^ motogp_2026.json: updated from image
        dates changed: Qatar 2026-04-12 -> 2026-11-08; 19 rounds renumbered
        previous copy saved as motogp_2026.json.bak
```

Control this with `MOTOGPARR_CALENDAR_UPDATE`:

| Value | Behaviour |
|---|---|
| `auto` (default) | Refresh the `/config` copy when it is provably the same season, keeping a `.bak`. |
| `warn` | Never write, just report differences and how to fix them. |
| `off` | Stay silent unless the files differ. |

A calendar is only replaced automatically when the set of venues matches, so a
file for a different championship is never overwritten - but note that `auto`
*will* roll back a local edit to a season calendar, which is why the `.bak` is
kept. Use `warn` if you hand-edit calendars.

Check a calendar - and the library it points at - at any time. With no
arguments every calendar of every series is checked:

```bash
docker exec -it motogparr /opt/venv/bin/python3 /app/utils/validate_calendar.py
docker exec -it motogparr /opt/venv/bin/python3 /app/utils/validate_calendar.py \
    --series motogp --season 2026 --library /media/MotoGP
```

See [docs/CALENDAR_CHANGES.md](docs/CALENDAR_CHANGES.md) for what to do when
MotoGP changes the calendar, and for the 2027 weekend format.

### Venue artwork

**MotoGP's** venue images are **relative links to two shared JPEGs** in
`assets/`, because every venue used the same placeholder. **Formula 1's are real
per-venue JPEGs** in `assets/f1_fanart/` and `assets/f1_posters/`, recompressed
to keep the repository and the image reasonable. The
sorter uses them as source images, adds the venue and session text, and writes
ordinary `poster.jpg`, `fanart.jpg` and `thumb.jpg` files into the library for
Plex. Plex does not need access to the links inside the MotoGParr container.

To add MotoGP artwork in the repository, **remove the venue's link first** and
put a real JPEG at the same path (for example,
`assets/motogp_posters/poster_thailand.jpg`). Do not edit or copy *onto* a link:
that would change the shared source image for every venue. Calendar filenames
stay the same. The unused `assets/external/2025` pre-rendered artwork is no
longer part of the image.

F1 needs no such care: its files are already real, so replacing one is an
ordinary file replace.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

CI runs the same tests plus `utils/validate_calendar.py` on every season on each
push and pull request; a pull request that edits a calendar gets a dedicated
check that also rejects duplicate JSON keys.

Run `python -m pytest tests` — not bare `pytest`, which also walks the repository
root. Root-level scratch scripts are not permitted (see `tests/test_build_hygiene.py`).

### Maintenance scripts

`scripts/cleanup_utility.py` removes random grabbed episodes from the library so
the download path can be exercised again. **It deletes media.** It is not a test,
it is not in the container image, and it refuses to run without `--confirm`:

```bash
python scripts/cleanup_utility.py --count 5 --dry-run     # preview
python scripts/cleanup_utility.py --count 5 --confirm     # delete
```

### For contributors and AI agents

| Document | What it is |
|---|---|
| [`AGENTS.md`](AGENTS.md) | **Start here.** Current status, the security/design priority list and the implementation plan. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | How the app fits together: processes, data pipeline, config precedence, the WebUI route map and the auth model. |
| [`SANDBOX.md`](SANDBOX.md) | Known agent-sandbox problems and their fixes (no Docker daemon, bare `pytest` breaks collection, …). |

`aicontextual_notes/` is historical (up to v2.0) and kept for the design
rationale only; surviving facts are folded into `docs/ARCHITECTURE.md`.
