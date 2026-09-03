#!/usr/bin/env python3
"""Probe AnkiWeb's private study endpoints (the ones the browser study page uses).

Read-only by default: logs in, lists decks with due counts, fetches the next card
in the current deck, prints it. Nothing is answered unless you pass --answer.

Usage:
    ANKIWEB_USERNAME=you@example.com ANKIWEB_PASSWORD=... python3 scripts/ankiweb_probe.py
    python3 scripts/ankiweb_probe.py --deck "History"          # select a deck first
    python3 scripts/ankiweb_probe.py --answer 3                # WRITES a review (Good) for the shown card

These endpoints are undocumented and unsupported by AnkiWeb. Protocol was reverse-
engineered from the study page's JS bundle on 2026-09-02; it can change at any time.
"""
import argparse, getpass, html, http.cookiejar, os, re, sys, urllib.request, urllib.parse, time

WEB = "https://ankiweb.net"
USER = "https://ankiuser.net"

# ---- minimal protobuf wire codec ---------------------------------------------------------
def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F; n >>= 7
        if n: out.append(b | 0x80)
        else: out.append(b); return bytes(out)

def enc(fields):
    """fields: list of (no, wire, value). wire 0 = varint int, 2 = bytes/str/submessage."""
    out = bytearray()
    for no, wire, v in fields:
        if v is None: continue
        out += _varint((no << 3) | wire)
        if wire == 0: out += _varint(v)
        else:
            if isinstance(v, str): v = v.encode()
            out += _varint(len(v)) + v
    return bytes(out)

def dec(buf):
    """Generic decode -> {field_no: [value, ...]}; length-delimited values stay raw bytes."""
    i, out = 0, {}
    def rv():
        nonlocal i
        shift = n = 0
        while True:
            b = buf[i]; i += 1; n |= (b & 0x7F) << shift; shift += 7
            if not b & 0x80: return n
    while i < len(buf):
        tag = rv(); no, wire = tag >> 3, tag & 7
        if wire == 0: v = rv()
        elif wire == 2: ln = rv(); v = buf[i:i+ln]; i += ln
        elif wire == 1: v = buf[i:i+8]; i += 8
        elif wire == 5: v = buf[i:i+4]; i += 4
        else: raise ValueError(f"unsupported wire type {wire}")
        out.setdefault(no, []).append(v)
    return out

def first(d, no, default=None):
    return d[no][0] if no in d else default

# ---- HTTP ----------------------------------------------------------------------------------
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
opener.addheaders = [("User-Agent", "anki-mcp-probe/0.1")]

def rpc(base, path, body):
    req = urllib.request.Request(base + path, data=body, method="POST",
                                 headers={"Content-Type": "application/octet-stream"})
    with opener.open(req, timeout=30) as r:
        return r.status, r.read()

def strip_html(s):
    s = re.sub(r"<style>.*?</style>", "", s, flags=re.S)
    s = re.sub(r"<img[^>]*src=\"([^\"]+)\"[^>]*>", r" [IMG \1] ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()

# ---- flow ----------------------------------------------------------------------------------
def login(username, password):
    status, body = rpc(WEB, "/svc/account/login", enc([(1, 2, username), (2, 2, password)]))
    r = dec(body)
    st = first(r, 1, 0); token = first(r, 2, b"").decode()
    names = {0: "UNKNOWN", 1: "AUTHENTICATED", 2: "INVALID_USER", 3: "INVALID_PASS"}
    print(f"login: HTTP {status}, status={names.get(st, st)}, token={'yes' if token else 'no'}")
    if st != 1: sys.exit(1)
    # hand the session to ankiuser.net (this is what the browser does after login)
    with opener.open(USER + "/account/ankiuser-login?t=" + urllib.parse.quote(token), timeout=30) as r:
        print(f"ankiuser handoff: HTTP {r.status} -> {r.url}")

def walk(node, depth=0, out=None):
    out = out if out is not None else []
    d = dec(node)
    out.append((first(d, 1), first(d, 2, b"").decode(), first(d, 8, 0), first(d, 7, 0), first(d, 6, 0), depth))
    for child in d.get(3, []): walk(child, depth + 1, out)
    return out

def deck_list():
    status, body = rpc(WEB, "/svc/decks/deck-list-info", enc([(1, 0, 0)]))
    r = dec(body)
    decks = walk(first(r, 1))
    cur = first(r, 2)
    print(f"deck-list-info: HTTP {status}, current_deck_id={cur}, collection={first(r,3,0)/1e6:.1f} MB")
    for did, name, new, learn, rev, depth in decks:
        if depth == 0: continue  # root node
        mark = "*" if did == cur else " "
        print(f"  {mark} {'  '*(depth-1)}{name:45s} new={new:<4} learn={learn:<4} review={rev}")
    return decks

def select_deck(deck_id):
    status, _ = rpc(WEB, "/svc/decks/select-deck", enc([(1, 0, deck_id)]))
    print(f"select-deck {deck_id}: HTTP {status}")

def study_cards(answer=None):
    status, body = rpc(USER, "/svc/study/study-cards", enc([(1, 2, answer)]) if answer else b"")
    r = dec(body)
    print(f"study-cards: HTTP {status}, sched_ver={first(r,1)}, counts new={first(r,3,0)} learn={first(r,4,0)} review={first(r,5,0)}, cards returned={len(r.get(2, []))}")
    cards = []
    for raw in r.get(2, []):
        c = dec(raw)
        cards.append(dict(card_id=first(c, 1), question=first(c, 2, b"").decode(), answer=first(c, 3, b"").decode(),
                          buttons=[b.decode() for b in c.get(5, [])], note_id=first(c, 6), next_states_raw=first(c, 8)))
    return cards

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deck", help="deck name to select before fetching")
    ap.add_argument("--answer", type=int, choices=[1, 2, 3, 4], help="WRITES a review for the shown card (1=Again .. 4=Easy)")
    a = ap.parse_args()
    user = os.environ.get("ANKIWEB_USERNAME") or input("AnkiWeb username (email): ")
    pw = os.environ.get("ANKIWEB_PASSWORD") or getpass.getpass("AnkiWeb password: ")
    login(user, pw)
    decks = deck_list()
    if a.deck:
        match = [d for d in decks if d[1] == a.deck]
        if not match: sys.exit(f"no deck named {a.deck!r}")
        select_deck(match[0][0])
    cards = study_cards()
    if not cards: print("no cards due in current deck"); return
    c = cards[0]
    print(f"\nCARD {c['card_id']} (note {c['note_id']}) buttons={c['buttons']}")
    print("  FRONT:", strip_html(c["question"])[:300])
    print("  BACK :", strip_html(c["answer"])[:300])
    if a.answer:
        ns = dec(c["next_states_raw"])                     # 1=current 2=again 3=hard 4=good 5=easy
        chosen = first(ns, {1: 2, 2: 3, 3: 4, 4: 5}[a.answer])
        t0 = int(time.time() * 1000)
        ans = enc([(1, 0, c["card_id"]), (2, 0, a.answer), (3, 0, 3000), (4, 0, t0),
                   (5, 2, first(ns, 1)), (6, 2, chosen)])
        nxt = study_cards(answer=ans)
        print(f"answered card {c['card_id']} with {a.answer}; next card: {nxt[0]['card_id'] if nxt else None}")

if __name__ == "__main__":
    main()
