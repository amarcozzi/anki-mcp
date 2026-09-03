"""FastMCP application: OAuth routes, health, and the review tools."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image
from mcp.types import TextContent
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .auth import OAuthServer
from .collection import RATING_NAMES, AnkiSession, CardView, NotDue
from .config import Settings
from .media import MediaStore

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "You are helping the user review Anki flashcards by conversation, often by voice on a walk, so they "
    "usually cannot see anything you are sent. Call next_card, show or read out the FRONT and wait for the "
    "user's answer. Then call reveal, compare their answer with the BACK, and propose a rating (1 Again, "
    "2 Hard, 3 Good, 4 Easy). Grade with answer once the user confirms, or immediately if they asked you to "
    "auto-grade. Keep it brisk: one card per exchange. Never invent card content.\n\n"
    "Images: cards may include images, attached to the tool result after the text in the order of the "
    "[image: ...] markers. You can see them; the user often cannot. When an image is the prompt, describe "
    "what it shows precisely enough for the user to answer, without giving the answer away. When an image is "
    "the answer, describe it and grade the user's answer against it. When it is just context (a paper "
    "header, a figure) mention it only if it helps. If a card does not work without seeing the image, "
    "offer skip (hidden for this session, still due on the desktop) or bury (until tomorrow)."
)


def build(settings: Settings) -> tuple[FastMCP, AnkiSession]:
    media = MediaStore(settings.media_cache_dir, max_edge=settings.image_max_edge)
    session = AnkiSession(
        settings.collection_dir,
        settings.ankiweb_username,
        settings.ankiweb_password,
        settings.idle_sync_seconds,
        media=media,
    )
    oauth = OAuthServer(
        base_url=settings.base_url,
        owner_password=settings.owner_password,
        signing_key=settings.jwt_signing_key.encode(),
        extra_redirects=settings.extra_redirect_uris,
    )

    @asynccontextmanager
    async def lifespan(_: FastMCP):
        # Warm up in the background so the port opens immediately; the first tool
        # call waits on the lock if the download is still running.
        threading.Thread(target=_warm, args=(session,), name="warm-up", daemon=True).start()
        try:
            yield
        finally:
            session.close()

    mcp = FastMCP(
        "anki-mcp", instructions=INSTRUCTIONS, version="0.3.0", auth=oauth.auth_provider(), lifespan=lifespan
    )
    oauth.register_routes(mcp)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request):
        return PlainTextResponse("ok")

    @mcp.custom_route("/", methods=["GET"])
    async def root(_: Request):
        return PlainTextResponse("anki-mcp: connect an MCP client to /mcp\n")

    def attach(lines: list[str], names: list[str]) -> ToolResult:
        """Text block first, then one image block per fetched file, in marker order."""
        fetched = session.images(names)
        blocks: list = []
        missing = [n for n in names if fetched.get(n) is None]
        if names:
            got = len(names) - len(missing)
            lines.append(
                f"\n{got} of {len(names)} image(s) attached below, in the order of the [image: ...] markers."
            )
            if missing:
                lines.append("Could not load: " + ", ".join(missing))
        blocks.append(TextContent(type="text", text="\n".join(lines)))
        for n in names:
            data = fetched.get(n)
            if data is not None:
                blocks.append(Image(data=data, format="jpeg").to_image_content())
        return ToolResult(content=blocks)

    def present(card: CardView | None, prefix: str = "") -> ToolResult:
        if card is None:
            text = prefix + "No more reviewable cards due in this deck. Call list_decks to pick another deck."
            return ToolResult(content=[TextContent(type="text", text=text)])
        lines = [
            f"card_id: {card.card_id}",
            f"deck: {card.deck}",
            f"remaining: new {card.new}, learn {card.learn}, review {card.review}",
        ]
        if card.passed_occlusion:
            lines.append(
                f"(passed over {card.passed_occlusion} image occlusion card(s); they stay due for the desktop)"
            )
        if card.passed_over:
            lines.append(
                f"(passed over {card.passed_over} card(s) with image fronts; they stay due for the desktop)"
            )
        lines += ["", "FRONT:", card.front]
        if prefix:
            lines.insert(0, prefix)
        return attach(lines, card.front_images)

    @mcp.tool
    def list_decks() -> str:
        """List Anki decks with due counts (new, learning, review). '*' marks the selected deck."""
        rows = []
        for d in session.decks():
            mark = "*" if d.current else " "
            rows.append(f"{mark} {'  ' * d.level}{d.name}  (new {d.new}, learn {d.learn}, review {d.review})")
        return "\n".join(rows) + "\n\n* = currently selected deck"

    @mcp.tool
    def next_card(deck: str | None = None, text_only: bool = False) -> ToolResult:
        """Get the next due card's FRONT from the current deck, or select a deck by name first.

        Images on the front are attached after the text. Image occlusion cards are
        always passed over. With text_only, cards with any image on the front are
        passed over too. Show the front, let the user answer, then call reveal.
        """
        if deck:
            try:
                session.select_deck(deck)
            except KeyError:
                raise ToolError(f"no deck named {deck!r}; call list_decks") from None
        return present(session.next_card(text_only=text_only))

    @mcp.tool
    def reveal(card_id: int) -> ToolResult:
        """Reveal the BACK of a card returned by next_card, with the next interval for each rating.

        Images on the back are attached after the text. Compare the user's answer
        with the back and propose a rating; let the user confirm unless they asked
        you to auto-grade.
        """
        try:
            b = session.reveal(card_id)
        except NotDue:
            raise ToolError(f"card {card_id} is not in the queue any more; call next_card") from None
        lines = [f"card_id: {card_id}", "", "BACK:", b.back, "", "Ratings:"]
        for i, label in enumerate(b.labels, start=1):
            lines.append(f"  {i} = {RATING_NAMES[i]:<5} (next: {_clean(label)})")
        return attach(lines, b.back_images)

    @mcp.tool
    def answer(card_id: int, rating: int, seconds_taken: int = 15) -> ToolResult:
        """Grade a card: rating 1 Again, 2 Hard, 3 Good, 4 Easy. Then returns the next card's front.

        This writes a review to the user's Anki collection. The most recent grade can
        be reverted with undo until the next grade or bury.
        """
        if rating not in RATING_NAMES:
            raise ToolError("rating must be 1-4")
        try:
            name = session.answer(card_id, rating, seconds_taken)
        except NotDue:
            raise ToolError(
                f"card {card_id} is not in the queue any more; nothing graded. Call next_card"
            ) from None
        return present(session.next_card(), prefix=f"Graded card {card_id} as {name}.\n")

    @mcp.tool
    def skip(card_id: int) -> ToolResult:
        """Set a card aside for this session without grading it, then return the next card's front.

        Nothing is written: the card stays due on every device, so the user can do it
        on the desktop later. Use when a card does not work in conversation, for
        example when the image cannot be described well enough.
        """
        try:
            session.skip(card_id)
        except NotDue:
            raise ToolError(f"card {card_id} is not in the queue any more; call next_card") from None
        return present(session.next_card(), prefix=f"Skipped card {card_id} for this session.\n")

    @mcp.tool
    def bury(card_id: int) -> ToolResult:
        """Bury a card until tomorrow without grading it, then return the next card's front.

        This is written to the collection and synced, so the card disappears on every
        device until tomorrow. Prefer skip unless the user wants the card gone for today.
        """
        try:
            session.bury(card_id)
        except NotDue:
            raise ToolError(f"card {card_id} is not in the queue any more; call next_card") from None
        return present(session.next_card(), prefix=f"Buried card {card_id} until tomorrow.\n")

    @mcp.tool
    def undo() -> str:
        """Revert the most recent skip, or the most recent grade or bury if it has not been synced yet."""
        label = session.undo()
        if label is None:
            return "Nothing to undo: the last change has already been synced."
        return f"Undid: {label}. Call next_card to continue."

    @mcp.tool
    def sync() -> str:
        """Push any unsynced grades to AnkiWeb now. Call at the end of a session or before switching to another device."""
        return "Sync: " + session.sync("tool")

    return mcp, session


def _warm(session: AnkiSession) -> None:
    try:
        session.start()
    except Exception:
        log.exception("warm-up failed; will retry lazily on first use")


def _clean(label: str) -> str:
    """Strip the Unicode isolate marks Anki wraps around interval labels."""
    return label.replace("\u2068", "").replace("\u2069", "")


def create_app():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    mcp, _ = build(settings)
    return mcp.http_app(path="/mcp", stateless_http=True)


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    uvicorn.run(create_app(), host="0.0.0.0", port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
