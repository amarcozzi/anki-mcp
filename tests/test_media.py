"""Media store: downloadFiles framing, zip parsing, shrinking, caching. No network."""

import io
import json
import zipfile

import httpx
import zstandard
from PIL import Image

from anki_mcp.media import MediaStore


def png_bytes(size=(3000, 1000), mode="RGBA"):
    buf = io.BytesIO()
    Image.new(mode, size, (200, 30, 30, 255)).save(buf, format="PNG")
    return buf.getvalue()


def fake_ankiweb(requests, files):
    def handler(req: httpx.Request) -> httpx.Response:
        header = json.loads(req.headers["anki-sync"])
        body = json.loads(zstandard.ZstdDecompressor().decompressobj().decompress(req.content))
        requests.append((str(req.url), header, body))
        z = io.BytesIO()
        meta = {}
        with zipfile.ZipFile(z, "w") as zf:
            for i, name in enumerate(body["files"]):
                if name in files:
                    zf.writestr(str(i), files[name])
                    meta[str(i)] = name
            zf.writestr("_meta", json.dumps(meta))
        return httpx.Response(200, content=zstandard.ZstdCompressor().compress(z.getvalue()))

    return handler


def test_download_shrink_cache(tmp_path):
    requests = []
    transport = httpx.MockTransport(fake_ankiweb(requests, {"big.png": png_bytes()}))
    store = MediaStore(str(tmp_path / "cache"), max_edge=600, transport=transport)
    store.set_auth("hkey123", "https://sync11.ankiweb.net/")
    out = store.get(["big.png", "missing.png", "notes.txt"])

    url, header, body = requests[0]
    assert url == "https://sync11.ankiweb.net/msync/downloadFiles"
    assert header["k"] == "hkey123" and header["v"] == 11
    assert body == {"files": ["big.png", "missing.png"]}  # non-image names are never requested

    im = Image.open(io.BytesIO(out["big.png"]))
    assert im.format == "JPEG" and im.size == (600, 200) and im.mode == "RGB"
    assert out["missing.png"] is None and out["notes.txt"] is None

    # second call is served from the cache: no request
    assert store.get(["big.png"])["big.png"] == out["big.png"]
    assert len(requests) == 1


def test_download_failure_is_soft(tmp_path):
    def boom(req):
        raise httpx.ConnectError("down")

    store = MediaStore(str(tmp_path), transport=httpx.MockTransport(boom))
    store.set_auth("k", None)
    assert store.get(["a.png"]) == {"a.png": None}


def test_no_auth_is_soft(tmp_path):
    assert MediaStore(str(tmp_path)).get(["a.png"]) == {"a.png": None}
