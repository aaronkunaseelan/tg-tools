"""Real HTTP edge for tg_gateway.bot_api: the client's urlopen against a local Bot API.

| call                           | edge response                          | outcome                                   |
| send_rich_message              | 200 ok                                 | JSON body, result returned                |
| send_document + reply + markup | 200 ok                                 | multipart file, reply_parameters, keyboard|
| any send                       | 200 ok:false 400                       | BotApiError names method, never the token |
| any send                       | HTTP 429 body retry_after 9            | BotApiError.retry_after == 9              |
| any send                       | retry_after True/False/0/-1/"9"/None   | retry_after None                          |
"""
import json

import pytest

from chain_harness import Edge
from tg_gateway.bot_api import BotApiClient, BotApiError

TOKEN = "123:secret"


@pytest.fixture
def edge():
    edge = Edge()
    yield edge
    edge.close()


def client(edge):
    return BotApiClient(token=TOKEN, base_url=edge.url)


def test_rich_message_and_document(edge):
    api = client(edge)
    assert api.send_rich_message(chat_id=-100, message_thread_id=55, rich_message={"markdown": "# Hello"})["message_id"]
    api.send_document(chat_id=-100, message_thread_id=55, reply_to_message_id=77, file_name="preview.md",
                      content=b"hello", caption="Preview",
                      reply_markup={"inline_keyboard": [[{"text": "Accept", "callback_data": "ms:t:a:1"}]]})
    rich, document = edge.sent()
    assert (rich["method"], rich["payload"]) == (
        "sendRichMessage", {"chat_id": -100, "message_thread_id": 55, "rich_message": {"markdown": "# Hello"}})
    fields = document["payload"]
    assert document["method"] == "sendDocument"
    assert fields["document"] == {"filename": "preview.md", "content": b"hello"}
    assert json.loads(fields["reply_parameters"])["message_id"] == 77
    assert json.loads(fields["reply_markup"])["inline_keyboard"][0][0]["text"] == "Accept"


def failing(edge, status, body):
    edge.route("POST", r"/sendMessage$", lambda req: (status, body))
    with pytest.raises(BotApiError) as caught:
        client(edge).send_message(chat_id=-100, text="x")
    return caught.value


def test_error_names_method_not_token(edge):
    err = failing(edge, 200, {"ok": False, "error_code": 400, "description": "Bad Request: test failure"})
    assert (err.method, err.error_code) == ("sendMessage", 400)
    assert "sendMessage" in str(err) and TOKEN not in str(err) and "secret" not in str(err)


@pytest.mark.parametrize("value, expected", [(9, 9), (True, None), (False, None), (0, None), (-1, None),
                                             ("9", None), (None, None)])
def test_http_429_retry_after(edge, value, expected):
    err = failing(edge, 429, {"ok": False, "error_code": 429, "description": "Too Many Requests",
                              "parameters": {"retry_after": value}})
    assert (err.error_code, err.retry_after) == (429, expected)
