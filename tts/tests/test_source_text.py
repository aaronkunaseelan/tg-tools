"""Source text: what bytes are, whether extraction is usable, what is spoken (pure tables).

effective_extension(data, declared mime, name)
| bytes                        | declared / name         | result                         |
| %PDF                         | text/html, a.html       | pdf (signature wins)           |
| APA Incapsula challenge HTML | text/html, x.pdf        | source_access_blocked          |
| landing page HTML            | application/pdf         | source_type_mismatch           |
| landing page HTML            | text/html               | html                           |
| docx zip                     | -, x.bin                | docx                           |
| clean text                   | octet-stream            | txt                            |
| random binary                | -                       | source_type_mismatch           |
check_extraction_sufficiency: short or replacement-heavy -> error; normal prose -> ok
chatgpt share HTML -> title + four turns via the streamed-data parser, rendered Q/A sections
artifact_to_speech_text: links, code, tables, front matter and entities never reach speech
"""
import io
import zipfile
from pathlib import Path

import pytest

from tts_common.chatgpt_share import parse_share_html, render_conversation
from tts_common.speech_text import artifact_to_speech_text
from tts_common.typed_fetch import TypedFetchError, check_extraction_sufficiency, effective_extension

FIXTURES = Path(__file__).parent / "fixtures"
APA = (FIXTURES / "wave0" / "apa-incapsula-challenge.html").read_bytes()
LANDING = (FIXTURES / "wave0" / "landing-page.html").read_bytes()
DOCX = io.BytesIO()
with zipfile.ZipFile(DOCX, "w") as zf:
    zf.writestr("[Content_Types].xml", "<Types/>")
    zf.writestr("word/document.xml", "<w:document/>")


@pytest.mark.parametrize("data, mime, name, result", [
    (b"%PDF-1.7 x", "text/html", "a.html", "pdf"),
    (APA, "text/html", "xlm-a0035546.pdf", "source_access_blocked"),
    (LANDING, "application/pdf", "doc.pdf", "source_type_mismatch"),
    (LANDING, "text/html", "p", "html"),
    (DOCX.getvalue(), "", "x.bin", "docx"),
    (b"hello world\n" * 20, "application/octet-stream", "noext", "txt"),
    (bytes(range(256)) * 4, "", "blob", "source_type_mismatch"),
])
def test_effective_extension(data, mime, name, result):
    try:
        assert effective_extension(data=data, declared_mime=mime, name=name) == result
    except TypedFetchError as err:
        assert err.error_class == result


@pytest.mark.parametrize("text, ok", [("too short", False), (("ok " + "�" * 10) * 50, False),
                                      ("A sensible paragraph of text. " * 20, True)])
def test_extraction_sufficiency(text, ok):
    if ok:
        check_extraction_sufficiency(text, min_chars=200)
    else:
        with pytest.raises(TypedFetchError):
            check_extraction_sufficiency(text, min_chars=200)


def test_chatgpt_share():
    parsed = parse_share_html((FIXTURES / "chatgpt-share-sample.html").read_bytes(),
                              "https://chatgpt.com/share/00000000-0000-4000-8000-000000000001")
    assert parsed["title"] == "Articles to Constitution Summary"
    assert [t["role"] for t in parsed["turns"]] == ["user", "assistant", "user", "assistant"]
    assert parsed["turns"][1]["text"].startswith("Assistant one sentinel response.")
    assert "## Q1" in render_conversation(parsed) and "## A2" in render_conversation(parsed)


def test_speech_text(tmp_path):
    artifact = tmp_path / "artifact.md"
    artifact.write_text("---\nid: x\n---\n# Head\n\nSee [link](https://x) ![img](i).\n\n```py\nprint(1)\n```\n\n"
                        "| A | B |\n| - | - |\n| 1 | 2 |\n\nPara &amp; end.")
    speech = artifact_to_speech_text(artifact)
    assert "https://" not in speech and "print(1)" not in speech and "id: x" not in speech
    assert "code block omitted" in speech and "table omitted" in speech and "Para & end" in speech
