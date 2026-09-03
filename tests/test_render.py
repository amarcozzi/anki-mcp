from anki_mcp.render import is_text_front, render, render_back


def test_render_basic():
    h = (
        '<style>body{color:red}</style><div class="front">What is <b>1 volt</b>?<br>'
        '<img src="v.png"> HINT:&nbsp;\\(1.6 \\times 10^{-19}\\)C</div>[sound:a.mp3]'
    )
    r = render(h)
    assert r.text == "What is 1 volt?\n[image: v.png] HINT: \\(1.6 \\times 10^{-19}\\)C\n[audio: a.mp3]"
    assert r.images == ["v.png"] and r.sounds == ["a.mp3"]


def test_render_back_marker():
    assert render_back("<div>front</div>", "<div>front</div><hr id=answer><div>back</div>").text == "back"


def test_render_back_prefix():
    q = '<div class="q"><div>Ctx</div> Question?</div>'
    a = q + '<div class="a"><hr>Answer<img src="a.png"></div>'
    r = render_back(q, a)
    assert r.text == "Answer [image: a.png]"
    assert r.images == ["a.png"]


def test_render_back_no_overlap():
    assert render_back("<div>front</div>", "<div>only back</div>").text == "only back"


def test_is_text_front():
    assert is_text_front("<div>What is 2+2?</div>")
    assert not is_text_front('<img src="x.png">')
    assert not is_text_front('Which country? <img src="map.png">')
    assert not is_text_front("<div></div>")
