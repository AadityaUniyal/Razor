import asyncio
import logging
import os
from contextlib import asynccontextmanager
from html import escape as html_escape
from pathlib import Path
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from backend.core.config import ALLOWED_ORIGINS, APP_NAME, COOKIE_NAME, IS_VERCEL
from backend.core.rate_limiter import RateLimitMiddleware
from backend.core.security import decode_token
from backend.routes.auth import router as auth_router
from backend.routes.cases import router as cases_router
from backend.routes.webhooks import router as webhooks_router
from backend.routes.ai import router as ai_router
from backend.routes.dashboard import router as dashboard_router
from backend.routes.recovery import router as recovery_router
from backend.services.scheduler import scheduler_loop, process_due_tasks
from backend.services.websocket_manager import app_websockets, set_event_loop
from database.connection import SessionLocal
from database.init_db import init_db
from database.models import User

logger = logging.getLogger("razorrescue.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # In serverless environments (Vercel), avoid running heavy DDL migrations on cold starts.
    # Schema initialization is executed during deployment or standalone migration runs.
    db_ready = IS_VERCEL
    if not IS_VERCEL:
        try:
            init_db()
            db_ready = True
        except Exception:
            # Serving the shell and health endpoint is preferable to making the
            # entire application unavailable while the database recovers.
            logger.exception("Database initialization failed; starting in degraded mode")
    loop = asyncio.get_running_loop()
    set_event_loop(loop)
    
    scheduler_task = None
    if not IS_VERCEL and db_ready:
        scheduler_task = asyncio.create_task(scheduler_loop())
        
    yield
    
    if scheduler_task and not scheduler_task.done():
        scheduler_task.cancel()


app = FastAPI(title=APP_NAME, lifespan=lifespan)

cors_origins = [origin for origin in ALLOWED_ORIGINS if origin != "*"]
if not cors_origins:
    cors_origins = ["http://127.0.0.1:8000", "http://localhost:8000"]

# Add Rate Limiting Middleware
app.add_middleware(RateLimitMiddleware)

# Add CORS Middleware for cross-origin frontend & Vercel preview domains
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
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
app.include_router(recovery_router)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    index_path = frontend_dir / "index.html"
    if not index_path.exists():
        index_path = Path("frontend/index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        html = f.read()
    session_token = request.cookies.get(COOKIE_NAME)
    user_id = decode_token(session_token) if session_token else None
    user = None
    if user_id:
        with SessionLocal() as db:
            user = db.get(User, user_id)

    if user:
        safe_email = html_escape(user.email, quote=True)
        safe_role = html_escape(user.role, quote=True)
        html = html.replace('<section class="auth-container" id="authPanel">', '<section class="auth-container hidden" id="authPanel">')
        html = html.replace('<div id="appPanel" class="app-container hidden">', '<div id="appPanel" class="app-container">')
        html = html.replace('<span id="userEmail" style="font-size:13px; font-weight:600; color:var(--text-muted);"></span>', f'<span id="userEmail" style="font-size:13px; font-weight:600; color:var(--text-muted);">{safe_email}</span>')
        html = html.replace('<div id="view"></div>', f'''
        <div id="view">
          <section class="metric-grid">
            <div class="metric-card"><div class="metric-label">Dashboard Ready</div><div class="metric-val highlight-green">LIVE</div><div class="metric-hint">Authenticated session active</div></div>
            <div class="metric-card"><div class="metric-label">Signed In As</div><div class="metric-val" style="font-size:18px;">{safe_email}</div><div class="metric-hint">Role: {safe_role}</div></div>
            <div class="metric-card"><div class="metric-label">Recovery Console</div><div class="metric-val highlight-blue">READY</div><div class="metric-hint">Live data loads when scripts are enabled</div></div>
          </section>
          <section class="panel"><div class="panel-header"><h3>RazorRescue Dashboard</h3><span class="badge recovered">Authenticated</span></div><p style="font-size:13px;color:var(--text-muted);">Your session is active. Use the navigation to open recovery operations, promises, AI analysis, decision audit, analytics, and system health.</p></section>
        </div>''')
    if request.query_params.get("mode") == "signup":
        html = html.replace('class="auth-tab active" id="tabSignIn"', 'class="auth-tab" id="tabSignIn"')
        html = html.replace('class="auth-tab" id="tabSignUp"', 'class="auth-tab active" id="tabSignUp"')
        html = html.replace('id="authSubheading">Sign in to continue', 'id="authSubheading">Create your account')
        html = html.replace('<form id="signInForm" action="/api/auth/login" method="post" autocomplete="on">', '<form id="signInForm" action="/api/auth/login" method="post" class="hidden" autocomplete="on">')
        html = html.replace('<form id="signUpForm" action="/api/auth/register" method="post" class="hidden" autocomplete="on">', '<form id="signUpForm" action="/api/auth/register" method="post" autocomplete="on">')
    return html


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
