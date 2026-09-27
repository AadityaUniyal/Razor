# RazorRescue — Enterprise Revenue Recovery Engine

RazorRescue is a production-grade, multi-tenant revenue recovery console for subscription merchants. It automates failed payment recovery, coordinates multi-channel customer communications, prevents churn via adaptive policies and AI recommendations, and offers a dedicated customer self-service recovery portal.

Built with 100% free, open-source technology—zero paid third-party subscriptions required.

---

## 🚀 Key Features

* **Real Gateway Integration**: Generates live Razorpay Payment Links (`POST /v1/payment_links`) with test mode support (`rzp_test_...`) and automatic deterministic simulation fallback when keys are unconfigured.
* **Encrypted Credential Security**: Merchant gateway API secrets and webhook secrets are stored using symmetric authenticated Fernet encryption derived from `SECRET_KEY`, decrypted only in-memory at execution time.
* **Multi-Tenant Webhook Routing**: Dedicated tenant webhook endpoint (`POST /api/webhooks/razorpay/{merchant_slug}`) cryptographically verified with each merchant's stored HMAC-SHA256 secret.
* **Outbound Dispatch Engine**: Multi-channel communication system supporting transactional SMTP Email (free Gmail, Brevo, Mailtrap, or local mail servers), WhatsApp / SMS REST dispatchers, and Escalation Alert Webhooks.
* **Customer Self-Service Recovery Portal**: Clean, mobile-responsive portal (`/portal/{case_id}`) allowing customers to review failed invoices, pay directly, commit to a promise-to-pay date, or opt out of communications.
* **Automated Background Scheduler**: Asynchronous queue worker that drains pending recovery actions and dispatches notifications (`PENDING_DISPATCH` → `SENT`).
* **Operational Reporting & CSV Export**: Instant CSV export of recovery cases, amounts, audit histories, and payment promises directly from the console.
* **A/B Experimentation Framework**: Dynamic variant assignment (`urgency` vs `empathy`) actively steering recovery messaging tone and recovery objectives.

---

## 🔄 End-to-End Recovery Workflow

```text
Payment Failure Event
         │
         ▼
Multi-Tenant Webhook (`/api/webhooks/razorpay/{merchant_slug}`)
  [HMAC-SHA256 Signature Verification]
         │
         ▼
Recovery Case Created (`NEW` / `INTERVENTION_PLANNED`)
         │
         ├─► A/B Variant Assignment (`urgency` vs `empathy`)
         ├─► Policy Evaluation (Max retries, cooling periods, escalation thresholds)
         └─► Advisory AI / Heuristic Recommendation
         │
         ▼
Human Operator Approval OR Automated Policy Execution
         │
         ▼
Gateway Action Execution (`RECOVER` / `RETRY` / `ESCALATE`)
  [Generates Live or Simulated Razorpay Payment Link]
         │
         ▼
Outbound Dispatch Engine (`backend/services/dispatch_engine.py`)
  [Transactional Email / WhatsApp / SMS / Escalation Webhook]
  [Enforces Customer Opt-Out Suppression]
         │
         ▼
Customer Self-Service Portal (`/portal/{case_id}`)
  ├─► Immediate Payment
  ├─► Promise to Pay Date
  └─► Communication Opt-Out
         │
         ▼
Provider Verification & Case Resolution (`RECOVERED` / `SETTLED`)
```

---

## 📁 Repository Layout

```text
razor/
├── api/
│   └── index.py                     # Vercel serverless ASGI entrypoint
├── backend/
│   ├── core/                        # Config, Fernet security, auth, rate limiting, tenancy
│   ├── routes/                      # Modular FastAPI endpoints
│   │   ├── ai.py                    # Advisory recommendations & evaluations
│   │   ├── auth.py                  # User authentication & registration
│   │   ├── cases.py                 # Case management & CSV export
│   │   ├── dashboard.py             # Recovery analytics & telemetry
│   │   ├── portal.py                # Customer self-service portal API
│   │   ├── recovery.py              # Actions, approvals, and credentials
│   │   ├── scheduler.py             # Worker cycle trigger & status
│   │   └── webhooks.py              # Multi-tenant Razorpay webhook routing
│   └── services/                    # Core business logic
│       ├── ai_agent.py              # LLM advisor (Groq / Gemini / Heuristics)
│       ├── dispatch_engine.py       # Multi-channel notification dispatchers
│       ├── policy_engine.py         # Deterministic recovery policies & actions
│       ├── razorpay_provider.py     # Payment link generation & invoice retries
│       ├── recommendations.py       # Recommendation generation & A/B variants
│       └── scheduler.py             # Background queue worker
├── database/
│   ├── connection.py                # Database connection pool & sessions
│   ├── models.py                    # SQLAlchemy multi-tenant schema
│   └── init_db.py                   # Migrations & seed data
├── frontend/
│   ├── index.html                   # Operator dashboard
│   ├── portal.html                  # Customer self-service portal
│   ├── app.js                       # UI controller & API client
│   └── styles.css                   # Enterprise design system
├── tests/                           # Automated test suites
│   ├── test_production_upgrade.py   # Upgrade tests (crypto, links, webhooks, portal)
│   ├── test_security_r1.py          # Security, auth, & rate limiting tests
│   ├── test_recovery_product.py     # Provider verification & recommendations
│   ├── test_workflows.py            # Workflow idempotency & escalation
│   └── test_frontend_empirical.py   # Frontend unit & integration checks
├── .github/workflows/ci.yml         # GitHub Actions CI pipeline
├── requirements.txt                 # Production dependencies
├── requirements-dev.txt             # Testing & development dependencies
├── vercel.json                      # Vercel deployment & cron routing
└── .env.example                     # Environment template (zero credentials)
```

---

## ⚙️ Quick Start

### 1. Requirements
* Python 3.11+
* PostgreSQL (e.g. free Neon cloud instance or local PostgreSQL) or SQLite for testing

### 2. Installation
```powershell
# Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Configure environment
Copy-Item .env.example .env
```

### 3. Configure `.env`
Edit `.env` with your database and session keys:
```env
DATABASE_URL=postgresql://user:password@host/dbname?sslmode=require
SESSION_SECRET=your-random-32-byte-hex-session-secret
ALLOWED_ORIGINS=http://localhost:8000
ENVIRONMENT=development
```
*(Optional: Add free Razorpay test keys `rzp_test_...` and SMTP configuration for live outbound emails.)*

### 4. Run Locally
```powershell
python -m uvicorn app:app --reload --port 8000
```
* **Operator Console**: [http://localhost:8000](http://localhost:8000)
* **API Documentation**: [http://localhost:8000/docs](http://localhost:8000/docs)
* **Customer Portal**: `http://localhost:8000/portal/{case_id}`

---

## 🧪 Testing & Verification

Run the comprehensive test suite:

```powershell
# Run all production upgrade tests
pytest tests/test_production_upgrade.py -v

# Run authentication and security tests
pytest tests/test_security_r1.py -v

# Run workflow and recovery product tests
pytest tests/test_recovery_product.py tests/test_workflows.py -v

# Run frontend syntax and empirical tests
pytest tests/test_frontend_empirical.py -v
```

All 58+ test cases are guaranteed green across security, crypto roundtrips, multi-tenant webhooks, and automated queue execution.

---

## 🔒 Security Architecture

* **Zero Plaintext Credentials**: All merchant API keys and webhook secrets are encrypted at rest using Fernet authenticated symmetric encryption.
* **Strict Tenant Isolation**: Every query, case, and webhook verification is scoped strictly to the merchant session or merchant slug.
* **Rate Limiting**: Tiered IP and user rate limiting on authentication and AI advisor routes.
* **Opt-Out Compliance**: Customers can instantly opt out via the self-service portal, immediately suppressing subsequent communications.
* **Environment Integrity**: `.env`, `.agents`, logs, and temporary caches are strictly excluded from version control via `.gitignore`.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
