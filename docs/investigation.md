# Anki-over-chat MCP server: feasibility investigation (2026-09-02)

## Verdict

Practical, and most of the hard parts already exist. The recommended design is a
**headless server built on the official `anki` Python library that syncs with AnkiWeb**,
exposed as a Streamable HTTP MCP server with OAuth, and added to claude.ai as a custom
connector (which then appears in the Claude iOS app). The Mac and iPad keep using the
GUI; they see the walk-time reviews after their next AnkiWeb sync.

Two things must be validated early because they decide the shape of the product:

1. **Voice mode + custom connectors is unverified.** Official docs say voice mode can use
   "tools you've connected" (Gmail, Calendar, Slack are named). Custom MCP connectors are
   not mentioned, and an April 2026 report said they did not work in voice. Test with a
   stub server before building anything.
2. **Image cards cannot be shown in chat.** MCP image blocks are only rendered inside the
   collapsed tool-use panel on claude.ai, and not at all in voice. Measured on the live
   collection: 12% of due cards have image-only fronts (302 of them are Image Occlusion).

## What was verified on this Mac

| Item | Result |
|---|---|
| Anki desktop | 26.8.1, running, AnkiConnect v6 responding on :8765 |
| Due cards (unsuspended) | 2,467 |
| Text-only front | 1,662 (67%) — 426 of these have an image on the back |
| Image + text front | 497 (20%) |
| Image-only front | 308 (12%), 302 are Image Occlusion Enhanced |
| MathJax in Q or A | 308 |
| Cloze | 659 |
| Headless `anki` pylib 26.08.1 | Opened a copy of the collection with no GUI, returned the queue in scheduler order with FSRS on, exposes `sync_login`, `sync_collection`, `sync_media`, `full_upload_or_download`, `answer_card`, `describe_next_states` |

Per top-level deck, due cards that are chat-friendly (text front):
History 172/179, Computer Science 19/19, Music 54/67, Forestry 35/41, Knowledge 805/1099,
Fire Science 369/574, Machine Learning 120/167, Ultimate Geography 64/131, Papers 15/61,
Great Works of Art 0/119.

## Three architectures

### A. Mac + AnkiConnect + tunnel (prototype only)
Anki must be open and the Mac awake. AnkiConnect has `answerCards` (V3/FSRS correct,
writes revlog) but no "next card in scheduler order" action, and it starts the timer
immediately before answering so every review records ~0 ms. Existing project
`ankimcp/anki-mcp-server` (462 stars, Streamable HTTP, `get_due_cards`/`present_card`/
`rate_card`) covers this route already and is the fastest way to test the client side.

### B. Headless pylib server in the cloud (recommended)
`pip install anki`, one collection file, `sync_login` once and store the hkey, then per
session: `sync_collection` → select deck → `get_queued_cards(1)` → show
`card.question()` → `card.start_timer()` → reveal `card.answer()` →
`build_answer`/`answer_card` → `sync_collection` at session end and every N cards.
No Qt, no VNC, ~200 MB container. `raslab/anki-sync-mcp` is a 0-star existing project
in exactly this shape (answering is gated behind a flag).

### C. Anki desktop in Docker with AnkiConnect
`ankimcp/headless-anki` image. Works but needs a virtual display, VNC login to AnkiWeb by
hand, has memory-leak reports, and inherits AnkiConnect's limitations. Not worth it.

## Sync model and its risks

- AnkiWeb has no public API (Damien Elmes, 2022). The sync protocol is undocumented but
  using pylib means you *are* the official client, so compatibility tracks the `anki`
  package version. Keep the server's pylib version >= the desktop version.
- Normal review answering is an incremental sync; USN-based merge handles the Mac and the
  server both reviewing on the same day, as long as every device syncs.
- `sync_collection` can return `FULL_SYNC` (note-type edits, "Check Database", some deck
  option changes on the Mac). The server must handle it explicitly, and the policy should
  be **server always downloads, never uploads**. Any unsynced server reviews are lost in
  that case, so sync frequently.
- Media does not need to sync for text-only reviews. Optional: `sync_media` once (1.7 GB)
  so the server can send images to the model for the image-on-back cards.

## MCP tool surface (draft)

- `list_decks()` → due/new/learn counts per deck (from the synced collection).
- `start_session(deck)` → syncs, selects deck, returns first card front + counts.
- `reveal(card_id)` → back side, with next-interval labels for Again/Hard/Good/Easy.
- `answer(card_id, rating)` → `answer_card`, returns next card front. Rejects if the
  card is not the one currently presented (prevents stale state).
- `skip(card_id)` / `bury(card_id)` → for image cards or anything unreviewable by chat.
- `undo()` → `col.undo()` for mis-grades.
- `end_session()` → sync.

Card text rendering: strip template CSS/HTML, keep MathJax as LaTeX, render cloze deletions
as `[...]` on the front, replace images with `[image]` so the agent knows to skip or
describe. Existing projects do none of this well; it is where most of the UX lives.

Agent behaviour goes in the tool descriptions and a server-side prompt: present front,
wait for the user's answer, reveal, compare, *propose* a grade and let the user confirm
(or an explicit "auto-grade" mode for voice).

## Client side: claude.ai custom connectors

- Any plan can add a custom connector. Add it on web/desktop; it syncs to the iOS app.
- Transport: Streamable HTTP on a public HTTPS IPv4 hostname. Anthropic's servers call it,
  not the phone.
- Auth: OAuth 2.1 with dynamic client registration + PKCE, or no auth. Static bearer
  headers are a limited beta. Callback URL `https://claude.ai/api/mcp/auth_callback`.
- Simplest auth that works: Python FastMCP + `fastmcp-personal-auth` (single-user OAuth
  provider built for this exact case) or FastMCP `OAuthProxy` with Google/GitHub login and
  an owner-email check.
- Exposure: Tailscale Funnel (free, zero config) or Cloudflare Tunnel + own domain.
  Or host on Fly.io / a small VPS for ~$0–5/month so nothing depends on home hardware.
- Make the server stateless / tolerant of unknown session IDs: the mobile app has a
  known session-recovery bug after server restarts.
- Limits: 300 s tool timeout, ~150k-char results. Not a concern here.
- ChatGPT mobile does not support custom MCP servers (web only, read-only on Pro). A
  self-built Telegram bot is the fallback if voice via Claude does not pan out.

## Limitations summary

1. Image-front cards are unreviewable in chat/voice (12% of due; 20% more degrade).
2. Voice + custom connector support unconfirmed; text chat on the phone definitely works.
3. Review throughput will be lower than tapping in the GUI; expect 20–40 cards per walk.
   Upside: the agent can explain, quiz around the card, and grade free-form answers.
4. AnkiWeb credentials (or the hkey) live on the server; treat the box as sensitive.
5. Full-sync events on the Mac silently invalidate unsynced server reviews.
6. Version coupling between pylib and AnkiWeb's supported sync protocol range.

## Suggested order of work

1. **Half a day:** run `ankimcp/anki-mcp-server` (or a 30-line FastMCP stub) on the Mac,
   expose via Tailscale Funnel with `fastmcp-personal-auth`, add to claude.ai, test on the
   iPhone in text and in voice. This answers the single biggest unknown.
2. **Weekend:** build the headless pylib server (FastMCP, Docker), card-text renderer,
   sync policy, the tool surface above.
3. Deploy to Fly.io or a VPS, add a health check that syncs nightly so full-sync
   requirements are noticed early.

## Addendum (2026-09-02): AnkiWeb's private study API

Reverse-engineered from the study page's JS bundle (SvelteKit app, protobuf-es over
`application/octet-stream`). Probe script: `scripts/ankiweb_probe.py`. Login endpoint
verified live with a bogus account (returns `INVALID_USER`); the study calls are decoded
from the JS but not yet exercised against a real account.

Flow the browser uses:

1. `POST https://ankiweb.net/svc/account/login` body `{1: username, 2: password}` →
   `{1: status enum (1 = AUTHENTICATED), 2: token}` and a session cookie on ankiweb.net.
2. `GET https://ankiuser.net/account/ankiuser-login?t=<token>` hands the session to the
   ankiuser.net domain, where study and edit pages live.
3. `POST https://ankiweb.net/svc/decks/deck-list-info` `{1: minutes_west_of_utc}` →
   deck tree with per-deck new/learn/review counts and `current_deck_id`.
   `POST /svc/decks/select-deck` `{1: deck_id}`.
4. `POST https://ankiuser.net/svc/study/study-cards` with `StudyCardsRequest`
   `{1: answer?, 2: next_card_id?}` → `{1: sched_ver, 2: cards[], 3: new, 4: learn, 5: review}`.
   Each card: `{1: card_id, 2: question html, 3: answer html, 4: count_index,
   5: button_labels[], 6: note_id, 7: template_index, 8: next_states}`.
   `next_states = {1: current, 2: again, 3: hard, 4: good, 5: easy}` (SchedulingState).
   An empty request just fetches the next two cards. To grade, send
   `answer = {1: card_id, 2: button 1–4, 3: time_taken_ms, 4: answered_at_ms,
   5: current_state, 6: chosen next_state}`; the scheduler runs on AnkiWeb and the
   response carries the following cards. States can be round-tripped as opaque bytes.
5. Media is served per file at `https://ankiuser.net/study/media/<filename>`.
6. The page also has `/svc/editor/get-note-info`, `/svc/search/search`,
   `/svc/study/get-deck-limits`, `/svc/study/set-deck-limits`.

What this buys: no collection download, no sync, no scheduler code, on-demand images,
a truly stateless container. What it costs: unsupported private API that can change
without notice, must store the AnkiWeb password for re-login when the cookie expires,
unknown rate limits and cookie lifetime, and it is exactly the automated use of AnkiWeb
its author has said he does not want to support.
