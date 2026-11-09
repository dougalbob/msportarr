# SANDBOX.md — agent environment notes

Problems hit while working on this repo in an agent sandbox, and the fix that
worked. Append here whenever you lose time to the environment, so the next
session does not repeat it. Keep entries short: symptom → cause → fix.

**Sandbox checked:** 2026-11-05 · Ubuntu, Python 3.11.2, no Docker daemon.

---

## 1. `docker` is not installed — and there is no daemon

```
$ docker --version
bash: docker: command not found
```

Consequences:

- **You cannot build or run the image.** Anything requiring `docker build`,
  `docker run`, `docker inspect`, or the `HEALTHCHECK` must be validated in CI
  (`ubuntu-latest` runners have Docker).
- Container-behaviour work has to be proved by *reading* `entrypoint.sh` and by
  the block-extraction tests in `tests/test_container_lifecycle.py`. That is a
  weaker proof; say so in the PR rather than implying the image was run.
- Item 3 (non-root) and item 8 (smoke test) in `AGENTS.md` are effectively
  CI-only. Write the workflow, push, and read the Actions log.

Do not attempt `apt-get install docker.io`; there is no daemon to talk to and it
wastes a couple of minutes.

---

## 2. Nothing is installed by default — make a venv

The system Python has no `flask`, no `pytest`, nothing:

```
$ python -m pytest tests -q
/usr/bin/python: No module named pytest
```

Fix once per session:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

`.venv*/` is already gitignored and excluded from snapshots, so it is safe to
create. Expect **135 passed** at v2.4.1; a different number means you changed
something. `tests/test_webui_security.py` points `CONFIG_DIR` at a temp dir at
import time, so it leaves no artefacts in the tree.

If pip is slow or a wheel build appears, note that PyPI is reachable
(`pypi.org` / `files.pythonhosted.org`). `github.com` is reachable; most other
hosts are not, so do not expect to `curl` arbitrary docs — use the code.

---

## 3. `pytest` collects root-level scratch scripts and errors out

```
$ python -m pytest --collect-only -q
ERROR test_prowlarr.py - NameError: name 'detect_race_class' is not defined
!!! Interrupted: 1 error during collection !!!
```

Bare `pytest` walks the repo root and picks up `test_prowlarr.py` (and would pick
up `test_cleanup_utility.py`), which are scratch utilities, not tests.

**Always run `python -m pytest tests -q`** (with the directory). That is also what
CI runs, so a green local run means something.

---

## 4. Importing app modules needs the repo root on `sys.path`

`tests/conftest.py` does this for the test suite. For ad-hoc scripts, either put
the script in the repo root and run it from there, or:

```bash
.venv/bin/python - <<'EOF'
import sys; sys.path.insert(0, '.')
from utils.config_loader import validate
EOF
```

Modules reference `/app`, `/config` and `/opt/venv/bin/python3` as absolute paths.
When importing out of the container, **set `CONFIG_DIR` to a scratch directory**
or you will read (and potentially write) the real `/config` paths:

```bash
CONFIG_DIR=/tmp/cfg .venv/bin/python -c "from utils.config_loader import validate; print(validate())"
```

`PYTHON_PATH = "/opt/venv/bin/python3"` in `motogp_webui.py` does not exist in the
sandbox, so anything that *spawns* the grabber will fail here. Test the pure
functions instead.

---

## 5. JWT libraries install fine (relevant to the auth work)

Both options resolve and import, so pick on merit rather than availability:

```bash
.venv/bin/pip install "PyJWT[crypto]"          # PyJWT 2.15.1 + cryptography
.venv/bin/pip install "python-jose[cryptography]"   # jose 3.5.0
```

`python-jose` is what the old design notes mentioned and it works. `PyJWT` is
lighter, actively maintained and has `jwt.PyJWKClient` with built-in JWKS
caching, which maps directly onto the "cache the Cloudflare certs, refetch on
unknown `kid`" requirement. Either is acceptable; add whichever you choose to
`requirements.txt` and say why in the PR.

Testing JWT verification offline is easy: generate an RSA keypair in the test,
serve a matching JWKS dict, monkeypatch the fetch. No network needed — which
matters, because `*.cloudflareaccess.com` is not reachable from the sandbox.

---

## 6. No network to Cloudflare, Plex, Prowlarr or SABnzbd

Outbound access is limited to GitHub/PyPI. So:

- `/test-connections` and the Plex library-root check cannot be exercised
  against a real instance. Structure the code so the HTTP layer is injectable
  (pass a session or a `fetch` callable) and test that.
- Do not "verify" a Cloudflare issuer URL by fetching it. Use a fixture.

---

## 7. Git / GitHub

- `git` and `gh` are authenticated. `gh release list`, `gh pr create` etc. work.
- `gh release view <tag>` is the fastest way to read what a release actually
  claimed — useful for writing `RELEASE_NOTES_vX.Y.Z.md` and for checking
  whether a promised behaviour was ever delivered.
- Repo size: `.git` is ~373 MB (the old artwork remains in history), while
  `assets/` is ~4 MB after issue #25 removed unused art and linked placeholders.
  **Avoid `git log -p` or anything that walks binary history without a path
  filter** — it is slow and floods context.
- Only one tag exists locally (`v2.4.1`). Older tags are on the remote; fetch if
  you need them.

---

## 8. Test hygiene: side effects

`test_cleanup_utility.py` deletes files from `MOTOGP_LIBRARY_PATH` (default
`/media/MotoGP`) and rewrites `grabber_state.json`. Never run it, and never let
`pytest` collect it — see §3. It is scheduled for removal from the image
(`AGENTS.md` §7 PR 3) precisely because of this.

---

## 9. You cannot lint the workflows here

`actionlint` is not installed, and neither route to getting it works:

- `pip install actionlint-py` fails — it tries to build a wheel that has no
  release binary for this platform.
- The GitHub release asset host (`release-assets.githubusercontent.com`) is
  **not** in the allowlist, so `curl` of the tarball fails with
  `SSL_ERROR_SYSCALL` even though `github.com` and `codeload.github.com` are
  reachable.

Consequence: workflow YAML can only be checked with `yaml.safe_load` locally
(`tests/test_workflows.py` asserts structure and the properties that have broken
before). Everything else — action expressions, `gh api` flags, shell inside
`run:` — is verified by pushing and reading the Actions log. Budget for one
failing run per workflow you touch; that is normal here, not a mistake.

Remember that PyYAML parses the bare key `on:` as the boolean `True`, so
`data["on"]` raises `KeyError` on a valid workflow.

## 10. Testing the privilege drop without Docker

There is no daemon, but the sandbox *does* have `setpriv` and `sudo`, so the
root → PUID:PGID path can be tested for real:

```bash
chmod -R a+rX /path/to/checkout     # PUID must be able to read /app
chmod -R 777 /tmp/priv/config       # ...and write /config
sudo env CONFIG_DIR=/tmp/priv/config PUID=999 PGID=1000 \
     MOTORSPORTARR_PYTHON=/path/to/venv/bin/python3 \
     MOTOGP_LIBRARY_PATH=/tmp/priv/lib \
     bash /path/to/checkout/entrypoint.sh
```

Expect `Running as: 999:1000` and `uid=999` in the permissions block. To prove
the refusal, `chmod 755` the config dir instead of 777 — it should name the
folder, print the `chown` command and exit.

Two traps that cost time: the venv must be readable by the target uid
(`chmod -R a+rX`), and `/tmp` paths must be writable by it.

## 11. Reading a failed CI run when the logs are unreachable

`gh run view --log` / `--log-failed` fail here with EOF
(`results-receiver.actions.githubusercontent.com` is blocked). But **step
annotations are reachable**, and they carry a message:

```bash
gh api repos/dougalbob/msportarr/commits/$SHA/check-runs \
  --jq '.check_runs[] | "\(.id) \(.name) \(.conclusion)"'
gh api repos/dougalbob/msportarr/check-runs/<id>/annotations
```

The annotation gives `path` + `start_line` for the failing line and whatever the
`::error::` message contained. So **put the evidence in the `::error::` text** —
uids, exit codes, the first lines of a log — instead of printing it to stdout. A
bare `exit 1` is undiagnosable from here, and each CI round trip is ~3 minutes.

Worked example of why this matters: three consecutive runs failed the same step
with only "Process completed with exit code 1". The exit code itself was the clue
that solved it — `2` meant a child process exiting 2, not a failing assertion.

## 12. Traps that make a check pass or fail for the wrong reason

All of these were hit for real while writing `container-smoke.yml`:

- **GitHub runs every `run:` as `bash -e`.** Your `set -uo pipefail` only
  *adds* flags; `-e` stays on. A helper that exits non-zero (even `exit $count`
  where the count is legitimate) kills the step before its assertions print. Do
  the work in the container, `exit 0` from there, and judge outside.
- **`mktemp -d` is `0700`.** `sudo chown -R 99:100` on it locks the runner out of
  its own directory: `ls: cannot open directory ...: Permission denied`, which
  reads like a missing file. Add `chmod 0755` after the chown.
- **`docker exec` is not the running process.** It starts a new one as the
  image's configured user, so `docker exec id -u` reports root no matter how well
  the privilege drop worked. `docker top` is worse: no `ps` in
  `python:3.11-slim`, and it prints `root` (a name) for root but numbers for
  everything else.
- **A `/proc` scan matches the shell running it**, because that shell's own
  cmdline contains the string being searched for. Filter on `argv[0]` being a
  python interpreter. Same trap with `pkill -f motogp_webui.py` in a command you
  are also typing into — it kills your own shell. Use the `motogp_web[u]i.py`
  bracket trick.
- **`entrypoint.sh` restarts the WebUI within ~10s.** Kill only the children and
  they come back, so a "nothing is running" test can silently become a pass.
- **Check that the negative case fails.** Twice a negative test passed for the
  wrong reason: the root process had already exited ("Port 5679 is in use"), and
  the killed WebUI had already been restarted. A test that cannot fail is worse
  than no test.

## 13. Long-running processes

The WebUI is a Flask dev server on `0.0.0.0:5679`. It binds fine in the sandbox
and is shown as a live preview, but it will fail at `PYTHON_PATH` for anything
that spawns a child, and it expects `/config` to exist. If you need it for
manual poking:

```bash
mkdir -p /tmp/cfg && CONFIG_DIR=/tmp/cfg .venv/bin/python motogp_webui.py
```

Use `start_process` rather than a foreground `bash` call, and bind `0.0.0.0`.
Remember it is unauthenticated — anyone with the preview URL gets `/app-config`
with secrets (there will be none in a scratch `/config`, but do not point it at
a real one).
