import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from backend.core.config import APP_NAME, IS_VERCEL
from backend.routes.auth import router as auth_router
from backend.routes.cases import router as cases_router
from backend.routes.webhooks import router as webhooks_router
from backend.routes.ai import router as ai_router
from backend.routes.dashboard import router as dashboard_router
from backend.services.scheduler import scheduler_loop, process_due_tasks
from backend.services.websocket_manager import app_websockets, set_event_loop
from database.init_db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    # In serverless environments (Vercel), avoid running heavy DDL migrations on cold starts.
    # Schema initialization is executed during deployment or standalone migration runs.
    if not IS_VERCEL:
        init_db()
    loop = asyncio.get_running_loop()
    set_event_loop(loop)
    
    scheduler_task = None
    if not IS_VERCEL:
        scheduler_task = asyncio.create_task(scheduler_loop())
        
    yield
    
    if scheduler_task and not scheduler_task.done():
        scheduler_task.cancel()


app = FastAPI(title=APP_NAME, lifespan=lifespan)

# Add CORS Middleware for cross-origin frontend & Vercel preview domains
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Resolve frontend directory reliably across local dev and Vercel serverless
BASE_DIR = Path(__file__).resolve().parent.parent
frontend_dir = BASE_DIR / "frontend"
if not frontend_dir.exists():
    frontend_dir = Path("frontend")

if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

# Include Modular Routers
app.include_router(auth_router)
app.include_router(cases_router)
app.include_router(webhooks_router)
app.include_router(ai_router)
app.include_router(dashboard_router)


@app.get("/", response_class=HTMLResponse)
def home():
    index_path = frontend_dir / "index.html"
    if not index_path.exists():
        index_path = Path("frontend/index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/scheduler/tick")
def trigger_scheduler_tick():
    """Trigger due recovery tasks on-demand (used by Vercel cron or polling)."""
    count = process_due_tasks()
    return {"ok": True, "processed_tasks": count}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    app_websockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        app_websockets.discard(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
