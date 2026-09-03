"""The collection layer: a disposable local copy of the AnkiWeb collection.

Sync policy (see docs/investigation.md):
  * AnkiWeb is the source of truth; the local file is a cache.
  * Cold start: full download. Session start: normal sync.
  * Mutations are synced with a lag of one, so the last grade stays undoable
    until the next mutation. An idle flush and the shutdown hook close the gap.
  * When a full sync is required the server always DOWNLOADS. It never uploads.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

from anki.collection import Collection
from anki.scheduler_pb2 import CardAnswer, QueuedCards, SchedulingStates
from anki.sync_pb2 import SyncAuth, SyncCollectionResponse

from .media import MediaStore
from .render import is_occlusion, is_text_front, render, render_back

log = logging.getLogger(__name__)

FULL_SYNC_STATES = {
    SyncCollectionResponse.FULL_SYNC,
    SyncCollectionResponse.FULL_DOWNLOAD,
    SyncCollectionResponse.FULL_UPLOAD,
}
RATINGS = {1: CardAnswer.AGAIN, 2: CardAnswer.HARD, 3: CardAnswer.GOOD, 4: CardAnswer.EASY}
RATING_NAMES = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}
QUEUE_SCAN = 200  # how far into the queue to look for a servable card
SKIP_TTL = 8 * 3600  # a session skip hides a card for this long (seconds)
UNDOABLE = {"Answer Card", "Bury"}  # undo labels we are willing to revert


class NotDue(Exception):
    """The card is not in the current deck's review queue any more."""


@dataclass
class DeckInfo:
    id: int
    name: str
    level: int
    new: int
    learn: int
    review: int
    current: bool


@dataclass
class CardView:
    card_id: int
    note_id: int
    deck: str
    front: str
    front_images: list[str]
    new: int
    learn: int
    review: int
    passed_over: int  # due cards passed over because their front is not text (text_only mode)
    passed_occlusion: int  # due image occlusion cards passed over (never servable)


@dataclass
class BackView:
    card_id: int
    back: str
    back_images: list[str]
    labels: list[str]  # next interval per rating, index 0 = Again


class AnkiSession:
    """One collection, one lock, one sync policy."""

    def __init__(
        self,
        directory: str,
        username: str,
        password: str,
        idle_sync_seconds: int = 90,
        media: MediaStore | None = None,
    ):
        self.dir = directory
        self.media = media
        self.skipped: dict[int, float] = {}  # card id -> when it was skipped for this session
        self._last_skip: int | None = None  # so undo can revert a skip that was the latest action
        self.path = os.path.join(directory, "collection.anki2")
        self.username = username
        self.password = password
        self.idle_sync_seconds = idle_sync_seconds
        self.lock = threading.RLock()
        self.col: Collection | None = None
        self.auth: SyncAuth | None = None
        self.pending = 0  # mutations not yet synced
        self.synced_once = False  # never serve reads from a collection that has not synced
        self.last_activity = time.time()
        self._stop = threading.Event()
        self._flusher = threading.Thread(target=self._idle_flush_loop, name="idle-sync", daemon=True)

    # --- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        """Open (downloading if needed), sync, and start the idle flusher."""
        with self.lock:
            self._ensure_ready()
        self._flusher.start()

    def close(self) -> None:
        """Flush and close. Called on SIGTERM; Cloud Run gives ~10 s."""
        self._stop.set()
        with self.lock:
            if self.col is None:
                return
            try:
                if self.pending:
                    self.sync("shutdown")
            finally:
                self.col.close()
                self.col = None

    def _ensure_open(self) -> None:
        if self.col is not None:
            return
        os.makedirs(self.dir, exist_ok=True)
        if not os.path.exists(self.path):
            log.info("no local collection; the first sync will download it from AnkiWeb")
        self.col = Collection(self.path)

    def _ensure_ready(self) -> None:
        """Open, and sync once so a fresh (empty) collection is never served.

        A fresh collection goes: normal sync -> AnkiWeb answers FULL_DOWNLOAD and
        hands over its real sync endpoint -> full download. Downloading without
        that first sync is rejected by AnkiWeb (HTTP 400 "missing original size").
        """
        self._ensure_open()
        if not self.synced_once:
            self.sync("startup")

    def _login(self) -> SyncAuth:
        assert self.col is not None
        if self.auth is None:
            self.auth = self.col.sync_login(username=self.username, password=self.password, endpoint=None)
            log.info("logged in to AnkiWeb")
            self._share_auth()
        return self.auth

    def _share_auth(self) -> None:
        if self.media is not None and self.auth is not None:
            self.media.set_auth(self.auth.hkey, self.auth.endpoint or None)

    def _full_download(self, server_media_usn: int) -> None:
        """Replace the local collection with AnkiWeb's. Never the other direction."""
        assert self.col is not None
        auth = self._login()
        t0 = time.time()
        self.col.close_for_full_sync()
        self.col.full_upload_or_download(auth=auth, server_usn=server_media_usn, upload=False)
        self.col.reopen(after_full_sync=True)
        self.pending = 0
        log.info("full download complete in %.1fs (%d cards)", time.time() - t0, self.col.card_count())

    # --- sync -------------------------------------------------------------------------------

    def sync(self, reason: str) -> str:
        """Normal sync; falls back to a full download if AnkiWeb requires a full sync."""
        with self.lock:
            self._ensure_open()
            assert self.col is not None
            auth = self._login()
            t0 = time.time()
            out = self.col.sync_collection(auth, sync_media=False)
            if out.new_endpoint:
                self.auth = SyncAuth(hkey=auth.hkey, endpoint=out.new_endpoint)
                self._share_auth()
            if out.required in FULL_SYNC_STATES:
                lost = self.pending
                log.warning(
                    "AnkiWeb requires a full sync (%s); downloading, %d unsynced change(s) lost",
                    SyncCollectionResponse.ChangesRequired.Name(out.required),
                    lost,
                )
                self._full_download(out.server_media_usn)
                self.synced_once = True
                return f"full download (AnkiWeb required it; {lost} unsynced change(s) discarded)"
            self.pending = 0
            self.synced_once = True
            log.info("sync (%s) ok in %.2fs", reason, time.time() - t0)
            return "ok"

    def _before_mutation(self) -> None:
        """Sync lag of one: push the previous mutation right before making a new one."""
        self._ensure_ready()
        self.last_activity = time.time()
        if self.pending:
            self.sync("before-mutation")

    def _idle_flush_loop(self) -> None:
        while not self._stop.wait(15):
            with self.lock:
                if self.pending and time.time() - self.last_activity > self.idle_sync_seconds:
                    try:
                        self.sync("idle")
                    except Exception:
                        log.exception("idle sync failed")

    # --- reads ------------------------------------------------------------------------------

    def decks(self) -> list[DeckInfo]:
        with self.lock:
            self._ensure_ready()
            assert self.col is not None
            self.last_activity = time.time()
            cur = self.col.decks.current()["id"]
            out: list[DeckInfo] = []

            def walk(node, prefix: str, level: int) -> None:
                for ch in node.children:
                    name = f"{prefix}::{ch.name}" if prefix else ch.name
                    out.append(
                        DeckInfo(
                            ch.deck_id,
                            name,
                            level,
                            ch.new_count,
                            ch.learn_count,
                            ch.review_count,
                            ch.deck_id == cur,
                        )
                    )
                    walk(ch, name, level + 1)

            walk(self.col.sched.deck_due_tree(), "", 0)
            return out

    def select_deck(self, name: str) -> str:
        with self.lock:
            self._ensure_ready()
            assert self.col is not None
            names = [(d.name, d.id) for d in self.col.decks.all_names_and_ids()]
            exact = [n for n in names if n[0].lower() == name.lower()]
            suffix = [n for n in names if n[0].lower().endswith("::" + name.lower())]
            hit = exact or (suffix if len(suffix) == 1 else [])
            if not hit:
                raise KeyError(name)
            self.col.decks.select(hit[0][1])
            return hit[0][0]

    def _queue(self) -> QueuedCards:
        assert self.col is not None
        return self.col.sched.get_queued_cards(fetch_limit=QUEUE_SCAN)

    def _reset_queue(self) -> None:
        """Drop the backend's built queue so a card that is not at its top can be answered.

        The scheduler refuses to grade any card but the queue head, which is exactly
        what happens when a non-text card is being passed over. Changing the current
        deck is the public operation that clears the queue (selecting the same deck
        again is a no-op), so toggle away and back. The next get_queued_cards rebuilds.
        Also needed after undo, which leaves the built queue stale.
        """
        assert self.col is not None
        cur = self.col.decks.current()["id"]
        other = 1 if cur != 1 else next((d.id for d in self.col.decks.all_names_and_ids() if d.id != 1), None)
        if other is None:
            return
        self.col.decks.select(other)
        self.col.decks.select(cur)

    def _find_states(self, card_id: int) -> SchedulingStates:
        for qc in self._queue().cards:
            if qc.card.id == card_id:
                return qc.states
        raise NotDue(card_id)

    def next_card(self, text_only: bool = False) -> CardView | None:
        """The next servable due card: not skipped this session, not image occlusion,
        and with text_only also nothing with an image on the front."""
        with self.lock:
            self._ensure_ready()
            assert self.col is not None
            self.last_activity = time.time()
            self._expire_skips()
            q = self._queue()
            passed = occluded = 0
            for qc in q.cards:
                if qc.card.id in self.skipped:
                    continue
                card = self.col.get_card(qc.card.id)
                qhtml = card.question()
                if is_occlusion(qhtml, card.note_type()["name"]):
                    occluded += 1
                    continue
                if text_only and not is_text_front(qhtml):
                    passed += 1
                    continue
                fr = render(qhtml)
                return CardView(
                    card.id,
                    card.nid,
                    self.col.decks.name(card.did),
                    fr.text,
                    fr.images,
                    q.new_count,
                    q.learning_count,
                    q.review_count,
                    passed,
                    occluded,
                )
            return None

    def _expire_skips(self) -> None:
        cutoff = time.time() - SKIP_TTL
        for cid in [c for c, t in self.skipped.items() if t < cutoff]:
            del self.skipped[cid]

    def images(self, names: list[str]) -> dict[str, bytes | None]:
        """Shrunken JPEG bytes for the given media file names, fetched from AnkiWeb as needed."""
        if self.media is None or not names:
            return dict.fromkeys(names)
        with self.lock:
            self._ensure_ready()
            self._login()
        return self.media.get(names)  # network I/O outside the collection lock

    def reveal(self, card_id: int) -> BackView:
        with self.lock:
            self._ensure_ready()
            assert self.col is not None
            self.last_activity = time.time()
            states = self._find_states(card_id)
            card = self.col.get_card(card_id)
            back = render_back(card.question(), card.answer())
            labels = list(self.col.sched.describe_next_states(states))
            return BackView(card_id, back.text, back.images, labels)

    # --- mutations ----------------------------------------------------------------------------

    def answer(self, card_id: int, rating: int, seconds_taken: int) -> str:
        if rating not in RATINGS:
            raise ValueError("rating must be 1-4")
        with self.lock:
            self._before_mutation()
            assert self.col is not None
            states = self._find_states(card_id)
            self._last_skip = None
            new_state = {1: states.again, 2: states.hard, 3: states.good, 4: states.easy}[rating]
            self._reset_queue()
            self.col.sched.answer_card(
                CardAnswer(
                    card_id=card_id,
                    current_state=states.current,
                    new_state=new_state,
                    rating=RATINGS[rating],
                    answered_at_millis=int(time.time() * 1000),
                    milliseconds_taken=min(max(seconds_taken, 1), 60) * 1000,
                )
            )
            self.pending += 1
            return RATING_NAMES[rating]

    def bury(self, card_id: int) -> None:
        with self.lock:
            self._before_mutation()
            assert self.col is not None
            self._find_states(card_id)  # must be due in the current deck
            self._last_skip = None
            self.col.sched.bury_cards([card_id], manual=True)
            self.pending += 1

    def skip(self, card_id: int) -> None:
        """Hide a card for the rest of this session. Nothing is written; it stays due everywhere."""
        with self.lock:
            self._ensure_ready()
            self.last_activity = time.time()
            self._find_states(card_id)
            self.skipped[card_id] = time.time()
            self._last_skip = card_id

    def undo(self) -> str | None:
        """Undo the last skip, or the last mutation if it has not been synced yet. Returns its label."""
        with self.lock:
            self._ensure_ready()
            assert self.col is not None
            self.last_activity = time.time()
            if self._last_skip is not None:
                self.skipped.pop(self._last_skip, None)
                self._last_skip = None
                return "Skip"
            status = self.col.undo_status()
            # Only ever revert a grade or a bury. Deck selection and other bookkeeping
            # ops also sit on the undo stack and must not be exposed.
            if status.undo not in UNDOABLE:
                return None
            self.col.undo()
            self._reset_queue()  # undo leaves the built queue stale
            return status.undo
