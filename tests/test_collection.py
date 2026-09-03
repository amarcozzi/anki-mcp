"""Collection layer against a scratch collection. No AnkiWeb involved: sync is stubbed."""

import pytest
from anki.collection import Collection

from anki_mcp.collection import AnkiSession, NotDue


@pytest.fixture
def session(tmp_path, monkeypatch):
    # Build a small collection: 3 text cards + 1 image-front card in deck "Test", 1 card in "Other".
    path = tmp_path / "collection.anki2"
    col = Collection(str(path))
    test_did = col.decks.id("Test")
    other_did = col.decks.id("Other")
    basic = col.models.by_name("Basic")
    io_model = col.models.copy(basic, add=False)
    io_model["name"] = "Image Occlusion Enhanced"
    col.models.add(io_model)
    io_model = col.models.by_name("Image Occlusion Enhanced")
    for front, back, did, model in [
        ('<div id="io-wrapper"><img src="occ.png"></div>', "masked", test_did, io_model),
        ("What is 2+2?", "4", test_did, basic),
        ("Capital of France?", "Paris", test_did, basic),
        ('<img src="map.png">', "Somewhere", test_did, basic),
        ("Largest planet?", "Jupiter", test_did, basic),
        ("Other deck card", "yes", other_did, basic),
    ]:
        n = col.new_note(model)
        n["Front"], n["Back"] = front, back
        col.add_note(n, did)
    col.decks.select(test_did)
    col.close()

    s = AnkiSession(str(tmp_path), "u", "p")
    monkeypatch.setattr(AnkiSession, "sync", lambda self, reason: "stubbed")
    monkeypatch.setattr(AnkiSession, "_login", lambda self: None)
    s._ensure_open()
    s.synced_once = True  # sync is stubbed; pretend the startup sync happened
    return s


def test_decks(session):
    names = {d.name: d for d in session.decks()}
    assert names["Test"].new == 5 and names["Test"].current
    assert names["Other"].new == 1


def test_next_card_filters_images(session):
    seen = set()
    for _ in range(3):
        c = session.next_card(text_only=True)
        assert c and "[image" not in c.front and c.passed_occlusion == 1
        seen.add(c.card_id)
        session.answer(c.card_id, 4, 5)
    # fourth call: only the image card and the occlusion card are left
    assert session.next_card(text_only=True) is None
    c = session.next_card()
    assert c and c.front_images == ["map.png"] and c.passed_occlusion == 1
    session.answer(c.card_id, 4, 5)
    assert session.next_card() is None  # occlusion cards are never served
    assert len(seen) == 3


def test_skip_is_session_only(session):
    a = session.next_card()
    session.skip(a.card_id)
    assert session.pending == 0  # nothing written
    b = session.next_card()
    assert b.card_id != a.card_id
    assert session.undo() == "Skip"
    assert session.next_card().card_id == a.card_id
    with pytest.raises(NotDue):
        session.skip(999)


def test_images_without_media_store(session):
    assert session.images(["x.png"]) == {"x.png": None}


def test_reveal_answer_bury_undo(session):
    c = session.next_card()
    b = session.reveal(c.card_id)
    assert b.back in {"4", "Paris", "Jupiter"} and len(b.labels) == 4
    assert session.answer(c.card_id, 4, 10) == "Easy"  # Good would re-queue a new card via learn-ahead
    assert session.pending == 1
    with pytest.raises(NotDue):
        session.reveal(c.card_id)  # answered; no longer in the queue
    assert session.undo() is not None  # brings it back
    assert session.reveal(c.card_id).card_id == c.card_id
    session.bury(c.card_id)
    with pytest.raises(NotDue):
        session.reveal(c.card_id)
    assert session.next_card().card_id != c.card_id


def test_select_deck(session):
    assert session.select_deck("other") == "Other"
    assert session.next_card().front == "Other deck card"
    with pytest.raises(KeyError):
        session.select_deck("nope")


def test_sync_lag(session, monkeypatch):
    calls = []
    monkeypatch.setattr(AnkiSession, "sync", lambda self, reason: calls.append(reason) or "ok")
    a = session.next_card()
    session.answer(a.card_id, 3, 5)
    assert calls == []  # first mutation: nothing to push yet
    b = session.next_card()
    session.answer(b.card_id, 3, 5)
    assert calls == ["before-mutation"]  # second mutation pushed the first
