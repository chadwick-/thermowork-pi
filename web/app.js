/* Therm display logic.
 *
 * Polls the local server, which holds the API key and does the real talking to
 * ThermoWorks. This page never sees a credential and never contacts the
 * internet directly.
 *
 * The one rule that matters: if the server does not give us a trustworthy
 * number, show why instead of showing a number.
 */
"use strict";

const UI_POLL_MS = 3000;       // local request; cheap, keeps the clock honest
const RETRY_MS = 5000;         // when the local server is unreachable

const app = document.getElementById("app");
const panelsEl = document.getElementById("panels");
const connDot = document.getElementById("conn-dot");
const connText = document.getElementById("conn-text");
const dataAgeEl = document.getElementById("data-age");
const clockEl = document.getElementById("clock");
const bannerEl = document.getElementById("banner");
const bannerTextEl = document.getElementById("banner-text");
const template = document.getElementById("panel-template");

let focusedPanel = null;
let lastPayload = null;

/* ---------- rendering ---------- */

const CONNECTION_LABEL = {
  ok: "Connected",
  stale: "Data may be out of date",
  error: "Cannot reach ThermoWorks",
  auth_error: "API key rejected",
  starting: "Connecting…",
};

const STATE_MESSAGE = {
  no_probe: "NO PROBE",
  missing: "NOT FOUND",
  unknown: "WAITING…",
};

function panelKey(panel) {
  return `${panel.name}\u0000${panel.device}`;
}

/* Build or update the panel elements, reusing nodes so we are not rebuilding
 * the DOM every three seconds on a Pi 3. */
function renderPanels(panels) {
  const existing = new Map();
  for (const node of panelsEl.querySelectorAll(".panel")) {
    existing.set(node.dataset.key, node);
  }

  const seen = new Set();

  panels.forEach((panel, index) => {
    const key = panelKey(panel);
    seen.add(key);

    let node = existing.get(key);
    if (!node) {
      node = template.content.firstElementChild.cloneNode(true);
      node.dataset.key = key;
      node.addEventListener("click", () => toggleFocus(node));
      panelsEl.appendChild(node);
    }

    // Keep DOM order matching config order.
    if (panelsEl.children[index] !== node) {
      panelsEl.insertBefore(node, panelsEl.children[index] || null);
    }

    updatePanel(node, panel);
  });

  for (const [key, node] of existing) {
    if (!seen.has(key)) {
      if (node === focusedPanel) clearFocus();
      node.remove();
    }
  }
}

function updatePanel(node, panel) {
  node.dataset.state = panel.state;
  node.dataset.alarming = panel.alarming ? "true" : "false";

  setText(node.querySelector(".panel-name"), panel.name);

  const valueEl = node.querySelector(".value");
  const hasNumber = panel.state === "ok" || panel.state === "stale";

  if (hasNumber && panel.value !== null && panel.value !== undefined) {
    // Whole degrees: a tenth of a degree is noise on a cooking probe and
    // costs a lot of width on an 800px panel.
    setText(valueEl, String(Math.round(panel.value)));
  } else {
    setText(valueEl, STATE_MESSAGE[panel.state] || "--");
  }

  // Badge: the most useful single word about this panel right now.
  // Staleness outranks an alarm: the server clears the alarm flag on a stale
  // reading, and the badge should say which fact matters most right now.
  let badge = "";
  if (panel.state === "stale") badge = "STALE";
  else if (panel.alarming) badge = "ALARM";
  else if (panel.channelCount > 1) badge = `${panel.channelCount} sensors`;
  setText(node.querySelector(".panel-badge"), badge);

  // Footer left: when this reading was actually taken, not when we polled.
  let updated = "";
  if (panel.updatedAt) {
    updated = `updated: ${panel.updatedAt}`;
    if (panel.state === "stale" && panel.ageSeconds !== null) {
      updated += ` (${formatAge(panel.ageSeconds)} ago)`;
    }
  } else if (panel.detail) {
    updated = panel.detail;
  }
  setText(node.querySelector(".panel-updated"), updated);

  // Footer right: target, battery, signal.
  const meta = [];
  if (panel.alarmHigh !== null && panel.alarmHigh !== undefined) {
    meta.push(`target ${Math.round(panel.alarmHigh)}°`);
  }
  if (panel.battery !== null && panel.battery !== undefined) {
    meta.push(`${panel.battery}%`);
  }
  if (panel.signal !== null && panel.signal !== undefined) {
    meta.push(`${panel.signal}dBm`);
  }
  setText(node.querySelector(".panel-meta"), meta.join("  ·  "));
}

/* Avoid touching the DOM when nothing changed: cheaper, and stops the
 * screen-reader live region from re-announcing identical text. */
function setText(el, text) {
  if (el && el.textContent !== text) el.textContent = text;
}

function formatAge(seconds) {
  if (seconds === null || seconds === undefined) return "";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

function render(payload) {
  lastPayload = payload;

  const connection = payload.connection || "error";
  app.dataset.connection = connection;
  setText(connText, CONNECTION_LABEL[connection] || connection);
  setText(clockEl, payload.serverTime || "--:--");

  if (payload.dataAgeText && connection !== "ok") {
    setText(dataAgeEl, `data ${payload.dataAgeText} old`);
  } else if (payload.lastSuccessAt) {
    setText(dataAgeEl, `last poll ${payload.lastSuccessAt}`);
  } else {
    setText(dataAgeEl, "");
  }

  // Only show the banner for things the user can act on, and keep the
  // actionable sentence first.
  if (connection === "auth_error") {
    showBanner(
      `${payload.error || "The API key was rejected."}\n` +
      "Create a new key in the ThermoWorks Cloud web app under " +
      "Account Settings → Integrations, then update config.toml and restart."
    );
  } else if (payload.error) {
    showBanner(payload.error);
  } else {
    hideBanner();
  }

  renderPanels(payload.panels || []);
}

function showBanner(text) {
  setText(bannerTextEl, text);
  bannerEl.hidden = false;
}

function hideBanner() {
  bannerEl.hidden = true;
}

/* ---------- touch interaction ---------- */

function toggleFocus(node) {
  if (focusedPanel === node) {
    clearFocus();
    return;
  }
  if (focusedPanel) focusedPanel.classList.remove("is-focused");
  focusedPanel = node;
  node.classList.add("is-focused");
  panelsEl.dataset.focused = "true";
}

function clearFocus() {
  if (focusedPanel) focusedPanel.classList.remove("is-focused");
  focusedPanel = null;
  delete panelsEl.dataset.focused;
}

// Tapping the status bar forces an immediate server-side refresh.
document.querySelector(".bar").addEventListener("click", () => {
  fetch("/api/refresh").catch(() => {});
  poll();
});

// Escape leaves a focused panel, handy when a keyboard is attached.
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") clearFocus();
});

/* ---------- polling ---------- */

let timer = null;

function schedule(ms) {
  if (timer) clearTimeout(timer);
  timer = setTimeout(poll, ms);
}

async function poll() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
    schedule(UI_POLL_MS);
  } catch (error) {
    // The local server is down or restarting. Say so plainly, and keep the
    // last known readings on screen rather than blanking the display.
    app.dataset.connection = "error";
    setText(connText, "Display service not responding");
    showBanner(
      "Lost contact with the therm service on this Pi. " +
      "Retrying… (check: systemctl status therm)"
    );
    schedule(RETRY_MS);
  }
}

// Re-poll immediately when the page becomes visible again, so a woken screen
// never shows a stale number.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) poll();
});

poll();
