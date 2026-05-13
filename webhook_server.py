import asyncio
import hashlib
import hmac
import json
import os

from fastapi import FastAPI, Request, Response

app_http = FastAPI()

_tg_app = None
_main_loop: asyncio.AbstractEventLoop | None = None
_process_fn = None
_wh_events_total: int = 0


def init(tg_app, main_loop: asyncio.AbstractEventLoop, process_fn) -> None:
    global _tg_app, _main_loop, _process_fn
    _tg_app = tg_app
    _main_loop = main_loop
    _process_fn = process_fn


def get_events_total() -> int:
    return _wh_events_total


def _verify(raw: bytes, timestamp: str, signature: str, secret: str) -> bool:
    msg = f"{timestamp}.{raw.decode('utf-8', errors='replace')}"
    expected = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@app_http.get("/health")
async def health():
    return {"status": "ok"}


@app_http.post("/webhook")
async def webhook(request: Request):
    global _wh_events_total
    raw = await request.body()

    secret = os.environ.get("WEBHOOK_SECRET", "")
    if secret and secret != "СЮДА_ВСТАВИШЬ_СЕКРЕТ":
        sig = request.headers.get("x-om-webhook-signature", "")
        ts  = request.headers.get("x-om-webhook-timestamp", "")
        if not sig or not _verify(raw, ts, sig, secret):
            return Response(status_code=401)

    try:
        data = json.loads(raw)
    except Exception:
        return Response(status_code=400)

    _wh_events_total += 1

    if _process_fn is not None and _tg_app is not None and _main_loop is not None:
        asyncio.run_coroutine_threadsafe(_process_fn(_tg_app, data), _main_loop)

    return {"ok": True}
