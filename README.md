# anki-mcp

Review your Anki cards by chatting with an AI agent. A small Go server exposes
four MCP tools (`list_decks`, `next_card`, `reveal`, `answer`) over Streamable
HTTP, backed by the same private API AnkiWeb's own study page uses. No local
Anki, no sync, no persistent state: the container can scale to zero.

See `docs/investigation.md` for the research behind this design and its
trade-offs. The short version: AnkiWeb's study API is undocumented and can change
without notice, and it is not something AnkiWeb's author supports. Be a polite
client.

## Layout

```
cmd/anki-mcp/          the server
cmd/ankiweb-probe/     CLI to exercise the AnkiWeb client (read-only unless -answer)
internal/ankiweb/      AnkiWeb client, reverse-engineered proto schema, HTML→text renderer
internal/auth/         single-user OAuth 2.1 server (DCR + PKCE + JWT) for claude.ai
internal/tools/        the MCP tools
internal/config/       environment → config
scripts/ankiweb_probe.py   original Python probe, kept for re-decoding the protocol
```

## Secrets

| Variable | What it is |
|---|---|
| `ANKIWEB_USERNAME` | your AnkiWeb login |
| `ANKIWEB_PASSWORD` | your AnkiWeb password (needed to re-login when the cookie expires) |
| `OWNER_PASSWORD` | what you type on the consent page when claude.ai connects |
| `JWT_SIGNING_KEY` | ≥32 chars, `openssl rand -base64 48`; signs auth codes and tokens |
| `BASE_URL` | public URL of the server, no trailing slash |

They are read from the environment once at startup and never logged. Locally,
copy `.env.example` to `.env` (git-ignored). On Cloud Run they come from Secret
Manager.

## Try the AnkiWeb client first

```sh
set -a; source .env; set +a
go run ./cmd/ankiweb-probe                 # login, list decks, show next card
go run ./cmd/ankiweb-probe -deck History   # select a deck first
go run ./cmd/ankiweb-probe -answer 3       # WRITES one review (Good) for the shown card
```

## Run locally

```sh
go test ./...
go run ./cmd/anki-mcp
curl localhost:8080/health
```

To connect claude.ai to a local instance you need a public HTTPS URL
(Cloudflare Tunnel, ngrok, Tailscale Funnel) and `BASE_URL` set to it.

## Deploy to Cloud Run

```sh
PROJECT=your-project; REGION=us-central1
for s in ankiweb-username ankiweb-password owner-password jwt-signing-key; do
  printf '%s' "$VALUE" | gcloud secrets create $s --data-file=-     # one at a time
done
gcloud run deploy anki-mcp --source . --region $REGION \
  --allow-unauthenticated --max-instances 1 --min-instances 0 \
  --set-env-vars BASE_URL=https://anki-mcp-XXXX-uc.a.run.app \
  --set-secrets ANKIWEB_USERNAME=ankiweb-username:latest,ANKIWEB_PASSWORD=ankiweb-password:latest,OWNER_PASSWORD=owner-password:latest,JWT_SIGNING_KEY=jwt-signing-key:latest
```

`--allow-unauthenticated` is required because claude.ai must reach the OAuth
endpoints; the MCP endpoint itself is protected by the bearer token. Deploy once
to learn the URL, then redeploy with `BASE_URL` set to it.

## Connect from claude.ai

Settings → Connectors → Add custom connector → URL `https://<your host>/mcp`.
claude.ai registers itself, sends you to the consent page, you enter
`OWNER_PASSWORD`, and the connector appears on web, desktop, and the mobile app.

## Regenerating the protocol bindings

```sh
go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
go generate ./internal/ankiweb/...
```

If AnkiWeb changes its API, `scripts/ankiweb_probe.py` and the notes in
`docs/investigation.md` describe how the field numbers were recovered from the
study page's JS bundle.
