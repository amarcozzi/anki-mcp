# anki-mcp

Review your Anki cards by talking to an AI agent, on the phone or by voice. A
small Python server exposes MCP tools over Streamable HTTP, backed by the
official `anki` library working on a disposable copy of your collection that it
syncs with AnkiWeb. Your Mac and iPad keep using the normal Anki apps; reviews
done here reach them through AnkiWeb like any other device. Card images are
fetched from AnkiWeb per card and attached to tool results so the agent can see
them. It runs as one container that scales to zero on Cloud Run.

- `docs/claude-project.md`: connecting claude.ai and the project instructions
  that make the agent a good review partner. Start there once deployed.
- `docs/investigation.md`: the research behind the design, including the
  AnkiConnect and AnkiWeb private API routes that were tried and dropped.
- The earlier Go prototype that drove AnkiWeb's private study API lives on the
  `go-ankiweb` branch. It was set aside because that API cannot skip, bury,
  filter, or undo.

## Contents

1. [How it works](#how-it-works)
2. [Tools](#tools)
3. [Layout](#layout)
4. [Configuration and secrets](#configuration-and-secrets)
5. [Run locally](#run-locally)
6. [Deploy to Cloud Run](#deploy-to-cloud-run)
7. [Connect from claude.ai](#connect-from-claudeai)
8. [Operating it](#operating-it)
9. [Troubleshooting](#troubleshooting)
10. [Security model](#security-model)
11. [Limits and ideas](#limits-and-ideas)

## How it works

**AnkiWeb is the source of truth.** On cold start the server logs in, runs a
normal sync (which AnkiWeb answers with "full download required" for an empty
collection), then downloads the collection: about 29 MB and 2 to 3 seconds for
16,500 cards. On the first tool call after idle it syncs again, so a session
starts from the same due list as your other devices.

**Grades sync with a lag of one.** Each grade or bury is pushed to AnkiWeb right
before the next one, so the most recent change stays undoable. A background
flush after 90 seconds of inactivity and the shutdown hook close the gap, so a
grade is never more than a couple of minutes from AnkiWeb.

**The server never uploads a full collection.** If AnkiWeb says a full sync is
required (you edited a note type, ran Check Database, and so on) the server
downloads and discards whatever was unsynced, which is at most one change.
There is no code path that uploads.

**The real scheduler runs here.** The Rust backend inside the `anki` library
builds the queue exactly as the desktop does, FSRS included. Two quirks are
handled in `collection.py`: the backend only grades the card at the head of
its built queue, so the queue is rebuilt (by toggling the current deck) before
grading a card that was reached by passing over others, and undo leaves the
built queue stale, so it is rebuilt then too.

**Images go to the agent, not the screen.** `next_card` and `reveal` return the
card text with `[image: name]` markers, followed by one image block per file in
marker order. The model sees the images and can describe a prompt image, grade
against an answer image, or ignore a context image. claude.ai shows tool
images only inside the collapsed tool panel, and voice shows nothing, so on a
walk the agent's description is the interface.

**Media is fetched per file, never synced.** Files come from AnkiWeb's media
sync `downloadFiles` call using the same host key as the collection sync. They
are downscaled to 1280 px JPEGs (a 320 KB PNG becomes about 45 KB) and cached
on the container's disk. The whole media folder (1.7 GB for the author's
collection) is never synced; see the troubleshooting entry on memory for why
that matters.

**Two ways to set a card aside.** `skip` hides a card for this session only and
writes nothing, so it stays due on the desktop. `bury` is Anki's bury: gone
until tomorrow on every device. Image occlusion cards are always passed over,
since their SVG masks cannot be rendered here. With `text_only=true`, cards
with any image on the front are passed over as well.

## Tools

All tools work on the currently selected deck. Card ids are Anki's own.

| Tool | Arguments | What it does | Writes |
|---|---|---|---|
| `list_decks` | | Deck tree with new/learn/review counts; `*` marks the selected deck. | no |
| `next_card` | `deck?`, `text_only?` | Selects `deck` if given (exact name, or a unique `::suffix` such as `Mike Duncan`), then returns the next due card's front with its images. Notes how many cards were passed over. | selecting a deck |
| `reveal` | `card_id` | The back of the card with its images, plus the next interval for each rating. | no |
| `answer` | `card_id`, `rating` 1–4, `seconds_taken?` | Grades the card (1 Again, 2 Hard, 3 Good, 4 Easy) and returns the next card. | yes |
| `skip` | `card_id` | Hides the card for this session, returns the next card. | no |
| `bury` | `card_id` | Buries until tomorrow, returns the next card. | yes |
| `undo` | | Reverts the most recent skip, or the most recent grade or bury if it has not been synced yet. | yes |
| `sync` | | Pushes pending changes to AnkiWeb now. | sync |

A `next_card` result looks like this, followed by an image block:

```
card_id: 1767038130307
deck: Papers
remaining: new 0, learn 19, review 6

FRONT:
Generative Algorithms for Fusion of Physics-Based Wildfire Spread Models ...

What is the temporal resolution of GOES fire detections?

[image: paste-1597075755265376a94bfa737e5bd01674de0cd4.png]

1 of 1 image(s) attached below, in the order of the [image: ...] markers.
```

Errors come back as tool errors with a hint, for example "card N is not in the
queue any more; call next_card" after the card was graded elsewhere.

The server also sends instructions to the client at MCP initialization (the
review loop, how to treat images, when to skip). claude.ai does not reliably
surface those, which is why `docs/claude-project.md` repeats them as project
instructions.

## Layout

```
anki_mcp/config.py       environment → Settings; fails fast on missing secrets
anki_mcp/auth.py         single-user OAuth 2.1 (DCR + PKCE + HS256 JWTs) for claude.ai
anki_mcp/collection.py   collection layer: open, download, sync policy, queue, grade, bury, skip, undo
anki_mcp/media.py        per-file media download from AnkiWeb, shrink, cache
anki_mcp/render.py       card HTML → text, image markers, occlusion detection
anki_mcp/server.py       FastMCP app, tools, health, entrypoint
tests/                   renderer, OAuth flow, collection layer (scratch collection, sync stubbed), media store
scripts/deploy.sh        Cloud Run deploy with Secret Manager
scripts/mcpcall.py       call one tool from the shell, handling OAuth
scripts/ankiweb_probe.py the AnkiWeb private-API probe from the investigation (not used by the server)
docs/                    claude.ai setup, investigation notes
Dockerfile               python:3.13-slim + uv, runs `python -m anki_mcp.server`
```

## Configuration and secrets

| Variable | Required | What it is |
|---|---|---|
| `ANKIWEB_USERNAME` | yes | your AnkiWeb login |
| `ANKIWEB_PASSWORD` | yes | your AnkiWeb password, used for the sync login |
| `OWNER_PASSWORD` | yes | what you type on the consent page when claude.ai connects; any long random string |
| `JWT_SIGNING_KEY` | yes | at least 32 characters, `openssl rand -base64 48`; signs auth codes and tokens |
| `BASE_URL` | yes | public URL of the server, no trailing slash |
| `PORT` | no | listen port, default 8080 (Cloud Run sets it) |
| `COLLECTION_DIR` | no | where the collection copy lives, default `/tmp/anki-mcp` |
| `MEDIA_CACHE_DIR` | no | shrunken images, default `<COLLECTION_DIR>/media-cache` |
| `IMAGE_MAX_EDGE` | no | long side of attached images in pixels, default 1280 |
| `IDLE_SYNC_SECONDS` | no | flush an unsynced grade after this much inactivity, default 90 |
| `EXTRA_REDIRECT_URIS` | no | comma-separated OAuth redirect URIs beyond claude.ai's, for example MCP Inspector |

Secrets are read once at startup and never logged. Locally they live in `.env`
(git-ignored, mode 600); copy `.env.example` to start. On Cloud Run they live
in Secret Manager and `scripts/deploy.sh` wires them in. Never paste them into
a chat or a commit.

## Run locally

```sh
uv sync                        # Python 3.13 via uv, dependencies from uv.lock
uv run pytest                  # 17 tests, no network
uv run ruff check .
set -a; source .env; set +a
COLLECTION_DIR=/tmp/anki-mcp-dev BASE_URL=http://localhost:8080 uv run python -m anki_mcp.server
```

The first start downloads your collection into `COLLECTION_DIR`. Then, from
another shell:

```sh
curl localhost:8080/health
scripts/mcpcall.py http://localhost:8080 list_decks
scripts/mcpcall.py http://localhost:8080 next_card '{"deck": "History"}'
scripts/mcpcall.py http://localhost:8080 reveal '{"card_id": 1753239331777}'
scripts/mcpcall.py http://localhost:8080 answer '{"card_id": 1753239331777, "rating": 3}'
SAVE_IMAGES=/tmp scripts/mcpcall.py http://localhost:8080 next_card   # writes block1.jpg etc.
```

`mcpcall.py` performs the OAuth flow once per server using `OWNER_PASSWORD`
from `.env` and caches the token under `~/.cache/anki-mcp/`. The tests run
against a scratch collection with sync stubbed out, so they never touch
AnkiWeb; the collection tests exercise the real scheduler, grading, bury, skip
and undo.

## Deploy to Cloud Run

```sh
scripts/deploy.sh <gcp-project> [region]     # region defaults to us-central1
```

The script enables the APIs, creates or updates the four secrets from `.env`,
grants the compute service account the Secret Manager and Cloud Build roles,
builds the container from source, and deploys with:

```
--min-instances 0 --max-instances 1 --concurrency 4
--memory 512Mi --cpu 1 --timeout 300 --no-cpu-throttling
```

First run: deploy with a placeholder `BASE_URL`, note the printed service URL,
put it in `.env` as `BASE_URL`, run again. A source build takes 4 to 5
minutes. Configuration-only changes (memory, env vars) can be applied without a
rebuild with `gcloud run services update`.

Why those settings: one instance because the collection is a single SQLite
file and the sync protocol assumes one client per copy; CPU always on so the
idle flush thread runs between requests; 512 MiB because steady state is about
200 MB of process plus the 29 MB collection in the in-memory filesystem.

## Connect from claude.ai

Short version (details and the project instructions in `docs/claude-project.md`):

1. Settings → Connectors → Add custom connector, URL `https://<host>/mcp`.
   claude.ai detects "Always required" auth and dynamic client registration;
   keep those.
2. Connect, enter `OWNER_PASSWORD` on the consent page.
3. On the connector's page set tool permissions to Always allow, otherwise
   every call prompts.
4. Create a project with the instructions from the doc, and start review chats
   inside it. The connector and project show up in the mobile app.

## Operating it

Measured from a laptop against Cloud Run on a warm instance:

| Call | Time |
|---|---|
| next_card, reveal (text) | 0.2–0.3 s |
| reveal with an uncached image | about 1.2 s |
| answer (includes pushing the previous grade to AnkiWeb) | about 2.5 s |
| sync | about 2.3 s |
| cold start (container, login, full download) | 5–8 s, once per session |

Useful commands:

```sh
curl https://<host>/health                                   # "ok"; note: /healthz is swallowed by Google's front end
scripts/mcpcall.py https://<host> list_decks                 # end-to-end check incl. OAuth
gcloud run services describe anki-mcp --region us-central1 --project <p> \
  --format 'value(status.latestReadyRevisionName,spec.template.spec.containers[0].resources.limits.memory)'
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="anki-mcp"' \
  --project <p> --freshness 30m --limit 50 --format 'value(timestamp,textPayload)'
```

Log lines worth knowing: `sync (startup|before-mutation|idle|tool|shutdown) ok`,
`full download complete in N s`, `downloaded N/M media file(s)`,
`rss N MB after tool call`, and `authorize: issued code to <client>` for each
claude.ai connection.

Instances restart whenever Cloud Run scales to zero, which clears the
collection copy, the media cache and any session skips. Nothing is lost:
grades are flushed by the shutdown hook, and the next start downloads afresh.

## Troubleshooting

**"Memory limit of 512 MiB exceeded"** in the logs, instance restarting every
minute or so. Something is writing into `/tmp`, which is an in-memory
filesystem on Cloud Run and counts toward the limit. The known cause was the
backend's background media sync, which starts if `full_upload_or_download` is
given a server media USN; the server now passes none and aborts any media
sync. Check `du -sh /tmp/anki-mcp` behaviour locally if it recurs.

**Every call is slow and the logs show a full download each time.** The
instance is being killed between calls (see above) or `--max-instances` is
above 1.

**"card N is not in the queue any more".** The card was graded, buried or made
undue elsewhere (another device synced, or a session skip on a different
instance). Call `next_card`.

**HTTP 400 "missing original size" during a full download.** The full download
was attempted before a normal sync. A fresh collection must sync first so
AnkiWeb hands over its real endpoint; `_ensure_ready` does this.

**"AnkiWeb requires a full sync" warning.** Expected after schema changes on
another device. The server downloads and discards at most one unsynced change.

**claude.ai says the connector cannot connect.** Check `/health`, then
`/.well-known/oauth-authorization-server` on the public URL, and that
`BASE_URL` in the deployment matches the URL claude.ai uses. Reconnect from the
connector's page; tokens last 24 h and refresh for 180 days.

**Consent page rejects the password.** After several failures from one address
the server pauses that address briefly. Wait a minute and retry with the value
from `.env`.

**Build fails with "License file does not exist".** The Dockerfile copies
`LICENSE` because the package metadata references it; keep both.

## Security model

- Single user. The OAuth server issues tokens only after the owner password
  is entered on the consent page. Access tokens live 24 h, refresh tokens 180
  days, authorization codes 5 minutes; all are HS256 JWTs signed with
  `JWT_SIGNING_KEY`, so the server keeps no token state.
- Dynamic client registration is open (claude.ai requires it) but redirect
  URIs are allow-listed to claude.ai and claude.com callbacks plus
  `EXTRA_REDIRECT_URIS`. PKCE S256 is mandatory.
- Repeated wrong passwords from one address are rate limited.
- The AnkiWeb password is used only for the sync login; the resulting host key
  is kept in memory and shared with the media fetcher.
- The server can read and grade your collection and download media. It cannot
  upload a collection, delete notes, or change note content.

## Limits and ideas

- One instance, one user, one collection copy.
- Undo covers the most recent skip, or the most recent grade or bury until it
  is synced (at most until the next grade or 90 s of idle).
- Image occlusion cards are never served.
- Images are visible to the model but not, in practice, to you on the phone.
  An MCP Apps view could render them when you are looking at the screen.
- Audio is shown as a marker only.
- Voice mode with custom connectors is untested.
