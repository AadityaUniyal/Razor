# RazorRescue — AI Revenue Recovery Orchestrator

> **Tagline:** *"Don't chase every failed payment. Understand it first."*  
> An enterprise-grade, closed-loop revenue recovery operations platform designed for modern payment infrastructure.

---

## 1. Executive Summary

RazorRescue is an intelligent, closed-loop revenue recovery operations platform designed for merchants using modern payment infrastructure like Razorpay. 

Traditional payment recovery systems operate on brute-force retry scripts:
```
Payment Failed ➔ Send Reminder ➔ Retry Payment ➔ Repeat until blocked
```
This naive chasing causes severe revenue leakage, high customer disturbance, and poor brand experience. Many payment failures are temporary gateway timeouts or late-authorized asynchronous bank transfers where recovery is completely unnecessary. In other cases, customers need to update their payment methods, provide specific promises (*"Kal salary aane ke baad kar dunga"*), or claim payment was already made.

**RazorRescue shifts the objective:**
$$\text{Maximize Net Recovery Value while Minimizing Unnecessary Customer Disturbance}$$

---

## 2. Core Architectural Design Principles

1. **LLMs Never Directly Control Financial Actions**:
   - The AI language model (Groq) reasons about customer communication tone, promise commitments, and classification proposals.
   - The **Deterministic Policy Engine** is the sole authority with approval power to dispatch messages or alter recovery workflows.
2. **Dual-Level Idempotency**:
   - **Event-Level Idempotency**: Webhooks are deduplicated by `external_event_id` with database unique constraints.
   - **Action-Level Idempotency**: Bounded customer communications and gateway verifications use unique action idempotency keys (`case_id:action_type:key`).
3. **Out-of-Order Terminal State Protection**:
   - Asynchronous late-arriving `payment.failed` events cannot reopen a verified `RECOVERED` terminal case.
4. **Transparent Strategy Scoring & Operational Cost Model**:
   - Evaluates interventions by expected net financial yield:
     $$\text{Strategy Score} = (\text{Gross Amount} \times P_{\text{recovery}}) - \text{Intervention Cost} - \text{Customer Disturbance Penalty} - \text{Risk Penalty}$$
   - Configurable channel costs:
     - Gateway State Verification: ₹0
     - Automated Waiting Window: ₹0
     - Email Reminder: ₹1
     - WhatsApp / Recovery Link: ₹2
     - SMS Notice: ₹3
     - Human Operator Escalation: ₹100
5. **Multi-lingual Hinglish & Temporal Date Resolution**:
   - Converts natural language promises (*"Bhai kal salary aane par kar dunga"*, *"Monday shaam tak"*) into structured UTC verification timestamps with grace windows.

---

## 3. High-Level Architecture Diagram

```
                 RAZORPAY / SIMULATED EVENTS
                              │
                              ▼
                      WEBHOOK INBOX (/api/webhooks/razorpay)
                              │
                              ▼
                       IDEMPOTENCY GATE (UNIQUE external_event_id)
                              │
                              ▼
                      EVENT NORMALIZER & STATE RECONCILER
                              │
                ┌─────────────┴─────────────┐
                ▼                           ▼
        PAYMENT VERIFICATION           CASE DATABASE (Neon Serverless PostgreSQL)
                │                           │
                └─────────────┬─────────────┘
                              ▼
                        CASE MANAGER
                              │
                              ▼
                       STRATEGY SCORER (ROI & Disturbance Penalties)
                              │
                              ▼
                    AI ANALYSIS (Groq LLM / Robust Fallback)
                              │
                              ▼
                    DETERMINISTIC POLICY ENGINE
                              │
                ┌─────────────┴─────────────┐
                ▼                           ▼
          ACTION BLOCKED             ACTION APPROVED
          (Limit Exceeded)                  │
                                            ▼
                                     ACTION EXECUTOR
                              ┌─────────────┼─────────────┐
                              ▼             ▼             ▼
                            WAIT         CONTACT       ESCALATE
                              │             │             │
                              └─────────────┼─────────────┘
                                            ▼
                                     OUTCOME VERIFIER
                                            │
                                            ▼
                                  IMMUTABLE DECISION LEDGER
                                            │
                               ┌────────────┴────────────┐
                               ▼                         ▼
                          DATABASE                  WEBSOCKET BROADCAST
                        (Source of Truth)           (/ws ➔ Live Dashboard)
```

---

## 4. Directory Structure & Architecture

The project is structured into clean, modular enterprise tiers:

```
razor/
├── api/                          # Vercel Serverless ASGI entrypoint
│   └── index.py                 # FastAPI serverless handler
├── backend/                      # API, orchestrator & domain services
│   ├── core/                    # Security, auth tokens, RBAC & environment config
│   │   ├── config.py            # Pydantic Settings & environment variables
│   │   └── security.py          # Clerk token verification & session validation
│   ├── routes/                  # API routers
│   │   ├── ai.py                # AI intent testing & sandbox endpoints
│   │   ├── auth.py              # Clerk sync, session status & RBAC roles
│   │   ├── cases.py             # Recovery cases, overrides & communications
│   │   ├── dashboard.py         # Real-time metrics, funnel & health telemetry
│   │   └── webhooks.py          # Gateway webhook receiver with HMAC validation
│   ├── services/                # Business logic & recovery services
│   │   ├── ai_agent.py          # Groq -> Gemini -> Rule-based AI agent
│   │   ├── policy_engine.py     # Deterministic safety guards & limits
│   │   ├── scheduler.py         # Autonomous case progression & tick runner
│   │   ├── strategy_scorer.py   # Multi-action financial ROI ranker
│   │   └── websocket_manager.py # Real-time state change broadcasting
│   └── main.py                  # Primary FastAPI application with lifespan management
├── database/                     # Cloud Neon PostgreSQL connection & ORM
│   ├── connection.py            # Managed session, connection pooling & health checks
│   ├── models.py                # Relational SQLAlchemy models & enum definitions
│   └── init_db.py               # Schema migrations & initial record seeding
├── frontend/                     # Enterprise operations console
│   ├── index.html               # Semantic HTML5 console template
│   ├── styles.css               # Clean enterprise design system
│   └── app.js                   # Reactive state, WebSockets & live polling controller
├── app.py                       # Root application entrypoint (uvicorn app:app)
├── vercel.json                  # Vercel deployment configuration & serverless routing
├── requirements.txt             # Minimal, pinned production dependencies
└── .env.example                 # Environment variables configuration template
```

> **Zero Local DB Files Policy:** All application state, audit trails, and recovery cases live exclusively in the managed **Neon PostgreSQL cloud database** (`postgresql+psycopg://...aws.neon.tech/neondb?sslmode=require`). No SQLite or local `.db` files are created or stored on disk.

---

## 5. Setup & Local Execution

### Prerequisites
- Python 3.11+
- Git

### 1. Clone & Install Dependencies
```powershell
git clone https://github.com/AadityaUniyal/Razor.git
cd Razor
python -m pip install -r requirements.txt
```

### 2. Configure Environment Variables
Copy `.env.example` to `.env`:
```powershell
Copy-Item .env.example .env
```
Default working credentials for the cloud database and AI fallback are pre-configured:
- **`DATABASE_URL`**: Neon PostgreSQL cloud connection string.
- **`GEMINI_API_KEY`**: Secondary resilient AI provider.
- **`GROQ_API_KEY`**: Optional primary high-speed reasoning API key.
- **`CLERK_PUBLISHABLE_KEY`**: Optional Clerk authentication public key.
- **`RAZORPAY_WEBHOOK_SECRET`**: HMAC-SHA256 signature verification secret.

### 3. Start the Web Application
```powershell
python -m uvicorn app:app --reload --port 8000
```
Open **`http://127.0.0.1:8000`** in your browser.

---

## 6. Vercel Serverless Deployment

RazorRescue is engineered for zero-friction deployment on [Vercel](https://vercel.com):

1. **Connect Repository**: Import `https://github.com/AadityaUniyal/Razor` in your Vercel Dashboard.
2. **Environment Variables**: Add the variables from `.env.example` under **Settings ➔ Environment Variables** in Vercel:
   - `DATABASE_URL`
   - `GROQ_API_KEY`
   - `GEMINI_API_KEY`
   - `CLERK_PUBLISHABLE_KEY`
   - `CLERK_SECRET_KEY`
   - `RAZORPAY_WEBHOOK_SECRET`
   - `SESSION_SECRET`
3. **Deploy**: Vercel automatically detects `vercel.json`, routes static assets from `/frontend`, and provisions `/api` as Python serverless functions.

---

## 7. Enterprise Operations Console Views

1. **Operations Overview**:
   - Real-time KPI cards: *Revenue at Risk*, *Revenue Recovered*, *Net Recovered Value*, *Recovery Rate*, *Unnecessary Interventions Prevented*, *Safe Policy Stops*.
   - 5-stage Recovery Funnel tracking conversion from raw payment event to captured revenue.
   - Live recovery activity feed with WebSocket updates and adaptive serverless polling fallback.
   - High-priority operator attention queue.

2. **Recovery Queue & Case Inspector**:
   - Filterable data grid with risk categorization, retry counts, state badges, and strategy scores.
   - Clicking a row opens the **Decision Explainer** drawer:
     - **Why this action?** Transparent breakdown of strategy scoring for all 5 actions.
     - Deterministic checklist verifying communication limits, retry counts, and opt-out state.
     - Bounded operator overrides (*Verify Gateway, Send Recovery Link, Escalate to Operator, Stop*).

3. **Promises to Pay**:
   - Dedicated commitment registry tracking promised settlement dates, confidence ratings, and automated follow-up status.

4. **AI Sandbox & Benchmark Matrix**:
   - Language testing sandbox supporting English-first intent detection and temporal extraction.
   - Displays parsed intent, extracted temporal expressions, resolved UTC timestamps, tokens, and latency.
   - Live benchmark evaluation table comparing AI accuracy against ground-truth scenarios.

5. **Decision Ledger & Audit Trail**:
   - Immutable historical record of every recovery decision and transition made by the system.

6. **Analytics & ROI**:
   - Revenue breakdown by failure category.
   - Transparent Net Recovery Value calculation deducting operational costs (Email ₹1, WhatsApp ₹2, SMS ₹3, Escalation ₹100, Verification ₹0).

7. **Policies & Health**:
   - Active recovery policy rules inspector.
   - Subsystem diagnostic panel monitoring Webhook Receiver, Database, AI Providers, WebSocket, and Background Scheduler health.
