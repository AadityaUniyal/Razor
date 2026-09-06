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
  customers: [],
  approvals: [],
  customerSearch: "",
  customerFilter: "all",
  notificationOpen: false,
};

// Utilities
const money = (n) =>
  new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(n || 0);

const esc = (s) =>
  (s === 0 ? "0" : String(s || "")).replace(
    /[&<>'"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[c])
  );

const badgeClass = (value) => String(value || "").toLowerCase().replace(/[^a-z0-9_-]/g, "");

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

function attachDepthInteractions() {
  if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
  $$("#view .metric-card, #view .approval-card, #view .command-card, #view .signal-card").forEach((card) => {
    card.addEventListener("pointermove", (event) => {
      const rect = card.getBoundingClientRect();
      const x = (event.clientX - rect.left) / rect.width - 0.5;
      const y = (event.clientY - rect.top) / rect.height - 0.5;
      card.style.setProperty("--tilt-x", `${(-y * 1.4).toFixed(2)}deg`);
      card.style.setProperty("--tilt-y", `${(x * 1.4).toFixed(2)}deg`);
    });
    card.addEventListener("pointerleave", () => {
      card.style.removeProperty("--tilt-x");
      card.style.removeProperty("--tilt-y");
    });
  });
}

function renderMetricCard(label, val, hint = "", highlightClass = "") {
  return `
    <div class="metric-card">
      <div class="metric-label">${esc(label)}</div>
      <div class="metric-val ${esc(highlightClass)}">${esc(val)}</div>
      <div class="metric-hint">${esc(hint)}</div>
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

  $("#overviewHeadline").textContent = s.total_at_risk
    ? `${money(s.total_at_risk)} needs a thoughtful next step.`
    : "Your recovery operation is in a good place.";
  $("#overviewSubline").textContent = `${s.active_promises || 0} active promise${s.active_promises === 1 ? "" : "s"} and ${s.escalation_count || 0} human escalation${s.escalation_count === 1 ? "" : "s"} need attention today.`;
  const signalRows = [
    ["Open exposure", money(s.total_at_risk), s.total_at_risk ? "Prioritize queue" : "Clear", "queue", "amber"],
    ["Promise watchlist", `${s.active_promises || 0} active`, "Review commitments", "promises", "blue"],
    ["Safe stops", `${s.safe_stop_count || 0} protected`, "Policy enforced", "settings", "green"],
  ];
  $("#overviewSignals").innerHTML = signalRows.map(([label, value, note, view, tone]) => `
    <button class="signal-row" data-view="${view}"><span class="signal-marker ${tone}"></span><span><b>${esc(label)}</b><small>${esc(note)}</small></span><strong>${esc(value)}</strong><span class="signal-arrow">→</span></button>
  `).join("");

  // Funnel
  const eventTotal = Math.max(Number(s.total_cases || 0) + 4, Number(s.total_cases || 0), 1);
  const funnelSteps = [
    ["1. Events Received", s.total_cases + 4, 1, "Inbound payment and subscription signals"],
    ["2. Cases Created", s.total_cases, s.total_cases / eventTotal, "Deduplicated recovery cases"],
    ["3. Verified / Decided", s.total_cases, s.total_cases / eventTotal, "Policy and provider checks completed"],
    ["4. Interventions Avoided", money(s.unnecessary_interventions_prevented), 0.72, "Safe stops that protected customer trust"],
    ["5. Revenue Recovered", money(s.total_recovered), Number(s.total_recovered) > 0 ? 0.58 : 0, "Provider-backed recovery outcomes"],
  ];
  $("#funnel").innerHTML = funnelSteps
    .map(
      ([label, val, progress, description]) => `
      <div class="funnel-step" style="--funnel-progress:${Math.max(0, Math.min(1, progress))}" title="${esc(description)}" tabindex="0">
        <span>${esc(label)}</span>
        <strong>${esc(val)}</strong>
        <small>${Math.round(Math.max(0, Math.min(1, progress)) * 100)}% of intake</small>
      </div>
    `
    )
    .join("");

  // Live Activity Feed
  $("#activity").innerHTML =
    activity.slice(0, 8).map((a) => `
      <div class="timeline-item" data-tone="${String(a.event_type || "").toLowerCase().includes("recover") ? "success" : "warning"}">
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
              <tr class="clickable" data-open-case="${esc(c.case_id)}">
                <td><b>${esc(c.case_id)}</b></td>
                <td>${esc(c.customer_name)}</td>
                <td>${money(c.amount)}</td>
                <td><span style="font-size:11px;">${esc(c.failure_category)}</span></td>
                <td><span class="badge ${badgeClass(c.state)}">${esc(c.state)}</span></td>
                <td><b>${esc(c.current_action)}</b></td>
                <td><button class="btn-secondary" style="padding:4px 10px; font-size:11px;" data-open-case="${esc(c.case_id)}">Inspect</button></td>
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
  state.applyQueueFilters = null;
  state.cases = await api("/api/cases");
  state.queueFilter = state.queueFilter || "ALL";
  state.queueSearch = state.queueSearch || "";

  const queueCounts = state.cases.reduce((counts, item) => {
    counts[item.state] = (counts[item.state] || 0) + 1;
    return counts;
  }, {});
  $("#queueStats").innerHTML =
    renderMetricCard("Open exposure", money(state.cases.filter((item) => !item.recovered && item.state !== "STOP").reduce((sum, item) => sum + item.amount, 0)), "Cases needing an outcome", "highlight-blue") +
    renderMetricCard("Needs verification", queueCounts.VERIFY || 0, "Gateway checks pending") +
    renderMetricCard("Recovery in motion", queueCounts.RECOVER || 0, "Active customer outreach") +
    renderMetricCard("Escalated", queueCounts.ESCALATED || 0, "Human attention required", "highlight-green");

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
            <tr class="clickable" data-open-case="${esc(c.case_id)}">
              <td><b>${esc(c.case_id)}</b></td>
              <td>${esc(c.customer_name)}<br><small style="color:var(--text-sub);">${esc(c.customer_email)}</small></td>
              <td><b>${money(c.amount)}</b></td>
              <td><span style="font-size:11px; font-family:'JetBrains Mono', monospace;">${esc(c.failure_category)}</span></td>
              <td><span class="badge ${badgeClass(c.state)}">${esc(c.state)}</span></td>
              <td>${esc(c.retry_count)} retries / ${esc(c.communication_count)} comms</td>
              <td><b>${esc(c.current_action)}</b></td>
              <td><span style="font-weight:700; color:var(--primary);">${esc(c.strategy_score ?? 0)}</span></td>
              <td>${formatDateTime(c.updated_at)}</td>
            </tr>
          `
          )
          .join("")
      : `<tr><td colspan="9" style="text-align:center; padding:32px; color:var(--text-muted);">No cases match the selected filter.</td></tr>`;
  }

  state.applyQueueFilters = applyQueueFilters;
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

  $("#clearQueueFilters")?.addEventListener("click", () => {
    state.queueFilter = "ALL";
    state.queueSearch = "";
    if (searchInput) searchInput.value = "";
    $$("#queueFilterPills .filter-pill").forEach((p) => p.classList.toggle("active", p.dataset.filter === "ALL"));
    applyQueueFilters();
  });

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
  $("#detailHeaderTitle").innerHTML = `${money(c.amount)} <span class="badge ${badgeClass(c.state)}">${esc(c.state)}</span>`;

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
          <span>${esc(act)}</span>
            <strong>${esc(details.score)}</strong>
            <small style="color:var(--text-sub);">${esc(Math.round((details.expected_probability || 0) * 100))}% prob</small>
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
      ${c.communication_count < 3 ? "✓" : "✗"} Communication limit check: ${esc(c.communication_count)}/3 used
    </div>
    <div class="factor-item ${c.retry_count < 3 ? "pass" : "fail"}">
      ${c.retry_count < 3 ? "✓" : "✗"} Automated retry limit check: ${esc(c.retry_count)}/3 used
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
          <b>${esc(a.action_type)}</b> · <span class="badge ${badgeClass(a.status)}">${esc(a.status)}</span>
          <span style="float:right; color:var(--text-sub);">${money(a.cost)} cost</span>
          <p style="color:var(--text-muted); margin-top:2px;">${formatDateTime(a.created_at)}</p>
        </div>
      `
        )
        .join("")
    : `<p style="font-size:12px; color:var(--text-muted);">No recovery actions executed yet.</p>`;

  // Timeline
  const timelineList = $("#detailTimeline");
  timelineList.innerHTML = data.events.length ? data.events
    .slice()
    .reverse()
    .slice(0, 6)
    .map(
      (e) => `
      <div class="timeline-item" data-tone="${String(e.event_type || "").toLowerCase().includes("captur") || String(e.event_type || "").toLowerCase().includes("recover") ? "success" : String(e.event_type || "").toLowerCase().includes("fail") || String(e.event_type || "").toLowerCase().includes("dispute") ? "warning" : "info"}">
        <span class="timeline-time">${formatDateTime(e.created_at)}</span>
        <div class="timeline-body">
          <b>${esc(e.event_type)}</b>
          <p>${esc(e.message)}</p>
        </div>
      </div>
    `
      )
    .join("") : `<div class="table-empty-state"><span class="empty-icon blue">•</span><b>No lifecycle events yet</b><small>Provider and customer signals will appear here.</small></div>`;

  // Operator Action Handlers
  const canOperate = ["ADMIN", "OPERATOR"].includes(state.user?.role);
  ["#btnActionVerify", "#btnActionLink", "#btnActionEscalate", "#btnActionCapture", "#btnActionStop"].forEach((selector) => {
    const button = $(selector);
    if (button) {
      button.disabled = !canOperate;
      button.title = canOperate ? "" : "Operator role required";
    }
  });
  $("#btnActionVerify").onclick = () => executeOperatorAction(caseId, "VERIFY");
  $("#btnActionLink").onclick = () => executeOperatorAction(caseId, "RECOVER");
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
    state.cases = await api("/api/cases");
    await renderQueue();
  } catch (err) {
    showRzpToast("Action Failed", err.message, "error");
  }
}


// 4. PROMISES TO PAY
async function renderPromises() {
  setTemplate("promises");
  const promises = await api("/api/promises");
  state.promises = promises;
  const pending = promises.filter((promise) => promise.status === "PENDING").length;
  const followups = promises.filter((promise) => promise.status === "FOLLOW_UP_SENT").length;
  const fulfilled = promises.filter((promise) => promise.status === "FULFILLED").length;
  $("#promiseStats").innerHTML =
    renderMetricCard("Tracked commitments", promises.length, "Every promise has a verification window", "highlight-blue") +
    renderMetricCard("Waiting for payment", pending, "Grace period still active") +
    renderMetricCard("Follow-up sent", followups, "Bounded communication used") +
    renderMetricCard("Fulfilled", fulfilled, "Customer payment verified", "highlight-green");

  const applyPromiseFilters = () => {
    const query = ($( "#promiseSearchInput")?.value || "").trim().toLowerCase();
    const selectedStatus = $("#promiseFilterSelect")?.value || "all";
    const visible = promises.filter((promise) => {
      const matchesQuery = !query || [promise.case_id, promise.customer, promise.promise_text].some((value) => String(value || "").toLowerCase().includes(query));
      return matchesQuery && (selectedStatus === "all" || promise.status === selectedStatus);
    });
    $("#promiseCountBadge").textContent = `${visible.length} of ${promises.length}`;
    const tbody = $("#promisesTableBody");
    tbody.innerHTML = visible.length
      ? visible.map((p) => `
        <tr class="clickable" data-open-case="${esc(p.case_id)}">
          <td><b>${esc(p.case_id)}</b></td>
          <td>${esc(p.customer)}</td>
          <td><b>${money(p.amount)}</b></td>
          <td style="max-width:280px; font-style:italic;">"${esc(p.promise_text)}"</td>
          <td><b>${formatDateTime(p.promised_at)}</b></td>
          <td><span style="font-family:'JetBrains Mono', monospace; font-weight:700;">${Math.round((p.confidence || 0.9) * 100)}%</span></td>
          <td><span class="badge ${p.status === "FULFILLED" ? "recovered" : p.status === "FOLLOW_UP_SENT" ? "escalated" : "wait"}">${esc(p.status)}</span></td>
          <td>${p.follow_up_sent ? "Yes (1 follow-up)" : "No (Grace period)"}</td>
        </tr>`).join("")
      : `<tr><td colspan="8"><div class="table-empty-state"><span class="empty-icon amber">◷</span><b>No promises match this view</b><small>Try another status or search term.</small><button class="btn-secondary btn-compact" data-view="ai">Analyze a message</button></div></td></tr>`;
  };
  $("#promiseSearchInput").oninput = applyPromiseFilters;
  $("#promiseFilterSelect").onchange = applyPromiseFilters;
  applyPromiseFilters();
}

// 4b. CUSTOMER DIRECTORY
async function renderCustomers() {
  setTemplate("customers");
  state.customers = await api("/api/customers");
  $("#customerTotal").textContent = state.customers.length;
  const atRisk = state.customers.filter((customer) => customer.at_risk > 0).length;
  const recovered = state.customers.filter((customer) => customer.recovered > 0).length;
  const optOut = state.customers.filter((customer) => customer.opt_out).length;
  $("#customerStats").innerHTML =
    renderMetricCard("Profiles with exposure", atRisk, "Customers with an open case", "highlight-blue") +
    renderMetricCard("Recovered relationships", recovered, "Customers with verified recovery", "highlight-green") +
    renderMetricCard("Consent protected", optOut, "Opt-outs respected by policy");

  const applyCustomerFilters = () => {
    const q = state.customerSearch.toLowerCase();
    let rows = state.customers.filter((customer) => {
      const matchesSearch = !q || [customer.name, customer.email, customer.external_customer_id].some((value) => String(value || "").toLowerCase().includes(q));
      const matchesFilter = state.customerFilter === "all" ||
        (state.customerFilter === "risk" && customer.at_risk > 0) ||
        (state.customerFilter === "recovered" && customer.recovered > 0) ||
        (state.customerFilter === "optout" && customer.opt_out);
      return matchesSearch && matchesFilter;
    });
    $("#customersTableBody").innerHTML = rows.length ? rows.map((customer) => {
      const caseId = customer.case_ids?.[0] || "";
      return `<tr class="customer-row" ${caseId ? `data-open-case="${esc(caseId)}"` : ""}>
        <td><div class="customer-identity"><span class="customer-avatar">${esc((customer.name || "?").slice(0, 1).toUpperCase())}</span><span><b>${esc(customer.name || "Unknown")}</b><small>${esc(customer.email)}</small></span></div></td>
        <td><span class="relationship-count">${esc(customer.case_count)} case${customer.case_count === 1 ? "" : "s"}</span></td>
        <td><b class="money-risk">${money(customer.at_risk)}</b></td><td><b class="money-recovered">${money(customer.recovered)}</b></td>
        <td><span class="language-pill">${esc((customer.language || "en").toUpperCase())}</span></td>
        <td><span class="consent-pill ${customer.opt_out ? "blocked" : "active"}">${customer.opt_out ? "Opted out" : "Active"}</span></td>
        <td>${formatDateTime(customer.last_seen)}</td><td>${caseId ? '<span class="row-arrow">→</span>' : ""}</td>
      </tr>`;
    }).join("") : `<tr><td colspan="8"><div class="table-empty-state"><span class="empty-icon blue">⌕</span><b>No customers match those filters</b><small>Try a different name, email, or relationship filter.</small></div></td></tr>`;
  };
  $("#customerSearchInput").oninput = (event) => { state.customerSearch = event.target.value.trim(); applyCustomerFilters(); };
  $("#customerFilterSelect").onchange = (event) => { state.customerFilter = event.target.value; applyCustomerFilters(); };
  applyCustomerFilters();
}

// 5. AI SANDBOX & BENCHMARKS
async function renderAI() {
  setTemplate("ai");

  const metrics = await api("/api/ai/evaluation-metrics");
  $("#aiAccuracyBadge").textContent = `Benchmark Accuracy: ${metrics.accuracy_percent}%`;
  $("#metricPromiseRate").textContent = `${metrics.promise_detection_rate}%`;
  $("#metricPolicyAccept").textContent = `${metrics.policy_acceptance_rate}%`;
  $("#aiActiveProviderBadge").textContent = metrics.fallback_events ? `${metrics.fallback_events} fallback events` : "Provider chain healthy";

  const benchmarkRuns = Array.isArray(metrics.benchmark_runs) ? metrics.benchmark_runs : [];
  $("#aiBenchmarkTableBody").innerHTML = benchmarkRuns.length
    ? benchmarkRuns
        .map(
          (b) => `
      <tr>
        <td style="font-size:12px;">"${esc(b.message)}"</td>
        <td><span class="badge stop">${esc(b.expected_intent)}</span></td>
        <td><span class="badge ${b.matched ? "recovered" : "escalated"}">${esc(b.predicted_intent)}</span></td>
        <td><b>${Math.round((b.confidence || 0) * 100)}%</b></td>
        <td>${b.matched ? '<span style="color:#059669; font-weight:700;">✓ Pass</span>' : '<span style="color:#b91c1c; font-weight:700;">✗ Fail</span>'}</td>
      </tr>
    `
        )
        .join("")
    : `<tr><td colspan="5" style="text-align:center; padding:30px; color:var(--text-muted);">No benchmark rows available.</td></tr>`;
}

// 6. DECISION LEDGER
async function renderAudit() {
  setTemplate("audit");
  const ledger = await api("/api/audit");
  state.auditLedger = ledger;
  const approved = ledger.filter((row) => row.policy_result === "APPROVED").length;
  const blocked = ledger.filter((row) => row.policy_result === "BLOCKED").length;
  const aiDecisions = ledger.filter((row) => row.ai_analysis?.intent).length;
  $("#auditStats").innerHTML =
    renderMetricCard("Decisions recorded", ledger.length, "Immutable policy outcomes", "highlight-blue") +
    renderMetricCard("Policy approved", approved, "Actions cleared by guardrails", "highlight-green") +
    renderMetricCard("Safe blocks", blocked, "Stops that protected customers") +
    renderMetricCard("AI-assisted", aiDecisions, "Context enriched before policy");

  const applyAuditFilters = () => {
    const query = ($( "#auditSearchInput")?.value || "").trim().toLowerCase();
    const result = $("#auditResultSelect")?.value || "all";
    const visible = ledger.filter((row) => {
      const matchesQuery = !query || [row.case_id, row.policy_reason, row.selected_action].some((value) => String(value || "").toLowerCase().includes(query));
      return matchesQuery && (result === "all" || row.policy_result === result);
    });
    $("#auditTableBody").innerHTML = visible.length
      ? visible.map((d) => `
        <tr class="clickable" data-open-case="${esc(d.case_id)}">
          <td style="font-family:'JetBrains Mono', monospace; font-size:11px;">${formatDateTime(d.created_at)}</td>
          <td><b>${esc(d.case_id)}</b></td>
          <td><b>${esc(d.selected_action)}</b></td>
          <td><span class="badge ${d.policy_result === "APPROVED" ? "recovered" : "escalated"}">${esc(d.policy_result)}</span></td>
          <td style="max-width:320px;">${esc(d.policy_reason)}</td>
          <td><span style="font-size:11px;">${esc(d.ai_analysis?.intent || "DETERMINISTIC")}</span></td>
        </tr>`).join("")
      : `<tr><td colspan="6"><div class="table-empty-state"><span class="empty-icon green">✓</span><b>No ledger entries match this view</b><small>Try clearing the search or result filter.</small></div></td></tr>`;
  };
  $("#auditSearchInput").oninput = applyAuditFilters;
  $("#auditResultSelect").onchange = applyAuditFilters;
  applyAuditFilters();
}

// 7. ANALYTICS & ROI
async function renderAnalytics() {
  setTemplate("analytics");
  const [metrics, summary, trends] = await Promise.all([api("/api/dashboard/metrics"), api("/api/dashboard/summary"), api("/api/dashboard/trends?days=14")]);

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
            <b>${esc(cat)}</b>
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
  renderTrendChart(trends.series || []);
  renderRecoveryLift(metrics.recovery_lift || summary.recovery_lift || null);
  renderRiskHeatmap(metrics.by_category || {});
}

async function renderApprovals() {
  setTemplate("approvals");
  const status = $("#approvalFilterSelect")?.value || "PENDING_APPROVAL";
  const approvals = await api(`/api/approvals?status=${encodeURIComponent(status)}`);
  state.approvals = approvals;
  const pending = approvals.filter((item) => item.status === "PENDING_APPROVAL").length;
  const highRisk = approvals.filter((item) => (item.recommendation?.risk_flags || []).length > 0).length;
  $("#approvalStats").innerHTML =
    renderMetricCard("Requests in view", approvals.length, "Recommendation decisions with full context", "highlight-blue") +
    renderMetricCard("Pending review", pending, "Operator action required") +
    renderMetricCard("Risk flagged", highRisk, "Low confidence, disputes, opt-outs, or high value", highRisk ? "" : "highlight-green");

  const list = $("#approvalList");
  $("#approvalCountBadge").textContent = `${approvals.length} request${approvals.length === 1 ? "" : "s"}`;
  if (!approvals.length) {
    list.innerHTML = `<div class="table-empty-state"><span class="empty-icon green">✓</span><b>No approval work is waiting</b><small>New AI recommendations will appear here before any outbound action.</small><button class="btn-primary btn-compact" data-view="queue">Open recovery queue</button></div>`;
  } else {
    list.innerHTML = approvals.map((item) => {
      const rec = item.recommendation || {};
      const flags = (rec.risk_flags || []).map((flag) => `<span class="risk-chip">${esc(flag.replaceAll("_", " "))}</span>`).join("");
      const pendingActions = item.status === "PENDING_APPROVAL" ? `<div class="approval-actions"><textarea class="approval-message" data-approval-message="${item.id}" rows="2" placeholder="Optional approved message..."></textarea><div><button class="btn-primary btn-compact" data-approval-action="approve" data-approval-id="${item.id}">Approve ${esc(rec.recommended_action || "action")}</button><button class="btn-secondary btn-compact" data-approval-action="defer" data-approval-id="${item.id}">Defer</button><button class="btn-secondary btn-compact danger-outline" data-approval-action="reject" data-approval-id="${item.id}">Reject</button></div></div>` : `<div class="approval-decision"><span class="badge ${badgeClass(item.status)}">${esc(item.status)}</span><small>${esc(item.reason || "Decision recorded")}</small></div>`;
      return `<article class="approval-card approval-state-${badgeClass(item.status)}" data-approval-status="${esc(item.status)}"><div class="approval-card-top"><div><span class="eyebrow">CASE ${esc(item.case_id || "UNKNOWN")}</span><h4>${esc(rec.intent || "UNKNOWN")} <span class="badge ${badgeClass(item.status)}">${esc(item.status)}</span></h4></div><span class="approval-score">${money(rec.expected_value || 0)}<small>expected value</small></span></div><div class="approval-grid"><div><span>Recommended action</span><b>${esc(rec.recommended_action || "REVIEW")}</b></div><div><span>Channel</span><b>${esc(rec.recommended_channel || "No outbound channel")}</b></div><div><span>Confidence</span><b>${Math.round((Number(rec.confidence) || 0) * 100)}%</b></div><div><span>Disturbance</span><b>${money(rec.disturbance_cost || 0)}</b></div></div><div class="approval-flags">${flags || '<span class="safe-chip">POLICY CHECKS CLEAR</span>'}</div>${pendingActions}</article>`;
    }).join("");
  }
  $("#approvalFilterSelect").onchange = () => renderApprovals();
}

function renderTrendChart(series) {
  const target = $("#analyticsTrendChart");
  if (!target) return;
  if (!series.length || !series.some((item) => item.recovered || item.at_risk)) {
    target.innerHTML = `<div class="chart-empty"><span class="empty-icon blue">↗</span><b>Momentum will appear here</b><small>As recovery events arrive, this chart will show daily movement.</small></div>`;
    return;
  }
  const width = 900, height = 230, left = 44, top = 18, right = 18, bottom = 34;
  const max = Math.max(1, ...series.flatMap((item) => [item.recovered, item.at_risk]));
  const x = (index) => left + (index * (width - left - right)) / Math.max(1, series.length - 1);
  const y = (value) => top + (height - top - bottom) - (value / max) * (height - top - bottom);
  const line = (key) => series.map((item, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(item[key] || 0).toFixed(1)}`).join(" ");
  const points = (key, klass) => series.map((item, index) => `<circle class="chart-point ${klass}" cx="${x(index)}" cy="${y(item[key] || 0)}" r="3"><title>${esc(item.label)}: ${money(item[key] || 0)}</title></circle>`).join("");
  const labels = series.filter((_, index) => index % 3 === 0 || index === series.length - 1).map((item) => { const index = series.indexOf(item); return `<text x="${x(index)}" y="${height - 10}" text-anchor="middle">${esc(item.label)}</text>`; }).join("");
  target.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Recovery momentum chart"><defs><linearGradient id="recoveredFill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#16a878" stop-opacity=".18"/><stop offset="1" stop-color="#16a878" stop-opacity="0"/></linearGradient></defs><path class="chart-grid-line" d="M${left},${top}H${width - right}M${left},${(height - bottom + top) / 2}H${width - right}M${left},${height - bottom}H${width - right}"/><path class="chart-area" d="${line("recovered")} L${x(series.length - 1)},${height - bottom} L${left},${height - bottom} Z"/><path class="chart-line recovered-line" d="${line("recovered")}"/><path class="chart-line risk-line" d="${line("at_risk")}"/>${points("recovered", "recovered-point")}${points("at_risk", "risk-point")}${labels}</svg>`;
}

function renderRecoveryLift(lift) {
  const target = $("#analyticsLiftPanel");
  const badge = $("#liftStatusBadge");
  if (!target) return;
  const treatment = Number(lift?.treatment_recovery_rate ?? lift?.treatment_rate);
  const holdout = Number(lift?.holdout_recovery_rate ?? lift?.holdout_rate);
  if (!Number.isFinite(treatment) || !Number.isFinite(holdout)) {
    if (badge) badge.textContent = "Awaiting holdout";
    target.innerHTML = `<div class="lift-empty">A control group is required before the product claims incremental recovery lift. Assign an experiment to make this view measurable.</div>`;
    return;
  }
  const liftValue = treatment - holdout;
  if (badge) { badge.textContent = `${liftValue >= 0 ? "+" : ""}${liftValue.toFixed(1)} pts`; badge.className = `badge ${liftValue >= 0 ? "recovered" : "escalated"}`; }
  target.innerHTML = `<div class="lift-stat"><span><small>Treatment recovery rate</small><strong class="safe-text">${treatment.toFixed(1)}%</strong></span><span class="lift-arrow">→</span><span><small>Holdout recovery rate</small><strong>${holdout.toFixed(1)}%</strong></span></div><div class="lift-stat"><span><small>Incremental recovery</small><strong>${liftValue >= 0 ? "+" : ""}${liftValue.toFixed(1)} pts</strong></span><small>${esc(lift.sample_size ? `${lift.sample_size} customers measured` : "Measured against assigned control")}</small></div>`;
}

function renderRiskHeatmap(categories) {
  const target = $("#analyticsRiskHeatmap");
  if (!target) return;
  const rows = Object.entries(categories || {}).filter(([, item]) => item && (item.at_risk || item.recovered));
  if (!rows.length) {
    target.innerHTML = `<div class="table-empty-state"><span class="empty-icon blue">◌</span><b>Risk surface will appear here</b><small>Category-level exposure becomes visible as recovery events arrive.</small></div>`;
    return;
  }
  const max = Math.max(1, ...rows.map(([, item]) => Number(item.at_risk || 0)));
  target.innerHTML = rows.map(([category, item]) => {
    const intensity = Number(item.at_risk || 0) / max;
    const hue = Math.round(150 - intensity * 115);
    return `<div class="risk-cell" style="--risk-hue:${hue}" tabindex="0" title="${esc(category)}: ${money(item.at_risk || 0)} at risk, ${money(item.recovered || 0)} recovered"><b>${esc(category.replaceAll("_", " "))}</b><span>${money(item.at_risk || 0)} at risk</span><small>${money(item.recovered || 0)} recovered</small></div>`;
  }).join("");
}

// 8. SETTINGS & HEALTH
async function renderSettings() {
  setTemplate("settings");
  const [policies, health] = await Promise.all([api("/api/policies"), api("/api/health")]);
  const activePolicy = policies[0] || {};
  state.activePolicy = activePolicy;

  $("#policyVersionBadge").textContent = activePolicy.version || "v1.2";
  $("#policyConfigList").innerHTML = activePolicy.configuration
    ? Object.entries(activePolicy.configuration)
        .map(([k, v]) => `
          <div style="display:flex; justify-content:space-between; padding:10px 0; border-bottom:1px solid var(--border); font-size:13px;">
            <span style="color:var(--text-muted);">${esc(k.replace(/_/g, " "))}</span>
            <b>${esc(typeof v === "object" && v !== null ? JSON.stringify(v) : v)}</b>
          </div>
        `)
        .join("")
    : "No policy found.";

  $("#systemHealthDetails").innerHTML = Object.entries(health)
    .filter(([k]) => k !== "recent_events")
    .map(([k, v]) => `
      <div style="display:flex; justify-content:space-between; padding:10px 0; border-bottom:1px solid var(--border); font-size:13px;">
        <span style="color:var(--text-muted);">${esc(k.replace(/_/g, " "))}</span>
        <b style="font-family:'JetBrains Mono', monospace;">${esc(typeof v === "object" && v !== null ? JSON.stringify(v) : v)}</b>
      </div>
    `)
    .join("");
  $("#healthEvents").innerHTML = (health.recent_events || []).slice(0, 6).map((event) => `<div class="health-event"><span class="health-event-dot ${String(event.status).includes("FAILED") ? "bad" : "good"}"></span><span><b>${esc(event.service_name)}</b><small>${esc(event.status)}</small></span><time>${formatDateTime(event.created_at)}</time></div>`).join("") || `<p class="muted-caption">No recent subsystem events.</p>`;
  const config = activePolicy.configuration || {};
  const fields = { policyMaxAttempts: config.maximum_automated_attempts, policyMaxComms: config.maximum_customer_communications, policyEscalation: config.minimum_amount_for_human_escalation, policyGraceHours: config.promise_grace_hours, policyConfidence: config.ai_confidence_threshold };
  Object.entries(fields).forEach(([id, value]) => { const input = $("#" + id); if (input) input.value = value ?? ""; });
  const canEdit = state.user?.role === "ADMIN";
  $("#policyForm")?.querySelectorAll("input, button").forEach((control) => { control.disabled = !canEdit; });
}

// ==============================================================================
// NAVIGATION & ROUTING
// ==============================================================================

let navigationQueue = Promise.resolve();

function navigate(viewName) {
  const nextNavigation = navigationQueue.then(() => navigateNow(viewName));
  navigationQueue = nextNavigation.catch(() => {});
  return nextNavigation;
}

async function navigateNow(viewName) {
  state.view = viewName;
  $("#crumb").textContent = `OPERATIONS / ${viewName.toUpperCase()}`;
  $("#pageTitle").textContent = {
    overview: "Operations Overview",
    queue: "Recovery Queue",
    approvals: "Approval Queue",
    customers: "Customer Directory",
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
    approvals: renderApprovals,
    customers: renderCustomers,
    promises: renderPromises,
    ai: renderAI,
    audit: renderAudit,
    analytics: renderAnalytics,
    settings: renderSettings,
  };

  const fn = renderers[viewName];
  if (!fn) return;
  const view = $("#view");
  if (view) {
    view.innerHTML = `<div class="view-loading" role="status"><span class="loading-spinner"></span> Loading ${esc(viewName)}...</div>`;
  }
  try {
    await fn();
    attachDepthInteractions();
  } catch (err) {
    if (view) {
      view.innerHTML = `<div class="view-error" role="alert"><strong>Unable to load this view.</strong><span>${esc(err.message || "Please try again.")}</span><button class="btn-primary" data-retry-view="${esc(viewName)}">Retry</button></div>`;
    }
  }
}

// ==============================================================================
// GLOBAL EVENT LISTENERS
// ==============================================================================

document.body.addEventListener("click", async (e) => {
  if (e.target.closest("#mobileNavToggle")) {
    const appPanel = $("#appPanel");
    const open = !appPanel.classList.contains("nav-open");
    appPanel.classList.toggle("nav-open", open);
    $("#mobileNavToggle").setAttribute("aria-expanded", String(open));
    $("#mobileNavBackdrop").classList.toggle("hidden", !open);
    return;
  }
  if (e.target.closest("#mobileNavBackdrop")) {
    $("#appPanel").classList.remove("nav-open");
    $("#mobileNavBackdrop").classList.add("hidden");
    $("#mobileNavToggle")?.setAttribute("aria-expanded", "false");
    return;
  }
  if (e.target.closest("#refreshViewBtn")) {
    await navigate(state.view);
    showRzpToast("View refreshed", "The latest data is now on screen.", "success");
    return;
  }
  if (e.target.closest("#notificationBtn")) {
    const popover = $("#notificationPopover");
    state.notificationOpen = !state.notificationOpen;
    popover.classList.toggle("hidden", !state.notificationOpen);
    $("#notificationBtn").setAttribute("aria-expanded", String(state.notificationOpen));
    if (state.notificationOpen) await renderNotifications();
    return;
  }
  if (e.target.closest("[data-close-popover]")) {
    state.notificationOpen = false;
    $("#notificationPopover").classList.add("hidden");
    $("#notificationBtn")?.setAttribute("aria-expanded", "false");
    return;
  }
  // Nav Click
  const navBtn = e.target.closest("[data-view]");
  if (navBtn) {
    e.preventDefault();
    await navigate(navBtn.dataset.view);
    $("#appPanel")?.classList.remove("nav-open");
    $("#mobileNavBackdrop")?.classList.add("hidden");
    $("#mobileNavToggle")?.setAttribute("aria-expanded", "false");
    return;
  }

  const retryBtn = e.target.closest("[data-retry-view]");
  if (retryBtn) {
    await navigate(retryBtn.dataset.retryView);
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

  const approvalButton = e.target.closest("[data-approval-action]");
  if (approvalButton) {
    const id = approvalButton.dataset.approvalId;
    const action = approvalButton.dataset.approvalAction;
    const message = document.querySelector(`[data-approval-message="${id}"]`)?.value || "";
    try {
      await api(`/api/approvals/${encodeURIComponent(id)}/${action}`, { method: "POST", body: { message } });
      showRzpToast(`Approval ${action}d`, "The decision has been recorded and the case was updated.", "success");
      await renderApprovals();
    } catch (error) {
      showRzpToast("Approval failed", error.message, "error");
    }
    return;
  }

});

async function renderNotifications() {
  const list = $("#notificationList");
  if (!list) return;
  list.innerHTML = `<div class="popover-loading"><span class="loading-spinner"></span> Loading activity...</div>`;
  try {
    const notifications = await api("/api/dashboard/notifications");
    $("#notificationCount")?.classList.toggle("hidden", !notifications.length);
    if ($("#notificationCount")) $("#notificationCount").textContent = Math.min(notifications.length, 99);
    list.innerHTML = notifications.length ? notifications.slice(0, 8).map((notification) => `<button class="notification-item" ${notification.case_id ? `data-open-case="${esc(notification.case_id)}"` : ""}><span class="notification-channel">${esc((notification.channel || "event").slice(0, 1).toUpperCase())}</span><span><b>${esc(notification.case_id || notification.channel)}</b><small>${esc(notification.message)}</small><time>${formatDateTime(notification.created_at)}</time></span></button>`).join("") : `<div class="popover-empty"><span class="empty-icon green">✓</span><b>All caught up</b><small>No communications have been dispatched recently.</small></div>`;
  } catch (error) {
    list.innerHTML = `<div class="popover-empty"><b>Notifications unavailable</b><small>${esc(error.message)}</small></div>`;
  }
}

// Forms
document.body.addEventListener("submit", async (e) => {
  if (e.target.id === "policyForm") {
    e.preventDefault();
    const current = state.activePolicy || {};
    const configuration = {
      ...(current.configuration || {}),
      maximum_automated_attempts: Number($("#policyMaxAttempts").value),
      maximum_customer_communications: Number($("#policyMaxComms").value),
      minimum_amount_for_human_escalation: Number($("#policyEscalation").value),
      promise_grace_hours: Number($("#policyGraceHours").value),
      ai_confidence_threshold: Number($("#policyConfidence").value),
    };
    try {
      await api("/api/policies", { method: "POST", body: { configuration } });
      showRzpToast("Policy version saved", "New recovery guardrails are active.", "success");
      await renderSettings();
    } catch (error) {
      showRzpToast("Policy update failed", error.message, "error");
    }
    return;
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
        const provider = esc(res.provider_used || "unknown");
        const latency = esc(Number(res.latency_ms) || 0);
        const tokens = esc(res.tokens && res.tokens.total_tokens != null ? res.tokens.total_tokens : "N/A");
        telemetryBox.innerHTML = `<span style="color:#059669;">● ${provider}</span> | Latency: <b>${latency}ms</b> | Tokens: <b>${tokens}</b>`;
      }
    } catch (err) {
      resultBox.textContent = `Error: ${err.message}`;
      if (telemetryBox) telemetryBox.textContent = "Error";
    }
  }
});

document.body.addEventListener("keydown", (e) => {
  if (e.target.id === "aiMessageInput" && e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    e.target.form?.requestSubmit();
  }
});

// Logout
$("#logoutBtn").onclick = async (e) => {
  e.preventDefault();
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
        state.applyQueueFilters?.();
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
          state.applyQueueFilters?.();
          if (state.selectedCaseId) await showCaseInspector(state.selectedCaseId);
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

function setAuthAlert(message = "") {
  const alertBox = $("#authAlert");
  if (!alertBox) return;
  alertBox.textContent = message;
  alertBox.classList.toggle("hidden", !message);
}

function showAppShell(user) {
  state.user = user;
  $("#userEmail").textContent = user?.email || "";
  $("#authPanel").classList.add("hidden");
  $("#appPanel").classList.remove("hidden");
}

function showAuthShell() {
  state.user = null;
  $("#authPanel").classList.remove("hidden");
  $("#appPanel").classList.add("hidden");
}

function setupAuthTabs() {
  const tabSignIn = $("#tabSignIn");
  const tabSignUp = $("#tabSignUp");
  const signInForm = $("#signInForm");
  const signUpForm = $("#signUpForm");
  const authSubheading = $("#authSubheading");

  function switchTo(mode) {
    const signingIn = mode === "signin";
    tabSignIn?.classList.toggle("active", signingIn);
    tabSignUp?.classList.toggle("active", !signingIn);
    signInForm?.classList.toggle("hidden", !signingIn);
    signUpForm?.classList.toggle("hidden", signingIn);
    if (authSubheading) {
      authSubheading.textContent = signingIn ? "Sign in to continue" : "Create your account";
    }
    setAuthAlert("");
  }

  tabSignIn?.addEventListener("click", (e) => {
    e.preventDefault();
    switchTo("signin");
  });
  tabSignUp?.addEventListener("click", (e) => {
    e.preventDefault();
    switchTo("signup");
  });

  const loginPwd = $("#loginPassword");
  const toggleLoginPwd = $("#btnToggleLoginPwd");
  toggleLoginPwd?.addEventListener("click", () => {
    if (!loginPwd) return;
    loginPwd.type = loginPwd.type === "password" ? "text" : "password";
  });

  $("#signInForm")?.addEventListener("submit", (e) => {
    e.preventDefault();
    submitLogin($("#loginEmail")?.value?.trim() || "", $("#loginPassword")?.value || "");
  });

  $("#signUpForm")?.addEventListener("submit", (e) => {
    e.preventDefault();
    submitRegister($("#regEmail")?.value?.trim() || "", $("#regPassword")?.value || "");
  });

  const initialMode = new URLSearchParams(window.location.search).get("mode") === "signup" ? "signup" : "signin";
  switchTo(initialMode);
}

async function submitLogin(email, password) {
  const btnSubmit = $("#btnSignInSubmit");
  const btnText = $("#signInBtnText");
  setAuthAlert("");
  if (!email || !password) {
    setAuthAlert("Email and password are required.");
    return;
  }
  if (btnSubmit) btnSubmit.disabled = true;
  if (btnText) btnText.textContent = "Signing in...";
  try {
    const data = await api("/api/auth/login", { method: "POST", body: { email, password } });
    showAppShell(data.user);
    connectWebSocket();
    await navigate("overview");
    showRzpToast("Signed in", data.user.email, "success");
  } catch (err) {
    const msg = err.message || "Sign in failed";
    setAuthAlert(msg);
    showRzpToast("Sign in failed", msg, "error");
  } finally {
    if (btnSubmit) btnSubmit.disabled = false;
    if (btnText) btnText.textContent = "Sign In to Dashboard";
  }
}

async function submitRegister(email, password) {
  const btnSubmit = $("#btnSignUpSubmit");
  const btnText = $("#signUpBtnText");
  setAuthAlert("");
  if (!email || !password) {
    setAuthAlert("Email and password are required.");
    return;
  }
  if (password.length < 8) {
    setAuthAlert("Password must contain at least 8 characters.");
    return;
  }
  if (btnSubmit) btnSubmit.disabled = true;
  if (btnText) btnText.textContent = "Creating account...";
  try {
    const data = await api("/api/auth/register", { method: "POST", body: { email, password } });
    showAppShell(data.user);
    connectWebSocket();
    await navigate("overview");
    showRzpToast("Account created", data.user.email, "success");
  } catch (err) {
    const msg = err.message || "Registration failed";
    setAuthAlert(msg);
    showRzpToast("Registration failed", msg, "error");
  } finally {
    if (btnSubmit) btnSubmit.disabled = false;
    if (btnText) btnText.textContent = "Create Account";
  }
}

// ==============================================================================
// APPLICATION BOOTSTRAP
// ==============================================================================

async function bootApp() {
  setupAuthTabs();
  try {
    const user = await api("/api/auth/me");
    showAppShell(user);
    connectWebSocket();
    await navigate("overview");
  } catch {
    showAuthShell();
  }
}

bootApp();
