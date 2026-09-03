# anki-mcp

Review your Anki cards by chatting with an AI agent. A small Python server exposes
MCP tools (`list_decks`, `next_card`, `reveal`, `answer`, `skip`, `bury`, `undo`,
`sync`) over Streamable HTTP, backed by the official `anki` library working on a
disposable copy of your collection that it syncs with AnkiWeb. Card images are
fetched from AnkiWeb per card and attached to tool results so the agent can see
them. It runs as a single container that scales to zero on Cloud Run.

`docs/investigation.md` has the research behind the design. The earlier Go
prototype that drove AnkiWeb's private study API lives on the `go-ankiweb`
branch; it was set aside because that API cannot skip, bury, filter, or undo.

## How it works

- **AnkiWeb is the source of truth.** On cold start the server downloads your
  collection (tens of MB, a few seconds). On the first tool call after idle it
  runs a normal sync, so a session starts from the same due list as your apps.
- **Grades sync with a lag of one.** Each grade or skip is pushed to AnkiWeb
  right before the next one, so the most recent change stays undoable. An idle
  flush after 90 s and the shutdown hook close the gap.
- **The server never uploads a full collection.** If AnkiWeb says a full sync is
  required (you edited a note type, ran Check Database, and so on) the server
  downloads and discards whatever was unsynced, which is at most one change.
- **Images go to the agent, not the screen.** `next_card` and `reveal` return
  the card text with `[image: name]` markers, followed by one image block per
  file. The model sees the images and can describe a prompt image, grade
  against an answer image, or ignore a context image. claude.ai only shows
  tool images inside the collapsed tool panel, and voice shows nothing, so on a
  walk the description is the interface.
- **Media is fetched per card.** Files come from AnkiWeb's media sync
  `downloadFiles` call (the same host key as the collection sync), are
  downscaled to 1280 px JPEGs, and cached on the container's disk. The media
  folder is never synced as a whole.
- **Two ways to set a card aside.** `skip` hides it for this session only and
  writes nothing, so it stays due on the desktop. `bury` is Anki's bury: gone
  until tomorrow on every device. Image occlusion cards are always passed over,
  since their masks cannot be rendered here; `text_only=true` also passes over
  any card with an image on the front.

## Layout

```
anki_mcp/config.py       environment → Settings; fails fast on missing secrets
anki_mcp/auth.py         single-user OAuth 2.1 (DCR + PKCE + HS256 JWTs) for claude.ai
anki_mcp/collection.py   collection layer: open, download, sync policy, queue, grade, bury, undo
anki_mcp/media.py        per-file media download from AnkiWeb, shrink, cache
anki_mcp/render.py       card HTML → text, image markers, occlusion detection
anki_mcp/server.py       FastMCP app, tools, health, entrypoint
tests/                   renderer, OAuth flow, collection layer (scratch collection, sync stubbed), media store
scripts/deploy.sh        Cloud Run deploy with Secret Manager
scripts/ankiweb_probe.py the AnkiWeb protocol probe from the investigation
```

## Secrets

| Variable | What it is |
|---|---|
| `ANKIWEB_USERNAME` | your AnkiWeb login |
| `ANKIWEB_PASSWORD` | your AnkiWeb password, used for the sync login |
| `OWNER_PASSWORD` | what you type on the consent page when claude.ai connects |
| `JWT_SIGNING_KEY` | ≥32 chars, `openssl rand -base64 48`; signs auth codes and tokens |
| `BASE_URL` | public URL of the server, no trailing slash |

Optional: `COLLECTION_DIR` (default `/tmp/anki-mcp`), `MEDIA_CACHE_DIR`
(default `<COLLECTION_DIR>/media-cache`), `IMAGE_MAX_EDGE` (default 1280),
`IDLE_SYNC_SECONDS` (default 90).

Read once at startup, never logged. Locally: copy `.env.example` to `.env`
(git-ignored). On Cloud Run: Secret Manager, wired up by `scripts/deploy.sh`.

## Run locally

```sh
uv sync
uv run pytest
set -a; source .env; set +a
COLLECTION_DIR=/tmp/anki-mcp-dev BASE_URL=http://localhost:8080 uv run python -m anki_mcp.server
curl localhost:8080/health
```

The first start downloads your collection from AnkiWeb into `COLLECTION_DIR`.

## Deploy to Cloud Run

```sh
scripts/deploy.sh <gcp-project> [region]
```

First run: creates the secrets, grants the build and secret roles, deploys with a
placeholder `BASE_URL`, prints the service URL. Put that URL in `.env` as
`BASE_URL` and run again. The service runs with `--max-instances 1` (one
collection copy, one sync client) and `--no-cpu-throttling` so the idle flush
can run between requests.

## Connect from claude.ai

Settings → Connectors → Add custom connector → `https://<your host>/mcp`.
claude.ai registers itself, sends you to the consent page, you enter
`OWNER_PASSWORD`, and the connector appears on web, desktop, and the mobile app.

## Limits

- One instance, one user. The collection is a SQLite file and the sync protocol
  assumes one client per copy.
- Undo covers the most recent skip, or the most recent grade or bury until it
  is synced.
- Image occlusion cards are never served. Other images are visible to the
  model but not, in practice, to you on the phone; an MCP Apps view would fix
  that.
- Audio is shown as a marker only.
