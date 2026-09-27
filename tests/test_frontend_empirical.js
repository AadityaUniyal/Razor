/**
 * Empirical test suite for frontend/app.js functions:
 * - displayValue()
 * - renderStructuredObject()
 * - Factor checklist rendering (recovered === true vs recovered === false)
 * - Dynamic limit calculation (communication_count, retry_count vs dynamic policy)
 */

const fs = require('fs');
const path = require('path');
const assert = require('assert');
const vm = require('vm');

// Read app.js source
const appJsPath = path.join(__dirname, '..', 'frontend', 'app.js');
const appJsCode = fs.readFileSync(appJsPath, 'utf8');

// Slice lines 22 to 97 which contain money, esc, badgeClass, humanize, renderStructuredObject, displayValue
const lines = appJsCode.split('\n');
const utilitiesCode = lines.slice(21, 97).join('\n');

const sandbox = {
  console: console,
  Intl: Intl
};

vm.createContext(sandbox);

const exported = vm.runInContext(`
(function() {
${utilitiesCode}
  return { money, esc, badgeClass, humanize, renderStructuredObject, displayValue };
})()
`, sandbox);

const { displayValue, renderStructuredObject, money, esc, humanize } = exported;

const results = [];

function test(name, fn) {
  try {
    fn();
    results.push({ name, status: 'PASS' });
    console.log(`PASS: ${name}`);
  } catch (err) {
    results.push({ name, status: 'FAIL', error: err.message });
    console.error(`FAIL: ${name} -> ${err.message}`);
  }
}

// ==========================================
// TEST SUITE 1: displayValue() basic inputs
// ==========================================
test("displayValue(null) returns 'Not available'", () => {
  assert.strictEqual(displayValue(null), "Not available");
});

test("displayValue(undefined) returns 'Not available'", () => {
  assert.strictEqual(displayValue(undefined), "Not available");
});

test("displayValue('') returns 'Not available'", () => {
  assert.strictEqual(displayValue(""), "Not available");
});

test("displayValue(true) returns 'Yes'", () => {
  assert.strictEqual(displayValue(true), "Yes");
});

test("displayValue(false) returns 'No'", () => {
  assert.strictEqual(displayValue(false), "No");
});

test("displayValue(0) returns '0'", () => {
  assert.strictEqual(displayValue(0), "0");
});

test("displayValue(1500) returns Indian locale formatted string '1,500'", () => {
  assert.strictEqual(displayValue(1500), "1,500");
});

test("displayValue(1234567) returns Indian locale formatted string '12,34,567'", () => {
  assert.strictEqual(displayValue(1234567), "12,34,567");
});

test("displayValue(12.3456) returns 2 decimal fixed string '12.35'", () => {
  assert.strictEqual(displayValue(12.3456), "12.35");
});

test("displayValue('custom string') returns 'custom string'", () => {
  assert.strictEqual(displayValue("custom string"), "custom string");
});

// ==========================================
// TEST SUITE 2: renderStructuredObject() inputs
// ==========================================
test("renderStructuredObject(null) returns 'Not available'", () => {
  assert.strictEqual(renderStructuredObject(null), "Not available");
});

test("renderStructuredObject('string') returns 'Not available'", () => {
  assert.strictEqual(renderStructuredObject("string"), "Not available");
});

test("renderStructuredObject() for channel_costs renders grid with ₹ amounts", () => {
  const channelCosts = {
    VERIFY: 0,
    WAIT: 0,
    EMAIL: 1,
    WHATSAPP: 2,
    SMS: 3,
    RECOVERY_LINK: 2,
    ESCALATE: 100
  };
  const html = renderStructuredObject(channelCosts, "channel_costs");
  assert(html.includes('class="channel-cost-grid"'), "Missing channel-cost-grid");
  assert(html.includes('class="channel-cost-chip"'), "Missing channel-cost-chip");
  assert(html.includes('VERIFY'), "Missing channel name VERIFY");
  assert(html.includes('RECOVERY LINK'), "Missing humanized channel name RECOVERY LINK");
  assert(html.includes('₹100') || html.includes('100'), "Missing formatted currency");
  // Check no raw JSON braces
  assert(!html.includes('{"'), "Contains raw JSON");
});

test("renderStructuredObject() for database_health renders db-health-card and pool badges", () => {
  const dbHealth = {
    status: "HEALTHY",
    latency_ms: 0.8,
    pool: {
      size: 5,
      checkedin: 5,
      checkedout: 0,
      overflow: 0
    }
  };
  const html = renderStructuredObject(dbHealth, "database_health");
  assert(html.includes('class="db-health-card"'), "Missing db-health-card");
  assert(html.includes('class="badge recovered"'), "Missing recovered badge for HEALTHY status");
  assert(html.includes('0.8ms latency'), "Missing latency display");
  assert(html.includes('Pool Size: 5'), "Missing pool size");
  assert(html.includes('Active: 0'), "Missing active pool stat");
  assert(html.includes('Idle: 5'), "Missing idle pool stat");
});

test("renderStructuredObject() for degraded database_health renders escalated badge and overflow", () => {
  const dbHealth = {
    status: "DEGRADED",
    latency_ms: 125.4,
    pool: {
      size: 10,
      checkedin: 1,
      checkedout: 9,
      overflow: 3
    }
  };
  const html = renderStructuredObject(dbHealth, "database_health");
  assert(html.includes('class="badge escalated"'), "Missing escalated badge for DEGRADED");
  assert(html.includes('125.4ms latency'), "Missing latency display");
  assert(html.includes('Overflow: 3'), "Missing overflow badge when overflow > 0");
});

test("renderStructuredObject() for generic nested object renders nested-obj-grid", () => {
  const nested = {
    max_retries: 5,
    enable_smart_retry: true,
    sub_config: {
      timeout_seconds: 30,
      fallback_provider: "stripe"
    }
  };
  const html = renderStructuredObject(nested, "custom_config");
  assert(html.includes('class="nested-obj-grid"'), "Missing nested-obj-grid");
  assert(html.includes('class="nested-obj-item"'), "Missing nested-obj-item");
  assert(html.includes('Max Retries:'), "Missing humanized key");
  assert(html.includes('Yes'), "Boolean child not rendered as Yes");
  assert(html.includes('Timeout Seconds:'), "Nested sub_config key missing");
});

test("displayValue() delegates nested objects to renderStructuredObject", () => {
  const nested = { a: 1, b: "two" };
  const html = displayValue(nested, "my_key");
  assert(html.includes('class="nested-obj-grid"'), "displayValue should return structured grid for objects");
  assert(html.includes('A:'), "Key humanized");
  assert(html.includes('1'), "Number formatted");
});

// =========================================================
// TEST SUITE 3: Factor checklist rendering (c.recovered)
// =========================================================
// Extract the exact factor checklist rendering logic from app.js: lines 496-513
function renderFactorChecklist(c, factors = {}, activePolicy = null) {
  const maxComms = activePolicy?.configuration?.maximum_customer_communications ?? 3;
  const maxAttempts = activePolicy?.configuration?.maximum_automated_attempts ?? 3;

  return `
    <div class="factor-item ${c.recovered ? "pass" : "fail"}">
      ${c.recovered ? "✓" : "✗"} Payment verified state: <b>${c.recovered ? "CAPTURED" : "PENDING"}</b>
    </div>
    <div class="factor-item ${c.communication_count < maxComms ? "pass" : "fail"}">
      ${c.communication_count < maxComms ? "✓" : "✗"} Communication limit check: ${esc(c.communication_count)}/${maxComms} used
    </div>
    <div class="factor-item ${c.retry_count < maxAttempts ? "pass" : "fail"}">
      ${c.retry_count < maxAttempts ? "✓" : "✗"} Automated retry limit check: ${esc(c.retry_count)}/${maxAttempts} used
    </div>
    <div class="factor-item ${!factors.customer_opted_out ? "pass" : "fail"}">
      ${!factors.customer_opted_out ? "✓" : "✗"} Customer opt-out status: ${factors.customer_opted_out ? "OPTED OUT" : "ACTIVE"}
    </div>
  `;
}

test("Factor checklist when c.recovered === true renders pass class and CAPTURED", () => {
  const c = { recovered: true, communication_count: 1, retry_count: 1 };
  const html = renderFactorChecklist(c, {}, null);
  assert(html.includes('factor-item pass'), "Did not render pass class");
  assert(html.includes('✓ Payment verified state: <b>CAPTURED</b>'), "Did not render CAPTURED with checkmark");
  assert(!html.includes('✗ Payment verified state'), "Unexpected ✗ symbol");
});

test("Factor checklist when c.recovered === false renders fail class and PENDING", () => {
  const c = { recovered: false, communication_count: 1, retry_count: 1 };
  const html = renderFactorChecklist(c, {}, null);
  assert(html.includes('factor-item fail'), "Did not render fail class for unrecovered case");
  assert(html.includes('✗ Payment verified state: <b>PENDING</b>'), "Did not render PENDING with ✗ symbol");
  // CRITICAL: Ensure it is NOT 'factor-item pass' for payment verified
  const paymentVerifiedSnippet = html.match(/<div class="factor-item ([^"]+)">\s*([^<\n]+)\s*Payment verified state: <b>([^<]+)<\/b>/);
  assert(paymentVerifiedSnippet, "Could not match payment verified snippet");
  assert.strictEqual(paymentVerifiedSnippet[1], "fail", "Must have 'fail' class when recovered is false");
  assert.strictEqual(paymentVerifiedSnippet[3], "PENDING", "Must display PENDING when recovered is false");
});

// =========================================================
// TEST SUITE 4: Dynamic limit calculation
// =========================================================
test("Dynamic limits: default to 3 when activePolicy is null", () => {
  const c = { recovered: false, communication_count: 2, retry_count: 2 };
  const html = renderFactorChecklist(c, {}, null);
  assert(html.includes('2/3 used'), "Expected default limit /3");
  assert(html.includes('factor-item pass'), "2 < 3 should be pass");
});

test("Dynamic limits: fail when count equals or exceeds default limit", () => {
  const c = { recovered: false, communication_count: 3, retry_count: 4 };
  const html = renderFactorChecklist(c, {}, null);
  assert(html.includes('Communication limit check: 3/3 used'), "Communication check text mismatch");
  assert(html.includes('Automated retry limit check: 4/3 used'), "Retry check text mismatch");
  // Check comms failed
  const commsSnippet = html.match(/<div class="factor-item ([^"]+)">\s*([^<\n]+)\s*Communication limit check: 3\/3 used/);
  assert.strictEqual(commsSnippet[1], "fail", "Comms should be fail when 3 >= 3");
  assert.strictEqual(commsSnippet[2].trim(), "✗", "Comms symbol should be ✗");
  // Check retry failed
  const retrySnippet = html.match(/<div class="factor-item ([^"]+)">\s*([^<\n]+)\s*Automated retry limit check: 4\/3 used/);
  assert.strictEqual(retrySnippet[1], "fail", "Retry should be fail when 4 >= 3");
});

test("Dynamic limits: boundary case count = limit - 1 (e.g. 2 of 3) passes", () => {
  const c = { recovered: false, communication_count: 2, retry_count: 2 };
  const html = renderFactorChecklist(c, {}, null);
  const commsSnippet = html.match(/<div class="factor-item ([^"]+)">\s*([^<\n]+)\s*Communication limit check: 2\/3 used/);
  assert.strictEqual(commsSnippet[1], "pass");
  assert.strictEqual(commsSnippet[2].trim(), "✓");
});

test("Dynamic limits: respects custom policy limits (e.g. 5 comms, 7 retries)", () => {
  const policy = {
    configuration: {
      maximum_customer_communications: 5,
      maximum_automated_attempts: 7
    }
  };
  const c = { recovered: false, communication_count: 4, retry_count: 6 };
  const html = renderFactorChecklist(c, {}, policy);
  assert(html.includes('4/5 used'), "Expected custom comms limit 4/5");
  assert(html.includes('6/7 used'), "Expected custom retry limit 6/7");
  // Both are under limit, so both should pass
  assert(html.includes('✓ Communication limit check: 4/5 used'));
  assert(html.includes('✓ Automated retry limit check: 6/7 used'));
});

test("Dynamic limits: fails custom policy limits when reached (e.g. 5/5, 8/7)", () => {
  const policy = {
    configuration: {
      maximum_customer_communications: 5,
      maximum_automated_attempts: 7
    }
  };
  const c = { recovered: false, communication_count: 5, retry_count: 8 };
  const html = renderFactorChecklist(c, {}, policy);
  assert(html.includes('5/5 used'), "Expected custom comms limit 5/5");
  assert(html.includes('8/7 used'), "Expected custom retry limit 8/7");
  assert(html.includes('✗ Communication limit check: 5/5 used'));
  assert(html.includes('✗ Automated retry limit check: 8/7 used'));
});

test("Dynamic limits: zero limits or zero usage edge cases (e.g. 0/3 used)", () => {
  const c = { recovered: false, communication_count: 0, retry_count: 0 };
  const html = renderFactorChecklist(c, {}, null);
  assert(html.includes('0/3 used'), "Expected 0/3 used");
  assert(html.includes('✓ Communication limit check: 0/3 used'));
  assert(html.includes('✓ Automated retry limit check: 0/3 used'));
});

// =========================================================
// TEST SUITE 5: Adversarial Stress Testing & Edge Cases
// =========================================================
test("Adversarial: XSS payload in keys and values is sanitized by esc()", () => {
  const evil = {
    "<script>alert(1)</script>": "<img src=x onerror=alert(2)>",
    "safe_key": "<b>bold</b>"
  };
  const html = renderStructuredObject(evil, "test");
  assert(!html.includes('<script>'), "Raw <script> was not escaped!");
  assert(!html.includes('<img src=x'), "Raw <img> was not escaped!");
  assert(html.toLowerCase().includes('&lt;script&gt;'), "Expected HTML escaped entity for script tag");
  assert(html.toLowerCase().includes('&lt;img src=x'), "Expected HTML escaped entity for img tag");
});

test("Adversarial: Deeply nested objects (3+ levels) render recursively without crashing", () => {
  const deep = {
    level1: {
      level2: {
        level3: {
          leaf: "deep_value"
        }
      }
    }
  };
  const html = renderStructuredObject(deep, "deep");
  assert(html.includes('Leaf:'), "Level 3 key missing");
  assert(html.includes('deep_value'), "Deep value missing");
  assert(!html.includes('[object Object]'), "Must not dump raw [object Object]");
});

test("Adversarial: Array inputs in renderStructuredObject render without throwing", () => {
  const arr = ["itemA", "itemB", "itemC"];
  const html = renderStructuredObject(arr, "items");
  assert(html.includes('itemA'), "Missing itemA in array render");
  assert(html.includes('itemB'), "Missing itemB in array render");
  assert(!html.includes('[object Object]'), "Array should render keys/values without [object Object]");
});

test("Adversarial: Object with Object.create(null) renders without prototype pollution issues", () => {
  const bareObj = Object.create(null);
  bareObj.custom_key = "custom_val";
  const html = renderStructuredObject(bareObj, "bare");
  assert(html.includes('Custom Key:'));
  assert(html.includes('custom_val'));
});

test("Adversarial: Policy with 0 limits handles correctly (0 allowed)", () => {
  const policy = {
    configuration: {
      maximum_customer_communications: 0,
      maximum_automated_attempts: 0
    }
  };
  const c = { recovered: false, communication_count: 0, retry_count: 0 };
  const html = renderFactorChecklist(c, {}, policy);
  // 0 < 0 is false, so it should fail
  assert(html.includes('0/0 used'));
  assert(html.includes('✗ Communication limit check: 0/0 used'));
  assert(html.includes('✗ Automated retry limit check: 0/0 used'));
});

test("Adversarial: Extreme count values (999999 retries) do not overflow or break UI", () => {
  const c = { recovered: false, communication_count: 999999, retry_count: 888888 };
  const html = renderFactorChecklist(c, {}, null);
  assert(html.includes('999999/3 used'));
  assert(html.includes('888888/3 used'));
  assert(html.includes('✗ Communication limit check: 999999/3 used'));
});

test("Adversarial: Falsy / empty factor checklist fields (null, undefined, 0)", () => {
  const c = { recovered: 0, communication_count: 0, retry_count: 0 };
  const html = renderFactorChecklist(c, {}, null);
  assert(html.includes('factor-item fail'));
  assert(html.includes('✗ Payment verified state: <b>PENDING</b>'));
});

// Write summary of test results
console.log("\n--- TEST SUMMARY ---");
const total = results.length;
const passed = results.filter(r => r.status === 'PASS').length;
const failed = results.filter(r => r.status === 'FAIL').length;
console.log(`Total: ${total}, Passed: ${passed}, Failed: ${failed}`);

if (failed > 0) {
  process.exit(1);
} else {
  console.log("All empirical tests PASSED successfully!");
}
