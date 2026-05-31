/* SeismicLog frontend.  Vanilla JS, Leaflet 1.9.  No build step.
   Two tabs (feed, watches) controlled by URL hash. */

(() => {
  "use strict";

  // ---------- State ----------
  const state = {
    days: 1,
    minMag: 2.5,
    events: [],
    source: "seed",
    feedMap: null,
    feedMarkers: new Map(),    // event id -> L.circleMarker
    watches: [],
    activeWatchId: null,
    watchMap: null,
    watchMapMarkers: [],
  };

  // ---------- Severity ----------
  const SEV_COLORS = {
    micro: "#9aa0a6",
    minor: "#1a73e8",
    light: "#f9a825",
    moderate: "#e8731c",
    strong: "#c83737",
    major: "#7c1d6f",
  };
  function severityFromMag(mag) {
    if (mag < 3) return "micro";
    if (mag < 4) return "minor";
    if (mag < 5) return "light";
    if (mag < 6) return "moderate";
    if (mag < 7) return "strong";
    return "major";
  }

  // ---------- Fetch helpers ----------
  async function getJSON(url) {
    const resp = await fetch(url, { headers: { Accept: "application/json" } });
    if (!resp.ok) {
      let msg = `HTTP ${resp.status}`;
      try { const j = await resp.json(); if (j && j.error) msg = j.error; } catch (_) { /* ignore */ }
      throw new Error(msg);
    }
    return resp.json();
  }
  async function postJSON(url, body) {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: body == null ? undefined : JSON.stringify(body),
    });
    if (!resp.ok) {
      let payload = null;
      try { payload = await resp.json(); } catch (_) { /* ignore */ }
      const err = new Error((payload && payload.error) || `HTTP ${resp.status}`);
      err.field = payload && payload.field;
      err.status = resp.status;
      throw err;
    }
    if (resp.status === 204) return null;
    return resp.json();
  }
  async function deleteReq(url) {
    const resp = await fetch(url, { method: "DELETE" });
    if (!resp.ok && resp.status !== 204) {
      throw new Error(`HTTP ${resp.status}`);
    }
  }

  // ---------- Health ----------
  async function refreshHealth() {
    const dot = document.getElementById("health-dot");
    const label = document.getElementById("health-label");
    try {
      const h = await getJSON("/api/health");
      dot.classList.add("ok");
      label.textContent = `ok · ${h.event_count} events · ${h.watch_count} watches`;
    } catch (e) {
      dot.classList.remove("ok");
      label.textContent = "offline";
    }
  }

  // ---------- Tab routing ----------
  function applyTabFromHash() {
    const hash = (window.location.hash || "#feed").replace("#", "");
    const tab = hash === "watches" ? "watches" : "feed";
    document.body.dataset.tab = tab;
    if (tab === "watches") {
      void loadWatches();
    } else {
      // Recompute size of leaflet maps after tab swap.
      if (state.feedMap) setTimeout(() => state.feedMap.invalidateSize(), 50);
    }
  }
  window.addEventListener("hashchange", applyTabFromHash);

  // ---------- Feed map ----------
  function initFeedMap() {
    state.feedMap = L.map("map", { worldCopyJump: true }).setView([20, 0], 2);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 12,
      attribution: '&copy; OpenStreetMap contributors',
    }).addTo(state.feedMap);
  }

  function clearFeedMarkers() {
    for (const m of state.feedMarkers.values()) {
      state.feedMap.removeLayer(m);
    }
    state.feedMarkers.clear();
  }

  function radiusForMag(mag) {
    const r = 4 + (mag - 2.5) * 3;
    return Math.max(4, Math.min(28, r));
  }

  function renderFeedMarkers() {
    if (!state.feedMap) return;
    clearFeedMarkers();
    for (const e of state.events) {
      const sev = e.severity || severityFromMag(e.magnitude);
      const m = L.circleMarker([e.lat, e.lng], {
        radius: radiusForMag(e.magnitude),
        fillColor: SEV_COLORS[sev],
        fillOpacity: 0.65,
        color: "#1a1a1a",
        weight: 1,
      });
      m.bindPopup(
        `<div style="font-family:'Public Sans',sans-serif;font-size:13px">
           <div><strong>${escapeHtml(e.place || "")}</strong></div>
           <div>Magnitude <span class="mono">${e.magnitude.toFixed(1)}</span> &middot;
                Depth <span class="mono">${e.depth_km.toFixed(1)} km</span></div>
           <div class="muted">${formatLocalTime(e.occurred_at)}</div>
         </div>`
      );
      m.on("click", () => selectEventRow(e.id));
      m.addTo(state.feedMap);
      state.feedMarkers.set(e.id, m);
    }
  }

  // ---------- Feed table ----------
  function renderFeedTable() {
    const tbody = document.getElementById("events-tbody");
    const empty = document.getElementById("events-empty");
    const countEl = document.getElementById("result-count");
    const srcEl = document.getElementById("result-source");

    tbody.innerHTML = "";
    countEl.textContent = state.events.length;
    srcEl.textContent = `(${state.source})`;

    if (state.events.length === 0) {
      empty.classList.remove("hidden");
      return;
    }
    empty.classList.add("hidden");

    const frag = document.createDocumentFragment();
    for (const e of state.events) {
      const tr = document.createElement("tr");
      tr.dataset.eventId = String(e.id);
      tr.addEventListener("click", () => {
        selectEventRow(e.id);
        if (state.feedMap) {
          state.feedMap.flyTo([e.lat, e.lng], 7, { duration: 0.8 });
          const m = state.feedMarkers.get(e.id);
          if (m) m.openPopup();
        }
      });
      const sev = e.severity || severityFromMag(e.magnitude);
      tr.innerHTML = `
        <td class="mono">${formatUtcShort(e.occurred_at)}</td>
        <td><span class="severity-pill sev-${sev}"></span><span class="mono">${e.magnitude.toFixed(1)}</span></td>
        <td class="mono">${e.depth_km.toFixed(1)} km</td>
        <td>${escapeHtml(e.place || "")}</td>
        <td><a href="https://earthquake.usgs.gov/earthquakes/eventpage/${encodeURIComponent(e.usgs_id)}"
               target="_blank" rel="noopener">view &#x2197;</a></td>
      `;
      frag.appendChild(tr);
    }
    tbody.appendChild(frag);
  }

  function selectEventRow(eventId) {
    const tbody = document.getElementById("events-tbody");
    for (const tr of tbody.querySelectorAll("tr[aria-selected='true']")) {
      tr.removeAttribute("aria-selected");
    }
    const target = tbody.querySelector(`tr[data-event-id='${eventId}']`);
    if (target) {
      target.setAttribute("aria-selected", "true");
      target.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }

  function showSkeletonRows() {
    const tbody = document.getElementById("events-tbody");
    tbody.innerHTML = "";
    for (let i = 0; i < 6; i++) {
      const tr = document.createElement("tr");
      tr.className = "skeleton-row";
      tr.innerHTML = `<td><div></div></td><td><div></div></td><td><div></div></td><td><div></div></td><td><div></div></td>`;
      tbody.appendChild(tr);
    }
  }

  function showBanner(text, isWarning = true) {
    const banner = document.getElementById("feed-banner");
    banner.innerHTML = `<span>${escapeHtml(text)}</span><button class="dismiss" type="button" aria-label="dismiss">x</button>`;
    banner.classList.remove("hidden");
    banner.querySelector(".dismiss").addEventListener("click", () => banner.classList.add("hidden"));
  }

  // ---------- Load events ----------
  async function loadEvents() {
    showSkeletonRows();
    try {
      const data = await getJSON(`/api/events?days=${state.days}&min_mag=${state.minMag}`);
      state.events = data.events || [];
      state.source = data.source || "seed";
      if (state.source === "seed" && state.events.length > 0) {
        showBanner("Could not reach USGS. Showing seeded demo data from the last 30 days.");
      }
      renderFeedTable();
      renderFeedMarkers();
    } catch (e) {
      state.events = [];
      state.source = "seed";
      renderFeedTable();
      showBanner("Failed to load events: " + e.message);
    }
  }

  // ---------- Refresh USGS ----------
  async function doRefresh() {
    const btn = document.getElementById("refresh-btn");
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = "Refreshing...";
    try {
      const r = await postJSON("/api/events/refresh", null);
      if (r && r.note) showBanner(r.note);
      await loadEvents();
    } catch (e) {
      showBanner("Refresh failed: " + e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = orig;
    }
  }

  // ---------- Filter wiring ----------
  function wireFilters() {
    const seg = document.getElementById("window-seg");
    seg.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-days]");
      if (!btn) return;
      for (const b of seg.querySelectorAll("button")) b.classList.remove("active");
      btn.classList.add("active");
      state.days = parseInt(btn.dataset.days, 10);
      void loadEvents();
    });

    const range = document.getElementById("minmag-range");
    const label = document.getElementById("minmag-label");
    range.addEventListener("input", () => {
      state.minMag = parseFloat(range.value);
      label.textContent = `M ≥ ${state.minMag.toFixed(1)}`;
    });
    range.addEventListener("change", () => { void loadEvents(); });

    document.getElementById("refresh-btn").addEventListener("click", doRefresh);
  }

  // ---------- Watches ----------
  async function loadWatches() {
    try {
      const data = await getJSON("/api/watch");
      state.watches = data.watches || [];
      renderWatchList();
      if (state.activeWatchId && state.watches.some(w => w.id === state.activeWatchId)) {
        await loadWatchDetail(state.activeWatchId);
      } else if (state.watches.length > 0) {
        await loadWatchDetail(state.watches[0].id);
      } else {
        document.getElementById("watch-detail").innerHTML =
          '<div class="empty-pane">Select a watch on the left, or add a new one.</div>';
      }
    } catch (e) {
      document.getElementById("watch-detail").innerHTML =
        `<div class="empty-pane">Failed to load watches: ${escapeHtml(e.message)}</div>`;
    }
  }

  function renderWatchList() {
    const ul = document.getElementById("watch-list");
    ul.innerHTML = "";
    if (state.watches.length === 0) {
      const empty = document.createElement("li");
      empty.className = "empty-watch-card";
      empty.style.height = "auto";
      empty.style.borderLeft = "1px dashed var(--border)";
      empty.textContent = "No watches yet. Add an address you care about.";
      ul.appendChild(empty);
      return;
    }
    for (const w of state.watches) {
      const li = document.createElement("li");
      if (state.activeWatchId === w.id) li.classList.add("active");
      li.innerHTML = `
        <div class="watch-label">${escapeHtml(w.label)}</div>
        <div class="watch-address">${escapeHtml(w.address)}</div>
      `;
      li.addEventListener("click", () => { void loadWatchDetail(w.id); });
      ul.appendChild(li);
    }
  }

  async function loadWatchDetail(id) {
    state.activeWatchId = id;
    renderWatchList();
    const pane = document.getElementById("watch-detail");
    pane.innerHTML = '<div class="empty-pane">Loading...</div>';
    try {
      const w = await getJSON(`/api/watch/${id}`);
      renderWatchDetail(w);
    } catch (e) {
      pane.innerHTML = `<div class="empty-pane">Failed: ${escapeHtml(e.message)}</div>`;
    }
  }

  function renderWatchDetail(w) {
    const pane = document.getElementById("watch-detail");
    const a = w.assessment;
    const maxMagColor = a && a.max_magnitude != null
      ? SEV_COLORS[severityFromMag(a.max_magnitude)] : "var(--text-muted)";

    pane.innerHTML = `
      <div class="watch-header">
        <div>
          <div class="watch-title">${escapeHtml(w.label)}</div>
          <div class="watch-sub">${escapeHtml(w.address)}</div>
        </div>
        <button class="watch-delete" type="button" id="watch-delete-btn">Delete</button>
      </div>

      <div id="watch-map"></div>

      <div class="tile-grid">
        <div class="tile">
          <div class="tile-title">M&ge;4 events, last 30 y</div>
          <div class="tile-value">${a ? a.n_events_30y : "-"}</div>
          <div class="tile-sub">within 100 km</div>
        </div>
        <div class="tile">
          <div class="tile-title">Max magnitude</div>
          <div class="tile-value" style="color:${maxMagColor}">${a && a.max_magnitude != null ? a.max_magnitude.toFixed(1) : "-"}</div>
          <div class="tile-sub">${a && a.max_magnitude_date ? formatUtcShort(a.max_magnitude_date) : "no event recorded"}</div>
        </div>
        <div class="tile">
          <div class="tile-title">p(M&ge;5 in 30 y)</div>
          <div class="tile-value">${a ? a.p_m5_30y_label : "-"}</div>
          <div class="tile-sub">rough heuristic; not an official USGS forecast</div>
        </div>
        <div class="tile">
          <div class="tile-title">Soil class</div>
          <div class="tile-value">${a ? escapeHtml((a.soil_class || "").split(" ")[0]) : "-"}</div>
          <div class="tile-sub">${a ? escapeHtml(a.soil_class.replace(/^./, "").replace(/^[A-Z]\s*[—\-]\s*/, "")) : ""}</div>
        </div>
      </div>

      <div class="briefing">
        ${a && a.llm_summary
          ? renderParagraphs(a.llm_summary)
          : '<p class="muted">Generating briefing...</p>'}
        ${a && a.llm_model
          ? `<div class="briefing-meta">Generated by ${escapeHtml(a.llm_model)} &middot; cached ${formatRelative(a.computed_at)}</div>`
          : ""}
      </div>

      <div class="checklist">
        <div class="checklist-header">
          <div class="checklist-title">Personal prep checklist</div>
          <div class="checklist-sub">Generated from this watch's risk tier and soil class.</div>
        </div>
        <div class="checklist-row">
          <label for="checklist-type" class="checklist-label">Building type:</label>
          <select id="checklist-type" class="checklist-select">
            <option value="apartment">Apartment</option>
            <option value="house">House</option>
            <option value="office">Office</option>
          </select>
          <button id="checklist-btn" class="btn-primary" type="button">Generate</button>
        </div>
        <div id="checklist-out" class="checklist-out"></div>
      </div>

      <div class="recompute-row">
        <button id="recompute-btn" class="btn-primary" type="button">Recompute</button>
      </div>
    `;

    // Wire delete + recompute
    document.getElementById("watch-delete-btn").addEventListener("click", async () => {
      if (!confirm(`Delete watch "${w.label}"?`)) return;
      try {
        await deleteReq(`/api/watch/${w.id}`);
        state.activeWatchId = null;
        await loadWatches();
      } catch (e) { alert("Delete failed: " + e.message); }
    });
    document.getElementById("checklist-btn").addEventListener("click", async (ev) => {
      const btn = ev.currentTarget;
      const out = document.getElementById("checklist-out");
      const sel = document.getElementById("checklist-type");
      const buildingType = sel.value || "apartment";
      btn.disabled = true;
      const orig = btn.textContent;
      btn.textContent = "Generating...";
      out.innerHTML = '<div class="checklist-pending">Generating checklist...</div>';
      try {
        const data = await postJSON(`/api/watch/${w.id}/checklist?building_type=${encodeURIComponent(buildingType)}`, null);
        const tierClass = `checklist-tier checklist-tier--${data.risk_tier}`;
        const itemsHtml = (data.items || []).map(
          (it) => `<li>${escapeHtml(it)}</li>`
        ).join("");
        out.innerHTML = `
          <div class="${tierClass}">Risk tier: ${escapeHtml(data.risk_tier)}</div>
          <ol class="checklist-list">${itemsHtml}</ol>
          <div class="checklist-meta">Model: ${escapeHtml(data.llm_model)}</div>
        `;
      } catch (e) {
        out.innerHTML = `<div class="checklist-err">${escapeHtml(e.message || "Generation failed.")}</div>`;
      } finally {
        btn.disabled = false;
        btn.textContent = orig;
      }
    });
    document.getElementById("recompute-btn").addEventListener("click", async (ev) => {
      const btn = ev.currentTarget;
      btn.disabled = true;
      const orig = btn.textContent;
      btn.textContent = "Recomputing...";
      try {
        const fresh = await postJSON(`/api/watch/${w.id}/recompute`, null);
        renderWatchDetail(fresh);
      } catch (e) {
        alert("Recompute failed: " + e.message);
        btn.disabled = false;
        btn.textContent = orig;
      }
    });

    // Mini-map
    setTimeout(() => initWatchMap(w), 30);
  }

  function renderParagraphs(text) {
    return text.split(/\n\s*\n/).map(p =>
      `<p>${escapeHtml(p.trim())}</p>`
    ).join("");
  }

  function initWatchMap(w) {
    const el = document.getElementById("watch-map");
    if (!el) return;
    if (state.watchMap) { state.watchMap.remove(); state.watchMap = null; }
    state.watchMap = L.map("watch-map").setView([w.lat, w.lng], 7);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 12,
      attribution: '&copy; OpenStreetMap contributors',
    }).addTo(state.watchMap);
    L.marker([w.lat, w.lng]).addTo(state.watchMap);
    L.circle([w.lat, w.lng], {
      radius: 100000,
      color: "#1a73e8",
      weight: 1,
      fillOpacity: 0.08,
    }).addTo(state.watchMap);
  }

  // ---------- Add-watch modal ----------
  function wireModal() {
    const backdrop = document.getElementById("modal-backdrop");
    const cancel = document.getElementById("modal-cancel");
    const form = document.getElementById("add-watch-form");
    const errLabel = document.getElementById("err-label");
    const errAddr = document.getElementById("err-address");

    document.getElementById("add-watch-btn").addEventListener("click", () => {
      errLabel.textContent = ""; errAddr.textContent = "";
      form.reset();
      backdrop.classList.remove("hidden");
      document.getElementById("label-input").focus();
    });
    cancel.addEventListener("click", () => backdrop.classList.add("hidden"));
    backdrop.addEventListener("click", (ev) => {
      if (ev.target === backdrop) backdrop.classList.add("hidden");
    });

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      errLabel.textContent = ""; errAddr.textContent = "";
      const label = document.getElementById("label-input").value.trim();
      const address = document.getElementById("address-input").value.trim();
      const submit = document.getElementById("modal-submit");
      submit.disabled = true;
      const origText = submit.textContent;
      submit.textContent = "Adding...";
      try {
        const created = await postJSON("/api/watch", { label, address });
        backdrop.classList.add("hidden");
        state.activeWatchId = created.id;
        await loadWatches();
      } catch (e) {
        if (e.field === "label") errLabel.textContent = e.message;
        else if (e.field === "address") errAddr.textContent = e.message;
        else errAddr.textContent = e.message;
      } finally {
        submit.disabled = false;
        submit.textContent = origText;
      }
    });
  }

  // ---------- Formatting ----------
  function pad2(n) { return n < 10 ? "0" + n : "" + n; }
  function formatUtcShort(iso) {
    // expects YYYY-MM-DDTHH:MM:SSZ
    if (!iso) return "";
    const d = new Date(iso);
    return `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())} ${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
  }
  function formatLocalTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleString();
  }
  function formatRelative(iso) {
    if (!iso) return "";
    const t = new Date(iso).getTime();
    const diff = Date.now() - t;
    if (diff < 60_000) return "just now";
    const mins = Math.floor(diff / 60_000);
    if (mins < 60) return mins + " min ago";
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return hrs + " h ago";
    const days = Math.floor(hrs / 24);
    return days + " d ago";
  }
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ---------- Init ----------
  document.addEventListener("DOMContentLoaded", () => {
    initFeedMap();
    wireFilters();
    wireModal();
    applyTabFromHash();
    void refreshHealth();
    void loadEvents();
  });
})();
