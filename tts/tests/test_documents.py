"""Documents: tg-tools' own conversion and guarded URL fetch (pure tables + real edges).

to_markdown(staged file) by suffix
| input                                  | outcome                                        |
| .md / .txt                             | text as given                                  |
| .json                                  | fenced json                                    |
| html: title, description, nav, article | "# T", "> D", article body, nav dropped        |
| html: short article, long body         | whole body (article too thin to trust)         |
| latin-1 html with meta charset         | decoded correctly                              |
| layout table with empty cells          | cell text kept, pipe-only lines dropped        |
| .docx / .bin                           | unsupported_format                             |
| text over the cap                      | extract_truncated (never a silent slice)       |
| empty                                  | extract_insufficient_text                      |
same bytes -> identical markdown and meta (meta is uploaded to an immutable key)
unwrap_pdf_text: printed lines -> paragraphs, broken words rejoined, page breaks split
real pdftotext: arXiv 1006.3868 -> title present, paragraphs, full length; materialise meta
address_allowed: only global unicast addresses
fetch against a local server (loopback allowed for the test only)
| path                        | outcome                                             |
| /doc                        | bytes, content type, attachment filename            |
| /hop -> /doc                | final URL after redirect                            |
| /meta -> 169.254.169.254    | blocked_fetch_target (redirect re-checked)          |
| /loop                       | too_many_redirects                                  |
| /gzip                       | decoded body                                        |
| /big (Content-Length), /stream (chunked) | byte_cap_exceeded                      |
| /403 /404 /429 /500         | taxonomy: access_blocked, access_blocked, transient, transient |
unpatched loopback URL -> blocked_fetch_target; ftp:// and userinfo -> invalid_url
"""
import gzip
import http.server
import json
import threading
from pathlib import Path

import pytest

from tts_common import source_fetch
from tts_common.convert import ConversionError, to_markdown, unwrap_pdf_text
from tts_common.materialise import materialise
from tts_common.source_fetch import FetchFailure, address_allowed, fetch
from tts_common.typed_fetch import TypedFetchError, fetch_url
from tts_worker.document_tools import DocumentToolError, pdftotext_bin, validate_document_tools

FIXTURES = Path(__file__).parent / "fixtures"
ARTICLE = "<p>" + "The study measured particles released by heated bottles. " * 12 + "</p>"
PAGE = (f"<html><head><title>T</title><meta name='description' content='D'></head><body>"
        f"<nav>Home | About</nav><article><h1>Head</h1>{ARTICLE}<ul><li>one</li><li>two</li></ul></article>"
        f"<footer>Copyright</footer><script>var x = 1;</script></body></html>").encode()
THIN = b"<html><body><article>Teaser</article><div>" + ARTICLE.encode() + b"</div></body></html>"
LATIN = "<html><head><meta charset='iso-8859-1'></head><body><p>Café crème</p></body></html>".encode("latin-1")


def _convert(tmp_path, name, data, *, max_chars=10_000, mime=""):
    path = tmp_path / name
    path.write_bytes(data)
    return to_markdown(path, mime=mime, max_chars=max_chars, pdftotext=pdftotext_bin())


@pytest.mark.parametrize("name, data, present, absent", [
    ("a.md", b"# Title\n\nBody text.", ["# Title\n\nBody text."], []),
    ("a.txt", b"plain words", ["plain words"], []),
    ("a.json", b'{"a": 1}', ['```json\n{"a": 1}\n```'], []),
    ("p.html", PAGE, ["# T\n", "> D", "# Head", "heated bottles.", "- one", "- two"],
     ["Home", "Copyright", "var x"]),
    ("p.html", THIN, ["heated bottles."], []),
    ("p.html", LATIN, ["Café crème"], []),
    ("p.html", b"<table><tr><td></td><td>Cell text</td></tr><tr><td></td><td></td></tr></table>", ["Cell text\n"], ["|"]),
])
def test_to_markdown(tmp_path, name, data, present, absent):
    markdown, meta = _convert(tmp_path, name, data)
    for text in present:
        assert text in markdown
    for text in absent:
        assert text not in markdown
    assert meta["extraction_state"] == "extracted" and meta["backend_attempts"][0]["status"] == "ok"


@pytest.mark.parametrize("name, data, max_chars, error_class", [
    ("x.docx", b"PK\x03\x04", 10_000, "unsupported_format"),
    ("x.bin", b"\x00\x01", 10_000, "unsupported_format"),
    ("big.txt", b"word " * 200, 50, "extract_truncated"),
    ("empty.txt", b"  \n", 10_000, "extract_insufficient_text"),
])
def test_to_markdown_refuses(tmp_path, name, data, max_chars, error_class):
    with pytest.raises(ConversionError) as err:
        _convert(tmp_path, name, data, max_chars=max_chars)
    assert err.value.error_class == error_class


@pytest.mark.parametrize("raw, expected", [
    ("one line\nwraps here\n\nnext para", "one line wraps here\n\nnext para"),
    ("Min-\nneapolis study", "Minneapolis study"),
    ("Section 2 -\nNext", "Section 2 - Next"),
    ("page one\fpage two", "page one\n\npage two"),
    ("\n\n  \n", ""),
])
def test_unwrap_pdf_text(raw, expected):
    assert unwrap_pdf_text(raw) == expected


def test_conversion_is_deterministic(tmp_path):
    first = _convert(tmp_path, "p.html", PAGE)
    assert _convert(tmp_path, "p.html", PAGE) == first


@pytest.mark.skipif(not (FIXTURES / "arxiv-1006.3868.pdf").exists(), reason="Non-CC arXiv fixture not distributed")
def test_real_pdftotext_through_materialise(tmp_path):
    result = materialise(FIXTURES / "arxiv-1006.3868.pdf", tmp_path / "out", mime="application/pdf")
    markdown = result["markdown_path"].read_text()
    meta = json.loads(result["meta_path"].read_text())
    assert "Philosophy and the practice of Bayesian statistics" in markdown
    assert len(markdown) > 50_000 and "\n\n" in markdown
    assert meta["extraction_method"] == "pdftotext" and meta["mime_type"] == "application/pdf"
    assert meta["source_sha256"] and meta["converter_version"]


def test_pdftotext_validation(tmp_path):
    assert validate_document_tools({"PATH": "/usr/bin:/bin"})["version"].startswith("pdftotext version ")
    with pytest.raises(DocumentToolError):
        validate_document_tools({"TTS_PDFTOTEXT_BIN": str(tmp_path / "missing")})


@pytest.mark.parametrize("address, allowed", [
    ("8.8.8.8", True), ("2606:4700:4700::1111", True),
    ("127.0.0.1", False), ("10.1.2.3", False), ("192.168.1.1", False), ("169.254.169.254", False),
    ("100.64.0.1", False), ("::1", False), ("::ffff:127.0.0.1", False), ("fd00::1", False), ("224.0.0.1", False),
])
def test_address_allowed(address, allowed):
    assert address_allowed(address) is allowed


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", headers=()):
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        if not any(key == "Transfer-Encoding" for key, _ in headers):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path
        if path == "/doc":
            self._send(200, b"%PDF-1.7 body", [("Content-Type", "application/pdf"),
                                                ("Content-Disposition", 'attachment; filename="paper.pdf"')])
        elif path == "/hop":
            self._send(302, headers=[("Location", "/doc")])
        elif path == "/meta":
            self._send(302, headers=[("Location", "http://169.254.169.254/latest/meta-data/")])
        elif path == "/loop":
            self._send(302, headers=[("Location", "/loop")])
        elif path == "/gzip":
            self._send(200, gzip.compress(b"hello " * 100), [("Content-Encoding", "gzip"), ("Content-Type", "text/plain")])
        elif path == "/big":
            self._send(200, b"x" * 2048)
        elif path == "/stream":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for _ in range(4):
                self.wfile.write(b"400\r\n" + b"y" * 1024 + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        else:
            self._send(int(path.strip("/")))


@pytest.fixture
def server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def loopback_allowed(monkeypatch):
    monkeypatch.setattr(source_fetch, "address_allowed", lambda address: address == "127.0.0.1")


def test_fetch_success_redirect_and_gzip(server, loopback_allowed):
    doc = fetch(server + "/doc", max_bytes=1024)
    assert (doc.data, doc.content_type, doc.filename) == (b"%PDF-1.7 body", "application/pdf", "paper.pdf")
    assert fetch(server + "/hop", max_bytes=1024).final_url == server + "/doc"
    assert fetch(server + "/gzip", max_bytes=1024).data == b"hello " * 100


@pytest.mark.parametrize("path, failure", [
    ("/meta", "blocked_fetch_target"), ("/loop", "too_many_redirects"),
    ("/big", "byte_cap_exceeded"), ("/stream", "byte_cap_exceeded"),
    ("/403", "http_forbidden"), ("/404", "http_not_found"), ("/429", "http_rate_limited"), ("/500", "http_server_error"),
])
def test_fetch_failures(server, loopback_allowed, path, failure):
    with pytest.raises(FetchFailure) as err:
        fetch(server + path, max_bytes=1024)
    assert err.value.failure_class == failure


@pytest.mark.parametrize("path, error_class", [
    ("/403", "source_access_blocked"), ("/404", "source_access_blocked"),
    ("/429", "source_fetch_transient"), ("/500", "source_fetch_transient"), ("/big", "source_too_large"),
])
def test_fetch_url_maps_to_taxonomy(server, loopback_allowed, tmp_path, path, error_class):
    with pytest.raises(TypedFetchError) as err:
        fetch_url(server + path, tmp_path, max_bytes=1024)
    assert err.value.error_class == error_class


def test_fetch_refuses_private_and_malformed(server):
    for url, failure in ((server + "/doc", "blocked_fetch_target"), ("ftp://example.org/x", "invalid_url"),
                         ("http://user:pw@example.org/", "invalid_url")):
        with pytest.raises(FetchFailure) as err:
            fetch(url, max_bytes=1024)
        assert err.value.failure_class == failure
