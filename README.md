# RazorRescue

RazorRescue is a FastAPI revenue-recovery console for subscription merchants. It combines payment events, subscription context, deterministic recovery policy, AI recommendations, human approval, provider verification, and outcome analytics in one operator workspace.

## Product Flow

```text
Payment event -> Recovery case -> Context + recommendation -> Policy -> Approval -> Provider verification -> Outcome
```

The AI layer is advisory. Financial state and outbound work remain governed by deterministic policy and the operator workflow.

## Repository Layout

```text
razor/
├── api/index.py                 # Vercel Python/ASGI entrypoint
├── backend/
│   ├── core/                    # Configuration, authentication, rate limiting, tenancy helpers
│   ├── routes/                  # FastAPI route modules
│   └── services/                # AI, policy, provider, scheduler, and recovery services
├── database/
│   ├── connection.py            # SQLAlchemy engine, sessions, pooling, health checks
│   ├── models.py                # SQLAlchemy models and relationships
│   └── init_db.py               # Schema initialization, upgrades, and seed data
├── frontend/
│   ├── index.html               # SPA templates
│   ├── app.js                   # View rendering and API interaction
│   ├── styles.css                # Console design system
│   └── favicon.svg
├── tests/                       # Product regression and security tests
├── app.py                       # Local application entrypoint
├── requirements.txt             # Python dependencies
├── vercel.json                  # Vercel routing and scheduled task configuration
└── .env.example                 # Secret names only; no credentials
```

## Local Setup

Requirements: Python 3.11+ and a PostgreSQL-compatible database.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Set the values in `.env` locally. Never commit `.env`, database URLs, API keys, webhook secrets, or session secrets.

```powershell
python -m uvicorn app:app --reload --port 8000
```

Open `http://localhost:8000` locally. The application stores state in the configured PostgreSQL database; it does not create a local SQLite database.

## Environment Variables

Required for production:

- `DATABASE_URL`
- `SESSION_SECRET`

Optional integrations:

- `GROQ_API_KEY`, `GROQ_MODEL`
- `GEMINI_API_KEY`, `GEMINI_MODEL`
- `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`
- `CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY`
- `INGEST_API_KEY`
- `ALLOWED_ORIGINS`
- `ENVIRONMENT`

The backend reads secrets only from environment variables. Secrets are not embedded in frontend assets or API responses.

## Vercel Deployment

1. Import the repository into Vercel.
2. Add production environment variables in the Vercel project settings. Use the variable names listed above; paste real values only into Vercel's encrypted environment settings.
3. Deploy with the repository’s `vercel.json` configuration.

Vercel serves the SPA from `frontend/` and routes `/api/*` to `api/index.py`. The scheduled endpoint is configured for the recovery task tick. WebSocket updates are replaced by the frontend polling fallback in serverless deployments.

## Main API Areas

- Authentication: `/api/auth/*`
- Cases and actions: `/api/cases/*`
- Webhooks and event ingestion: `/api/events/ingest`, `/api/webhooks/razorpay`
- AI evaluation: `/api/ai/*`
- Recommendations and approvals: `/api/cases/{case_id}/recommendations`, `/api/approvals/*`
- Provider verification and integrations: `/api/cases/{case_id}/verify-payment`, `/api/integrations/*`
- Dashboard and analytics: `/api/dashboard/*`, `/api/analytics/*`

## Development Checks

```powershell
\.venv\Scripts\python.exe -m pytest -q
\.venv\Scripts\python.exe -m compileall -q backend database
node --check frontend/app.js
git diff --check
```

## Security Notes

- Keep all secrets in local environment files or Vercel environment settings.
- `.env` is ignored and `.env.example` contains empty placeholders only.
- Do not paste credentials into README files, source code, frontend bundles, issue reports, or screenshots.
- Rotate any credential that has ever been committed or shared outside the secret manager.
