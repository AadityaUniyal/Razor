// ==============================================================================
// RAZORRESCUE FRONTEND APPLICATION
// ==============================================================================

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

const state = {
  view: "overview",
  cases: [],
  selectedCaseId: null,
  user: null,
  summary: {},
};

// Utilities
const money = (n) =>
  new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(n || 0);

const esc = (s) =>
  String(s || "").replace(
    /[&<>'"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[c])
  );

const formatDateTime = (iso) => {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString("en-IN", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: true,
    });
  } catch {
    return iso;
  }
};

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (window.Clerk && window.Clerk.session && !headers["Authorization"]) {
    try {
      const token = await window.Clerk.session.getToken();
      if (token) {
        headers["Authorization"] = `Bearer ${token}`;
      }
    } catch {}
  }
  if (opts.body && typeof opts.body === "object" && !(opts.body instanceof URLSearchParams)) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, { credentials: "include", ...opts, headers });
  if (!res.ok) {
    let errText = await res.text();
    try {
      const errObj = JSON.parse(errText);
      errText = errObj.detail || errText;
    } catch {}
    throw new Error(errText);
  }
  return res.json();
}

function setTemplate(name) {
  const t = $(`#${name}Template`);
  if (!t) return;
  $("#view").replaceChildren(t.content.cloneNode(true));
}

function renderMetricCard(label, val, hint = "", highlightClass = "") {
  return `
    <div class="metric-card">
      <div class="metric-label">${label}</div>
      <div class="metric-val ${highlightClass}">${val}</div>
      <div class="metric-hint">${hint}</div>
    </div>
  `;
}

// ==============================================================================
// VIEW RENDERERS
// ==============================================================================

// 1. OVERVIEW
async function renderOverview() {
  setTemplate("overview");

  const [s, activity, cases] = await Promise.all([
    api("/api/dashboard/summary"),
    api("/api/dashboard/activity"),
    api("/api/cases"),
  ]);

  state.summary = s;
  state.cases = cases;

  // Metric Cards
  $("#stats").innerHTML =
    renderMetricCard("Revenue at Risk", money(s.total_at_risk), "Active exposure across queue") +
    renderMetricCard("Revenue Recovered", money(s.total_recovered), "Verified payment settlements", "highlight-green") +
    renderMetricCard("Net Recovered Value", money(s.net_recovery_value), `Gross minus ops costs (${money(s.total_intervention_costs)})`, "highlight-blue") +
    renderMetricCard("Recovery Rate", `${s.recovery_rate}%`, `${s.total_cases} total cases tracked`) +
    renderMetricCard("Unnecessary Actions Prevented", money(s.unnecessary_interventions_prevented), "Temporary failures resolved safely", "highlight-green") +
    renderMetricCard("Policy Safe Stops", s.safe_stop_count, "Opt-outs & retry limits enforced");

  // Funnel
  const funnelSteps = [
    ["1. Events Received", s.total_cases + 4],
    ["2. Cases Created", s.total_cases],
    ["3. Verified / Decided", s.total_cases],
    ["4. Interventions Avoided", money(s.unnecessary_interventions_prevented)],
    ["5. Revenue Recovered", money(s.total_recovered)],
  ];
  $("#funnel").innerHTML = funnelSteps
    .map(
      ([label, val]) => `
      <div class="funnel-step">
        <span>${label}</span>
        <strong>${val}</strong>
      </div>
    `
    )
    .join("");

  // Live Activity Feed
  $("#activity").innerHTML =
    activity.slice(0, 8).map((a) => `
      <div class="timeline-item">
        <span class="timeline-time">${formatDateTime(a.created_at)}</span>
        <div class="timeline-body">
          <b>${esc(a.case_id)} · ${esc(a.event_type)}</b>
          <p>${esc(a.message || "Payment event processed")}</p>
        </div>
      </div>
    `).join("") || `<p style="font-size:12px; color:var(--text-muted);">No activity recorded yet.</p>`;

  // Attention Queue
  const needingAttention = cases.filter((c) => !c.recovered && c.state !== "STOP").slice(0, 5);
  $("#attentionQueue").innerHTML = needingAttention.length
    ? `
      <div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>Case ID</th>
              <th>Customer</th>
              <th>Amount</th>
              <th>Category</th>
              <th>State</th>
              <th>Current Action</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            ${needingAttention
              .map(
                (c) => `
              <tr class="clickable" data-open-case="${c.case_id}">
                <td><b>${c.case_id}</b></td>
                <td>${esc(c.customer_name)}</td>
                <td>${money(c.amount)}</td>
                <td><span style="font-size:11px;">${esc(c.failure_category)}</span></td>
                <td><span class="badge ${c.state.toLowerCase()}">${c.state}</span></td>
                <td><b>${esc(c.current_action)}</b></td>
                <td><button class="btn-secondary" style="padding:4px 10px; font-size:11px;" data-open-case="${c.case_id}">Inspect</button></td>
              </tr>
            `
              )
              .join("")}
          </tbody>
        </table>
      </div>
    `
    : `<p style="font-size:13px; color:var(--text-muted);">All active recovery cases are currently resolved or stopped.</p>`;
}

// 2. RECOVERY QUEUE & CASE DETAIL INSPECTOR
async function renderQueue() {
  setTemplate("queue");
  state.cases = await api("/api/cases");
  state.queueFilter = state.queueFilter || "ALL";
  state.queueSearch = state.queueSearch || "";

  function applyQueueFilters() {
    let filtered = state.cases;
    if (state.queueFilter !== "ALL") {
      filtered = filtered.filter((c) => c.state.toUpperCase() === state.queueFilter);
    }
    if (state.queueSearch) {
      const q = state.queueSearch.toLowerCase();
      filtered = filtered.filter(
        (c) =>
          c.case_id.toLowerCase().includes(q) ||
          c.customer_name.toLowerCase().includes(q) ||
          c.customer_email.toLowerCase().includes(q) ||
          c.failure_category.toLowerCase().includes(q)
      );
    }

    $("#queueCount").textContent = `${filtered.length} Cases`;

    const tbody = $("#queueTableBody");
    tbody.innerHTML = filtered.length
      ? filtered
          .map(
            (c) => `
            <tr class="clickable" data-open-case="${c.case_id}">
              <td><b>${c.case_id}</b></td>
              <td>${esc(c.customer_name)}<br><small style="color:var(--text-sub);">${esc(c.customer_email)}</small></td>
              <td><b>${money(c.amount)}</b></td>
              <td><span style="font-size:11px; font-family:'JetBrains Mono', monospace;">${esc(c.failure_category)}</span></td>
              <td><span class="badge ${c.state.toLowerCase()}">${c.state}</span></td>
              <td>${c.retry_count} retries / ${c.communication_count} comms</td>
              <td><b>${esc(c.current_action)}</b></td>
              <td><span style="font-weight:700; color:var(--primary);">${c.strategy_score || 0}</span></td>
              <td>${formatDateTime(c.updated_at)}</td>
            </tr>
          `
          )
          .join("")
      : `<tr><td colspan="9" style="text-align:center; padding:32px; color:var(--text-muted);">No cases match the selected filter.</td></tr>`;
  }

  applyQueueFilters();

  // Wire search input
  const searchInput = $("#queueSearchInput");
  if (searchInput) {
    searchInput.value = state.queueSearch;
    searchInput.oninput = (e) => {
      state.queueSearch = e.target.value.trim();
      applyQueueFilters();
    };
  }

  // Wire filter pills
  $$("#queueFilterPills .filter-pill").forEach((pill) => {
    pill.classList.toggle("active", pill.dataset.filter === state.queueFilter);
    pill.onclick = () => {
      $$("#queueFilterPills .filter-pill").forEach((p) => p.classList.remove("active"));
      pill.classList.add("active");
      state.queueFilter = pill.dataset.filter;
      applyQueueFilters();
    };
  });

  if (state.selectedCaseId) {
    await showCaseInspector(state.selectedCaseId);
  }
}

async function showCaseInspector(caseId) {
  state.selectedCaseId = caseId;
  const data = await api(`/api/cases/${caseId}`);
  const c = data.case;
  const latestDecision = data.decisions[0] || {};

  $("#caseDetailDrawer").classList.remove("hidden");
  $("#detailCaseId").textContent = `CASE INSPECTOR / ${c.case_id}`;
  $("#detailHeaderTitle").innerHTML = `${money(c.amount)} <span class="badge ${c.state.toLowerCase()}">${c.state}</span>`;

  // Explainer Card
  const explainerCard = $("#decisionExplainerCard");
  const isBlocked = latestDecision.policy_result === "BLOCKED";
  explainerCard.className = `decision-explainer-card ${isBlocked ? "blocked" : ""}`;
  $("#explainerPolicyResult").textContent = latestDecision.policy_result || "APPROVED";
  $("#explainerPolicyResult").className = `badge ${isBlocked ? "escalated" : "recovered"}`;
  $("#explainerReasonText").textContent =
    latestDecision.policy_reason || "Selected via deterministic recovery strategy scoring.";

  // Strategy Scores Grid
  const scoresGrid = $("#strategyScoresGrid");
  if (latestDecision.strategy_scores) {
    scoresGrid.innerHTML = Object.entries(latestDecision.strategy_scores)
      .map(([act, details]) => {
        const isSelected = act === latestDecision.selected_action;
        return `
          <div class="strategy-score-chip ${isSelected ? "selected" : ""}">
            <span>${act}</span>
            <strong>${details.score}</strong>
            <small style="color:var(--text-sub);">${Math.round(details.expected_probability * 100)}% prob</small>
          </div>
        `;
      })
      .join("");
  } else {
    scoresGrid.innerHTML = `<span style="color:var(--text-muted); font-size:12px;">Strategy scores evaluated deterministically.</span>`;
  }

  // Deterministic Factors
  const factorsList = $("#deterministicFactorsList");
  const factors = latestDecision.deterministic_factors || {};
  factorsList.innerHTML = `
    <div class="factor-item ${c.recovered ? "pass" : "pass"}">
      ${c.recovered ? "✓" : "•"} Payment verified state: <b>${c.recovered ? "CAPTURED" : "PENDING"}</b>
    </div>
    <div class="factor-item ${c.communication_count < 3 ? "pass" : "fail"}">
      ${c.communication_count < 3 ? "✓" : "✗"} Communication limit check: ${c.communication_count}/3 used
    </div>
    <div class="factor-item ${c.retry_count < 3 ? "pass" : "fail"}">
      ${c.retry_count < 3 ? "✓" : "✗"} Automated retry limit check: ${c.retry_count}/3 used
    </div>
    <div class="factor-item ${!factors.customer_opted_out ? "pass" : "fail"}">
      ${!factors.customer_opted_out ? "✓" : "✗"} Customer opt-out status: ${factors.customer_opted_out ? "OPTED OUT" : "ACTIVE"}
    </div>
  `;

  // Profile Details
  $("#detailCustName").textContent = c.customer_name;
  $("#detailCustEmail").textContent = c.customer_email;
  $("#detailAmount").textContent = money(c.amount);
  $("#detailNetVal").textContent = money(c.net_recovery_value);
  $("#detailCategory").textContent = c.failure_category;
  $("#detailPromiseTime").textContent = c.promise_time ? formatDateTime(c.promise_time) : "None scheduled";

  // Action Records
  const actionList = $("#detailActionRecords");
  actionList.innerHTML = data.actions.length
    ? data.actions
        .map(
          (a) => `
        <div style="padding:8px 0; border-bottom:1px solid var(--border); font-size:12px;">
          <b>${esc(a.action_type)}</b> · <span class="badge ${a.status.toLowerCase()}">${a.status}</span>
          <span style="float:right; color:var(--text-sub);">${money(a.cost)} cost</span>
          <p style="color:var(--text-muted); margin-top:2px;">${formatDateTime(a.created_at)}</p>
        </div>
      `
        )
        .join("")
    : `<p style="font-size:12px; color:var(--text-muted);">No recovery actions executed yet.</p>`;

  // Timeline
  const timelineList = $("#detailTimeline");
  timelineList.innerHTML = data.events
    .slice()
    .reverse()
    .slice(0, 6)
    .map(
      (e) => `
      <div class="timeline-item">
        <span class="timeline-time">${formatDateTime(e.created_at)}</span>
        <div class="timeline-body">
          <b>${esc(e.event_type)}</b>
          <p>${esc(e.message)}</p>
        </div>
      </div>
    `
    )
    .join("");

  // Operator Action Handlers
  $("#btnActionVerify").onclick = () => executeOperatorAction(caseId, "VERIFY");
  $("#btnActionLink").onclick = () => {
    executeOperatorAction(caseId, "RECOVER");
    showRzpToast("Recovery Link Dispatched", `Secure payment recovery link sent to ${c.customer_email}`, "success");
  };
  $("#btnActionEscalate").onclick = () => executeOperatorAction(caseId, "ESCALATE");
  $("#btnActionCapture").onclick = () => executeOperatorAction(caseId, "CAPTURE_PAYMENT");
  $("#btnActionStop").onclick = () => executeOperatorAction(caseId, "STOP");
}

// ==============================================================================
// FINTECH TOAST NOTIFICATION SYSTEM
// ==============================================================================

function showRzpToast(title, desc, type = "success") {
  const container = $("#toastContainer");
  if (!container) return;

  const toast = document.createElement("div");
  toast.className = `rzp-toast ${type}`;
  toast.innerHTML = `
    <div class="toast-icon">
      ${type === "success" 
        ? `<svg viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>`
        : `<svg viewBox="0 0 24 24" fill="none" stroke="#0b69ff" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`}
    </div>
    <div>
      <div class="toast-title">${esc(title)}</div>
      <div class="toast-desc">${esc(desc)}</div>
    </div>
  `;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateY(16px)";
    setTimeout(() => toast.remove(), 300);
  }, 4500);
}

async function executeOperatorAction(caseId, action) {
  try {
    await api(`/api/cases/${caseId}/action`, {
      method: "POST",
      body: { action, note: "Operator triggered from Case Inspector" },
    });
    showRzpToast("Action Executed", `Triggered ${action} for case ${caseId}`, "success");
    await showCaseInspector(caseId);
    state.cases = await api("/api/cases");
    renderQueue();
  } catch (err) {
    showRzpToast("Action Failed", err.message, "error");
  }
}


// 4. PROMISES TO PAY
async function renderPromises() {
  setTemplate("promises");
  const promises = await api("/api/promises");
  $("#promiseCountBadge").textContent = `${promises.length} Tracked`;

  const tbody = $("#promisesTableBody");
  tbody.innerHTML = promises.length
    ? promises
        .map(
          (p) => `
        <tr>
          <td><b>${p.case_id}</b></td>
          <td>${esc(p.customer)}</td>
          <td><b>${money(p.amount)}</b></td>
          <td style="max-width:280px; font-style:italic;">"${esc(p.promise_text)}"</td>
          <td><b>${formatDateTime(p.promised_at)}</b></td>
          <td><span style="font-family:'JetBrains Mono', monospace; font-weight:700;">${Math.round((p.confidence || 0.9) * 100)}%</span></td>
          <td><span class="badge ${p.status === "FULFILLED" ? "recovered" : p.status === "FOLLOW_UP_SENT" ? "escalated" : "wait"}">${p.status}</span></td>
          <td>${p.follow_up_sent ? "Yes (1 follow-up)" : "No (Grace period)"}</td>
        </tr>
      `
        )
        .join("")
    : `<tr><td colspan="8" style="text-align:center; padding:30px; color:var(--text-muted);">No pending customer payment promises recorded.</td></tr>`;
}

// 5. AI SANDBOX & BENCHMARKS
async function renderAI() {
  setTemplate("ai");

  const metrics = await api("/api/ai/evaluation-metrics");
  $("#aiAccuracyBadge").textContent = `Benchmark Accuracy: ${metrics.accuracy_percent}%`;
  $("#metricPromiseRate").textContent = `${metrics.promise_detection_rate}%`;
  $("#metricPolicyAccept").textContent = `${metrics.policy_acceptance_rate}%`;

  $("#aiBenchmarkTableBody").innerHTML = metrics.benchmark_runs
    .map(
      (b) => `
      <tr>
        <td style="font-size:12px;">"${esc(b.message)}"</td>
        <td><span class="badge stop">${b.expected_intent}</span></td>
        <td><span class="badge ${b.matched ? "recovered" : "escalated"}">${b.predicted_intent}</span></td>
        <td><b>${Math.round(b.confidence * 100)}%</b></td>
        <td>${b.matched ? '<span style="color:#059669; font-weight:700;">✓ Pass</span>' : '<span style="color:#b91c1c; font-weight:700;">✗ Fail</span>'}</td>
      </tr>
    `
    )
    .join("");
}

// 6. DECISION LEDGER
async function renderAudit() {
  setTemplate("audit");
  const ledger = await api("/api/audit");

  $("#auditTableBody").innerHTML = ledger.length
    ? ledger
        .map(
          (d) => `
        <tr>
          <td style="font-family:'JetBrains Mono', monospace; font-size:11px;">${formatDateTime(d.created_at)}</td>
          <td><b>${d.case_id}</b></td>
          <td><b>${d.selected_action}</b></td>
          <td><span class="badge ${d.policy_result === "APPROVED" ? "recovered" : "escalated"}">${d.policy_result}</span></td>
          <td style="max-width:320px;">${esc(d.policy_reason)}</td>
          <td><span style="font-size:11px;">${d.ai_analysis?.intent || "DETERMINISTIC"}</span></td>
        </tr>
      `
        )
        .join("")
    : `<tr><td colspan="6" style="text-align:center; padding:30px; color:var(--text-muted);">No decision ledger entries available.</td></tr>`;
}

// 7. ANALYTICS & ROI
async function renderAnalytics() {
  setTemplate("analytics");
  const [metrics, summary] = await Promise.all([api("/api/dashboard/metrics"), api("/api/dashboard/summary")]);

  $("#analyticsStatsGrid").innerHTML =
    renderMetricCard("Recovered Cases", metrics.recovered_cases, "Closed successfully", "highlight-green") +
    renderMetricCard("Unnecessary Interventions Prevented", money(summary.unnecessary_interventions_prevented), "Customer goodwill preserved", "highlight-green") +
    renderMetricCard("Human Escalations", metrics.escalated_cases, "High-value cases") +
    renderMetricCard("Net Recovered Value", money(summary.net_recovery_value), `Revenue minus ₹${summary.total_intervention_costs} ops costs`, "highlight-blue");

  // Category breakdown bars
  const maxVal = Math.max(1, ...Object.values(metrics.by_category).map((c) => c.at_risk + c.recovered));
  $("#analyticsCategoryBars").innerHTML = Object.entries(metrics.by_category)
    .map(([cat, val]) => {
      const total = val.at_risk + val.recovered;
      const pct = Math.round((total / maxVal) * 100);
      return `
        <div style="margin-bottom:14px;">
          <div style="display:flex; justify-content:space-between; font-size:12px; margin-bottom:4px;">
            <b>${cat}</b>
            <span>${money(val.recovered)} recovered / ${money(total)}</span>
          </div>
          <div style="height:10px; background:#e2e8f0; border-radius:99px; overflow:hidden;">
            <div style="height:100%; width:${pct}%; background:var(--brand-blue); border-radius:99px;"></div>
          </div>
        </div>
      `;
    })
    .join("");

  $("#analyticsRoiSummary").innerHTML = `
    <p><b>Gross Revenue Recovered:</b> ${money(summary.total_recovered)}</p>
    <p><b>Operational Intervention Costs:</b> ${money(summary.total_intervention_costs)}</p>
    <p><b>Net Revenue Added to Merchant:</b> <span style="font-size:16px; font-weight:800; color:#059669;">${money(summary.net_recovery_value)}</span></p>
    <p style="color:var(--text-muted); font-size:12px; margin-top:8px;">
      Intervention Cost Model: Verification = ₹0, Email = ₹1, WhatsApp = ₹2, SMS = ₹3, Human Escalation = ₹100.
    </p>
  `;
}

// 8. SETTINGS & HEALTH
async function renderSettings() {
  setTemplate("settings");
  const [policies, health] = await Promise.all([api("/api/policies"), api("/api/health")]);
  const activePolicy = policies[0] || {};

  $("#policyVersionBadge").textContent = activePolicy.version || "v1.2";
  $("#policyConfigList").innerHTML = activePolicy.configuration
    ? Object.entries(activePolicy.configuration)
        .map(([k, v]) => `
          <div style="display:flex; justify-content:space-between; padding:10px 0; border-bottom:1px solid var(--border); font-size:13px;">
            <span style="color:var(--text-muted);">${k.replace(/_/g, " ")}</span>
            <b>${typeof v === "object" ? JSON.stringify(v) : v}</b>
          </div>
        `)
        .join("")
    : "No policy found.";

  $("#systemHealthDetails").innerHTML = Object.entries(health)
    .filter(([k]) => k !== "recent_events")
    .map(([k, v]) => `
      <div style="display:flex; justify-content:space-between; padding:10px 0; border-bottom:1px solid var(--border); font-size:13px;">
        <span style="color:var(--text-muted);">${k.replace(/_/g, " ")}</span>
        <b style="font-family:'JetBrains Mono', monospace;">${v}</b>
      </div>
    `)
    .join("");
}

// ==============================================================================
// NAVIGATION & ROUTING
// ==============================================================================

async function navigate(viewName) {
  state.view = viewName;
  $("#crumb").textContent = `OPERATIONS / ${viewName.toUpperCase()}`;
  $("#pageTitle").textContent = {
    overview: "Operations Overview",
    queue: "Recovery Queue",
    promises: "Promises to Pay",
    ai: "AI Sandbox & Evaluation",
    audit: "Decision Ledger",
    analytics: "Analytics & Net Recovery ROI",
    settings: "Policies & System Health",
  }[viewName];

  $$("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === viewName));

  const renderers = {
    overview: renderOverview,
    queue: renderQueue,
    promises: renderPromises,
    ai: renderAI,
    audit: renderAudit,
    analytics: renderAnalytics,
    settings: renderSettings,
  };

  const fn = renderers[viewName];
  if (fn) await fn();
}

// ==============================================================================
// GLOBAL EVENT LISTENERS
// ==============================================================================

document.body.addEventListener("click", async (e) => {
  // Nav Click
  const navBtn = e.target.closest("[data-view]");
  if (navBtn) {
    await navigate(navBtn.dataset.view);
    return;
  }

  // Row open case
  const caseRow = e.target.closest("[data-open-case]");
  if (caseRow) {
    const caseId = caseRow.dataset.openCase;
    if (state.view !== "queue") {
      state.selectedCaseId = caseId;
      await navigate("queue");
    } else {
      await showCaseInspector(caseId);
    }
    return;
  }

  // Close inspector
  if (e.target.id === "closeDetailBtn") {
    $("#caseDetailDrawer").classList.add("hidden");
    state.selectedCaseId = null;
    return;
  }

  // Sim message chips in AI Sandbox
  const aiChip = e.target.closest(".sim-chip[data-aimsg]");
  if (aiChip) {
    $("#aiMessageInput").value = aiChip.dataset.aimsg;
    return;
  }

});

// Forms
document.body.addEventListener("submit", async (e) => {
  // Login
  if (e.target.id === "loginForm") {
    e.preventDefault();
    try {
      const data = new FormData(e.target);
      await api("/api/auth/login", {
        method: "POST",
        body: new URLSearchParams(data),
      });
      await bootApp();
    } catch (err) {
      showRzpToast("Login Failed", err.message, "error");
    }
  }

  // Sign Up
  if (e.target.id === "signUpForm") {
    e.preventDefault();
    const data = new FormData(e.target);
    await api("/api/register", { method: "POST", body: new URLSearchParams(data) });
    await bootApp();
  }

  // AI Sandbox Form
  if (e.target.id === "aiSandboxForm") {
    e.preventDefault();
    const msg = $("#aiMessageInput").value;
    const provider = $("#aiProviderSelect") ? $("#aiProviderSelect").value : "auto";
    const resultBox = $("#aiResultJson");
    const telemetryBox = $("#aiTelemetryBadge");
    resultBox.textContent = `Analyzing intent with ${provider.toUpperCase()} AI...`;
    if (telemetryBox) telemetryBox.textContent = "Processing...";

    try {
      const res = await api("/api/ai/sandbox", {
        method: "POST",
        body: { message: msg, provider: provider },
      });
      resultBox.textContent = JSON.stringify(res, null, 2);
      if (telemetryBox) {
        telemetryBox.innerHTML = `<span style="color:#059669;">● ${res.provider_used}</span> | Latency: <b>${res.latency_ms}ms</b> | Tokens: <b>${res.tokens ? res.tokens.total_tokens : 'N/A'}</b>`;
      }
    } catch (err) {
      resultBox.textContent = `Error: ${err.message}`;
      if (telemetryBox) telemetryBox.textContent = "Error";
    }
  }
});

// Logout
$("#logoutBtn").onclick = async () => {
  if (window.Clerk && window.Clerk.user) {
    try {
      await window.Clerk.signOut();
    } catch {}
  }
  await api("/api/auth/logout", { method: "POST", body: {} });
  location.reload();
};

// ==============================================================================
// REAL-TIME UPDATES (WEBSOCKET + ADAPTIVE SERVERLESS POLLING FALLBACK)
// ==============================================================================

let livePollingTimer = null;
let wsAttempts = 0;

function startLivePolling() {
  if (livePollingTimer) return;
  $("#systemHealthBadge").className = "status-badge healthy";
  $("#systemHealthBadge").innerHTML = `<span class="pulse-dot"></span> CLOUD LIVE (ADAPTIVE)`;

  livePollingTimer = setInterval(async () => {
    try {
      // Trigger on-demand task processing tick
      api("/api/scheduler/tick").catch(() => {});

      if (state.view === "overview") {
        const s = await api("/api/dashboard/summary");
        state.summary = s;
        $("#stats").innerHTML =
          renderMetricCard("Revenue at Risk", money(s.total_at_risk), "Active exposure across queue") +
          renderMetricCard("Revenue Recovered", money(s.total_recovered), "Verified payment settlements", "highlight-green") +
          renderMetricCard("Net Recovered Value", money(s.net_recovery_value), `Gross minus ops costs (${money(s.total_intervention_costs)})`, "highlight-blue") +
          renderMetricCard("Recovery Rate", `${s.recovery_rate}%`, `${s.total_cases} total cases tracked`) +
          renderMetricCard("Unnecessary Actions Prevented", money(s.unnecessary_interventions_prevented), "Temporary failures resolved safely", "highlight-green") +
          renderMetricCard("Policy Safe Stops", s.safe_stop_count, "Opt-outs & retry limits enforced");
      } else if (state.view === "queue") {
        state.cases = await api("/api/cases");
        const tbody = $("#queueTableBody");
        if (tbody) {
          // Re-render table if not inspecting
          const searchInput = $("#queueSearchInput");
          if (searchInput && !searchInput.value) {
            $("#queueCount").textContent = `${state.cases.length} Cases`;
          }
        }
      }
    } catch {}
  }, 3500);
}

function connectWebSocket() {
  if (location.hostname.includes("vercel.app")) {
    // Vercel serverless environment does not support persistent WebSockets
    startLivePolling();
    return;
  }

  const protocol = location.protocol === "https:" ? "wss" : "ws";
  let ws = null;
  try {
    ws = new WebSocket(`${protocol}://${location.host}/ws`);
  } catch {
    startLivePolling();
    return;
  }

  ws.onopen = () => {
    wsAttempts = 0;
    $("#systemHealthBadge").className = "status-badge healthy";
    $("#systemHealthBadge").innerHTML = `<span class="pulse-dot"></span> ALL SYSTEMS OPERATIONAL`;
  };

  ws.onmessage = async (event) => {
    try {
      const data = JSON.parse(event.data);
      if (data.type === "CASE_UPDATED") {
        if (state.view === "overview") {
          renderOverview();
        } else if (state.view === "queue") {
          state.cases = await api("/api/cases");
          renderQueue();
        }
      }
    } catch {}
  };

  ws.onclose = () => {
    wsAttempts++;
    if (wsAttempts >= 2) {
      // Gracefully switch to adaptive polling on disconnect
      startLivePolling();
    } else {
      setTimeout(connectWebSocket, 2500);
    }
  };

  ws.onerror = () => {
    ws.close();
  };
}

// ==============================================================================
// AUTHENTICATION & LOGIN UI HANDLERS
// ==============================================================================

function simpleTabSwitcher() {
  const tabSignIn = $("#tabSignIn");
  const tabSignUp = $("#tabSignUp");
  const signInForm = $("#signInForm");
  const signUpForm = $("#signUpForm");

  function switchTo(mode) {
    if (mode === "signin") {
      tabSignIn?.classList.add("active");
      tabSignUp?.classList.remove("active");
      signInForm?.classList.remove("hidden");
      signUpForm?.classList.add("hidden");
    } else {
      tabSignUp?.classList.add("active");
      tabSignIn?.classList.remove("active");
      signUpForm?.classList.remove("hidden");
      signInForm?.classList.add("hidden");
    }
  }

  if (tabSignIn && tabSignUp) {
    tabSignIn.onclick = () => switchTo("signin");
    tabSignUp.onclick = () => switchTo("signup");
  }

  // Initialize to Sign In view
  switchTo("signin");
}


  const tabOperator = $("#tabOperator");
  const clerkPane = $("#clerkAuthSection");
  const operatorPane = $("#directAuthSection");

  function switchTab(target) {
    if (target === "operator") {
      tabOperator?.classList.add("active");
      tabClerk?.classList.remove("active");
      operatorPane?.classList.remove("hidden");
      clerkPane?.classList.add("hidden");
    } else {
      tabClerk?.classList.add("active");
      tabOperator?.classList.remove("active");
      clerkPane?.classList.remove("hidden");
      operatorPane?.classList.add("hidden");
    }
  }

  if (tabClerk && tabOperator) {
    tabClerk.onclick = () => switchTab("clerk");
    tabOperator.onclick = () => switchTab("operator");
  }

  // Toggle password visibility
  const btnTogglePwd = $("#btnTogglePwd");
  const pwdInput = $("#operatorPassword");
  if (btnTogglePwd && pwdInput) {
    btnTogglePwd.onclick = () => {
      pwdInput.type = pwdInput.type === "password" ? "text" : "password";
    };
  }

  // Operator Login Form Submission
  const loginForm = $("#loginForm");
  if (loginForm) {
    loginForm.onsubmit = async (e) => {
      e.preventDefault();
      const alertBox = $("#loginErrorAlert");
      const btnSubmit = $("#btnLoginSubmit");
      const btnText = $("#loginBtnText");

      if (alertBox) alertBox.classList.add("hidden");
      if (btnSubmit) btnSubmit.disabled = true;
      if (btnText) btnText.textContent = "Verifying credentials...";

      const email = $("#operatorEmail")?.value.trim() || "";
      const password = $("#operatorPassword")?.value || "";

      try {
        const resp = await fetch("/api/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email, password }),
        });

        const data = await resp.json();
        if (!resp.ok) {
          throw new Error(data.detail || "Invalid email or password");
        }

        state.user = data.user;
        $("#userEmail").textContent = data.user.email;
        $("#authPanel").classList.add("hidden");
        $("#appPanel").classList.remove("hidden");

        connectWebSocket();
        await navigate("overview");
        showRzpToast(`Signed in as ${data.user.email}`, "success");
      } catch (err) {
        if (alertBox) {
          alertBox.textContent = err.message || "Failed to sign in. Please verify your credentials.";
          alertBox.classList.remove("hidden");
        }
        showRzpToast(err.message || "Sign in failed", "error");
      } finally {
        if (btnSubmit) btnSubmit.disabled = false;
        if (btnText) btnText.textContent = "Sign In to Dashboard";
      }
    };
  }

  // Logout Button Handler
  const logoutBtn = $("#logoutBtn");
  if (logoutBtn) {
    logoutBtn.onclick = async () => {
      try {
        await fetch("/api/auth/logout", { method: "POST" });
      } catch {}

      if (window.Clerk && window.Clerk.user) {
        try {
          await window.Clerk.signOut();
        } catch {}
      }

      state.user = null;
      $("#authPanel").classList.remove("hidden");
      $("#appPanel").classList.add("hidden");
      showRzpToast("Signed out successfully", "info");
    };
  }
}

// ==============================================================================
// CLERK AUTHENTICATION INTEGRATION & INITIALIZATION
// ==============================================================================

async function loadClerkSdk(publishableKey) {
  if (window.Clerk) return window.Clerk;

  return new Promise((resolve) => {
    let script = document.querySelector('script[src*="clerk"]');
    if (!script) {
      let fapi = "";
      try {
        const parts = publishableKey.split("_");
        if (parts.length >= 3) {
          fapi = atob(parts[2]).replace(/\$$/, "");
        }
      } catch {}

      script = document.createElement("script");
      script.crossOrigin = "anonymous";
      script.setAttribute("data-clerk-publishable-key", publishableKey);
      script.src = fapi
        ? `https://${fapi}/npm/@clerk/clerk-js@5/dist/clerk.browser.js`
        : "https://cdn.jsdelivr.net/npm/@clerk/clerk-js@5/dist/clerk.browser.js";
      document.head.appendChild(script);
    }

    const check = setInterval(() => {
      if (window.Clerk) {
        clearInterval(check);
        resolve(window.Clerk);
      }
    }, 50);

    setTimeout(() => {
      clearInterval(check);
      resolve(window.Clerk || null);
    }, 4000);
  });
}

async function initClerkAuth() {
  let clerkKey = "";

  try {
    const config = await api("/api/auth/clerk-config");
    clerkKey = config.publishable_key;
  } catch {}

  if (!clerkKey) {
    clerkKey = localStorage.getItem("CLERK_PUBLISHABLE_KEY") || "";
  }

  if (!clerkKey) {
    // Switch to Operator tab if no Clerk key configured
    const tabOperator = $("#tabOperator");
    tabOperator?.click();
    return false;
  }

  try {
    const clerk = await loadClerkSdk(clerkKey);
    if (!clerk) {
      throw new Error("Clerk SDK could not be loaded");
    }

    await window.Clerk.load({
      publishableKey: clerkKey,
    });

    const placeholder = $("#clerkLoadingPlaceholder");
    if (placeholder) placeholder.style.display = "none";

    if (window.Clerk.user) {
      // User is already signed in with Clerk
      const userEmail =
        window.Clerk.user.primaryEmailAddress?.emailAddress ||
        `${window.Clerk.user.id}@clerk.local`;
      const token = await window.Clerk.session.getToken();

      await api("/api/auth/clerk-sync", {
        method: "POST",
        body: { email: userEmail, token: token },
      });

      state.user = { email: userEmail, role: "ADMIN" };
      $("#userEmail").textContent = userEmail;

      const userBtnContainer = $("#clerkUserButton");
      if (userBtnContainer) {
        window.Clerk.mountUserButton(userBtnContainer);
      }

      $("#authPanel").classList.add("hidden");
      $("#appPanel").classList.remove("hidden");
      connectWebSocket();
      await navigate("overview");
      return true;
    } else {
      // Removed Clerk sign‑out call – not needednt
      const container = $("#clerkSignInContainer");
      if (container) {
        container.innerHTML = "";
        window.Clerk.mountSignIn(container, {
          appearance: {
            variables: {
              colorPrimary: "#0b69ff",
              colorText: "#0c2340",
            },
          },
        });
      }
    }
  } catch (err) {
    console.warn("Clerk initialization fallback:", err.message);
    const placeholder = $("#clerkLoadingPlaceholder");
    if (placeholder) {
      placeholder.innerHTML = `
        <p style="color:#64748b; font-size:12px; margin-bottom:12px;">Enterprise SSO is ready. You can sign in using your operator credentials:</p>
        <button type="button" class="btn-primary" onclick="document.getElementById('tabOperator').click()" style="width:auto; padding:8px 18px; font-size:12px;">
          Use Operator Login
        </button>
      `;
    }
  }

  return false;
}

// ==============================================================================
// APPLICATION BOOTSTRAP
// ==============================================================================

async function bootApp() {
  setupAuthTabs();

  // First initialize Clerk auth if available
  const signedInWithClerk = await initClerkAuth();
  if (signedInWithClerk) {
    return;
  }

  // Check traditional cookie session
  try {
    const user = await api("/api/auth/me");
    state.user = user;
    $("#userEmail").textContent = user.email;
    $("#authPanel").classList.add("hidden");
    $("#appPanel").classList.remove("hidden");

    connectWebSocket();
    await navigate("overview");
  } catch {
    $("#authPanel").classList.remove("hidden");
    $("#appPanel").classList.add("hidden");
  }
}

bootApp();

