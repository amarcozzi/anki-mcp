"""Turn rendered card HTML into text a chat agent can present."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

_STYLE = re.compile(r"<style.*?</style>", re.IGNORECASE | re.DOTALL)
_SCRIPT = re.compile(r"<script.*?</script>", re.IGNORECASE | re.DOTALL)
_IMG = re.compile(r"<img[^>]*?src=\"([^\"]+)\"[^>]*>", re.IGNORECASE)
_SOUND = re.compile(r"\[sound:([^\]]+)\]")
_BREAK = re.compile(r"</?(br|p|div|li|ul|ol|tr|table|hr|h[1-6]|blockquote|pre)\b[^>]*>", re.IGNORECASE)
_CELL = re.compile(r"</?(td|th)\b[^>]*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t ]+")
_LINES = re.compile(r"\n{3,}")
_ANSWER_MARK = re.compile(r"<hr id=\"?answer\"?[^>]*>", re.IGNORECASE)
_OCCLUSION = re.compile(r"id=\"?io-(overlay|wrapper)\"?|image-occlusion", re.IGNORECASE)


@dataclass
class Rendered:
    text: str
    images: list[str] = field(default_factory=list)
    sounds: list[str] = field(default_factory=list)

    @property
    def has_images(self) -> bool:
        return bool(self.images)

    @property
    def text_without_markers(self) -> str:
        t = re.sub(r"\[image: [^\]]*\]", "", self.text)
        t = re.sub(r"\[audio: [^\]]*\]", "", t)
        return t.strip()


def render(h: str) -> Rendered:
    """Plain text with images/audio replaced by markers; MathJax left as LaTeX."""
    r = Rendered(text="")
    h = _STYLE.sub("", h)
    h = _SCRIPT.sub("", h)

    def img(m: re.Match[str]) -> str:
        src = html.unescape(m.group(1))
        r.images.append(src)
        return f" [image: {src}] "

    def snd(m: re.Match[str]) -> str:
        r.sounds.append(m.group(1))
        return f" [audio: {m.group(1)}] "

    h = _IMG.sub(img, h)
    h = _SOUND.sub(snd, h)
    h = _BREAK.sub("\n", h)
    h = _CELL.sub(" ", h)
    h = _TAG.sub("", h)
    h = html.unescape(h)
    lines = [_SPACES.sub(" ", line).strip() for line in h.split("\n")]
    r.text = _LINES.sub("\n\n", "\n".join(lines)).strip()
    return r


def render_back(question_html: str, answer_html: str) -> Rendered:
    """The answer side with the repeated question removed.

    Templates usually start the back with {{FrontSide}}. Some mark the boundary
    with <hr id=answer>, others with a bare <hr> or nothing, so use the marker
    when present and otherwise strip the rendered front as a text prefix.
    """
    m = _ANSWER_MARK.search(answer_html)
    if m:
        return render(answer_html[m.end() :])
    front = render(question_html)
    back = render(answer_html)
    if front.text and back.text.startswith(front.text):
        back.text = back.text[len(front.text) :].strip()
        back.images = back.images[len(front.images) :]
        back.sounds = back.sounds[len(front.sounds) :]
    return back


def is_occlusion(question_html: str, notetype_name: str = "") -> bool:
    """True for image occlusion cards (built-in or the Enhanced add-on).

    Their prompt is an image with SVG masks drawn over it, which the server cannot
    composite, so they are never served.
    """
    return notetype_name.lower().startswith("image occlusion") or bool(_OCCLUSION.search(question_html))


def is_text_front(question_html: str) -> bool:
    """True if the front can be presented in chat: no images, some text."""
    r = render(question_html)
    return not r.has_images and bool(r.text_without_markers)
