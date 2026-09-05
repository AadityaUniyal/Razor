import asyncio
from typing import Any

app_websockets: set = set()
running_loop: Any = None


def set_event_loop(loop):
    global running_loop
    running_loop = loop


def broadcast(message: dict[str, Any]) -> None:
    if not running_loop:
        return
    dead = []
    for ws in list(app_websockets):
        try:
            asyncio.run_coroutine_threadsafe(ws.send_json(message), running_loop)
        except Exception:
            dead.append(ws)
    for ws in dead:
        app_websockets.discard(ws)
