# log-master
Software for simplifying and automating logging of remote robot data. This project fulfills the identified requirement that different data sources are not always appropriate to store in the same logging format, or appropriate to be logged by the same process, and on the same host.

`log master` is a daemon hosting a HTTP server that allows users on the same subnet to connect via the browser and control logging from different data sources. `log master` is packaged with its own CLI client tool for interaction. The web UI is optional, but is the preferred interface for non-automated use. `log master` is intended to be launched on startup (with systemd or the like) and to always be available when the robot system is running.

Each data source has a logging server process. `log master` communicates with them via HTTP. All logging servers share a similar HTTP API --- start/stop logging, status snapshot, get/set config, etc.

The web interface provides an easy way to download logs stored on different hosts.

Log files are stored at a location on the recording server's host specified by the recording server. Log files are always named by timestamp and an additional unique hash. Timestamp and hash is their unique identification.

`log master` displays all detected logs in the web UI. Logs are detected by the `log master` by querying the recording servers about what logs they have stored at their recording path. When multiple logging servers have data belonging to the same log (same timestamp and hash), they are shown as one and the same, and it is made clear to the user what data sources have contributed to a particular log.

## Repository layout

| Path | Contents |
|---|---|
| `packages/log_master/` | The daemon (FastAPI) and the `log-master` CLI. The built web UI is bundled under `log_master/static/`. |
| `packages/logserver/` | Reference implementation of the Logging Server API for Python servers, plus `logserver-fake`. |
| `frontend/` | SvelteKit web UI (built into the Python package). |
| `spec/` | Generated, committed OpenAPI documents. `logserver.openapi.yaml` is the normative Logging Server API. |
| `system/` | systemd unit template. |
| `scripts/` | OpenAPI generator and the local development stack. |

## Development

Requirements: [uv](https://docs.astral.sh/uv/) and Node.js ≥ 20.12 (Node is only needed to build the UI, never on target hosts).

```bash
make sync        # Python workspace + frontend dependencies
make frontend    # build the UI into the Python package
make dev         # two logserver-fake instances (:8101, :8102) + log-master on http://127.0.0.1:8080
```

For UI work with hot reload, keep `make dev` running and run `npm run dev` in `frontend/` (it proxies `/api` to `:8080`).

```bash
make lint        # ruff + svelte-check
make test        # unit + integration tests (spawn fake servers and a daemon on localhost)
make test-e2e    # Playwright smoke tests at phone and desktop viewports
make spec        # regenerate spec/*.openapi.yaml and frontend/src/lib/api/schema.d.ts after API changes
```

On hosts with ROS sourced, `PYTHONPATH` breaks pytest; the Makefile targets unset it. Playwright needs a browser: `npx playwright install chromium` in `frontend/`, or `PLAYWRIGHT_CHANNEL=chrome make test-e2e` to use an installed Google Chrome.

## Deployment

```bash
make build                         # dist/log_master-*.whl (UI included) and dist/logserver-*.whl
sudo env "PATH=$PATH" make install # venv in /opt/log-master, user log-master, /etc/log-master/config.yaml, systemd unit
sudo systemctl enable --now log-master
```

`make install` copies `config.example.yaml` to `/etc/log-master/config.yaml` unless it exists; edit the source URLs there, or from the UI/CLI. The service user owns `/etc/log-master/` (the UI and CLI edit the config) and `/var/lib/log-master/` (active-recording state). `PREFIX`, `CONFIG`, `SERVICE_USER` and `PYTHON` (default `/usr/bin/python3`, must be ≥ 3.11) can be overridden on the `make` command line. The wheels are pure Python (`py3-none-any`), so they can be built on any machine and installed on another architecture; installing fetches the dependencies from PyPI.

Logging servers are installed by their own projects. Python servers can build on the `logserver` package (see `packages/logserver/README.md`); others implement `spec/logserver.openapi.yaml` directly.

## CLI

The `log-master` command talks to `--url` / `$LOG_MASTER_URL` (default `http://localhost:8080`). Every command accepts `--json`.

```bash
log-master status
log-master start [-s rover,base | --exclude base] [--duration 120] [--force | --skip-busy]
log-master stop
log-master pending dismiss base
log-master logs [--since 2026-10-17] [--source rover]
log-master logs show <log_id>
log-master download <log_id> [-s rover] [-o DIR] [--zip]   # per-file downloads resume where they stopped
log-master sources | sources add NAME URL [--no-default] | sources edit NAME [--url URL] [--default/--no-default] | sources remove NAME
log-master source base status
log-master source base config get [key] | config set key=value ...
log-master source base actions
log-master source base action survey-in --seconds 300 --accuracy-mm 20
log-master settings get | settings set poll.interval_s=0.5
log-master serve --config /etc/log-master/config.yaml
```

Exit codes: `0` ok, `1` error, `2` usage, `3` sources busy (use `--force` or `--skip-busy`), `4` log-master unreachable.

