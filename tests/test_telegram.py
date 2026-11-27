import asyncio

import httpx

from app.telegram import BotAPI


def test_file_upload_is_multipart_and_plain_calls_are_json():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    api = BotAPI("123:abc", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(api("sendDocument", chat_id=5, caption="r", files={"document": ("a.csv", b"x;y")}))
    asyncio.run(api("sendMessage", chat_id=5, text="hi"))
    upload, plain = seen
    assert upload.headers["content-type"].startswith("multipart/form-data")
    body = upload.read()
    assert b'name="chat_id"' in body and b'filename="a.csv"' in body and b"x;y" in body
    assert plain.headers["content-type"] == "application/json"
