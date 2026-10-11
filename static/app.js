let searchTimer = null;
let searchRestore = null;
let collectorPollTimer = null;
let collectorWasRunning = false;

const motionMedia = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
const motionLiveValues = new Map();
const motionProgressValues = new Map();
const motionSkeletonTimers = new WeakMap();
let motionToastHideTimer = null;
let motionRowsPrimed = false;

function reducedMotion() {
  return Boolean(motionMedia?.matches);
}

function positionNavIndicator() {
  const nav = document.querySelector("[data-nav-motion]");
  const indicator = document.getElementById("nav-active-indicator");
  const active = nav?.querySelector(".nav-link.active");
  if (!nav || !indicator || !active) {
    indicator?.classList.remove("ready");
    return;
  }
  const navRect = nav.getBoundingClientRect();
  const activeRect = active.getBoundingClientRect();
  indicator.style.setProperty("--nav-y", `${Math.round(activeRect.top - navRect.top)}px`);
  indicator.style.height = `${Math.round(activeRect.height)}px`;
  indicator.classList.add("ready");
}

function parseMotionNumber(text) {
  const raw = String(text || "").trim();
  const match = raw.match(/^([+-]?[\d,.]+(?:\.\d+)?)(.*)$/);
  if (!match) return null;
  const numericText = match[1].replace(/,/g, "");
  const value = Number(numericText);
  if (!Number.isFinite(value)) return null;
  const decimalPart = numericText.split(".")[1] || "";
  return {value, suffix:match[2] || "", decimals:decimalPart.length, grouped:match[1].includes(",")};
}

function formatMotionNumber(value, meta) {
  if (meta.grouped) {
    return `${value.toLocaleString(undefined, {minimumFractionDigits:meta.decimals, maximumFractionDigits:meta.decimals})}${meta.suffix}`;
  }
  return `${value.toFixed(meta.decimals)}${meta.suffix}`;
}

function animateLiveValues(forceFlash=false) {
  document.querySelectorAll("[data-live-value]").forEach((el, index) => {
    const key = el.dataset.liveKey || `${window.location.pathname}:${index}`;
    const targetText = el.textContent.trim();
    const parsed = parseMotionNumber(targetText);
    const previous = motionLiveValues.get(key);
    motionLiveValues.set(key, parsed ? {value:parsed.value, text:targetText} : {value:null, text:targetText});

    const changed = previous && ((parsed && previous.value !== null && Math.abs(previous.value - parsed.value) > 1e-9) || previous.text !== targetText);
    if (!changed && !forceFlash) return;

    el.classList.remove("value-flash");
    void el.offsetWidth;
    el.classList.add("value-flash");

    if (!changed || reducedMotion() || !parsed || previous?.value == null) return;
    const from = Number(previous.value);
    const to = parsed.value;
    const duration = 360;
    const start = performance.now();
    const step = now => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      el.textContent = formatMotionNumber(from + (to - from) * eased, parsed);
      if (t < 1) requestAnimationFrame(step);
      else el.textContent = targetText;
    };
    requestAnimationFrame(step);
  });
}

function motionProgressKey(bar, index) {
  if (bar.dataset.motionProgressKey) return bar.dataset.motionProgressKey;
  const scope = bar.closest("article,section,.indexer-card,.provider-card,.sample-indexer-card,.panel") || bar.parentElement;
  const heading = scope?.querySelector("h1,h2,h3,strong,.provider-name,.indexer-name")?.textContent?.trim().replace(/\s+/g, " ").slice(0, 80) || "progress";
  return `${window.location.pathname}:${heading}:${index}`;
}

function animateProgressBars() {
  const selector = ".progress i,.provider-share-track i,.drawer-provider-track i,.sample-bar-track i,.provider-sample-progress i,.indexer-sample-progress i";
  document.querySelectorAll(selector).forEach((bar, index) => {
    const target = bar.style.width || getComputedStyle(bar).width;
    const key = motionProgressKey(bar, index);
    const previous = motionProgressValues.get(key);
    motionProgressValues.set(key, target);
    if (!previous || previous === target || reducedMotion()) return;
    bar.style.width = previous;
    requestAnimationFrame(() => requestAnimationFrame(() => { bar.style.width = target; }));
  });
}

function animateChartBars() {
  document.querySelectorAll("[data-chart-bar]").forEach((bar, index) => {
    bar.style.setProperty("--bar-delay", `${Math.min(index, 24) * 16}ms`);
  });
}

function animateDrawerContent() {
  const body = document.getElementById("drawer-body");
  if (!body) return;
  const items = body.querySelectorAll(".journey-attempt-card,.story-step,.stage-row,.drawer-provider-card,.drawer-provider-contribution");
  items.forEach((item, index) => {
    item.classList.add("drawer-motion-item");
    item.style.setProperty("--motion-order", String(Math.min(index, 14)));
  });
}

function primeMotionRows(forceHighlight=false) {
  const rows = [...document.querySelectorAll("[data-motion-row]")];
  if (!rows.length) return;
  if (!motionRowsPrimed) {
    motionRowsPrimed = true;
    return;
  }
  if (!forceHighlight) return;
  rows.slice(0, 4).forEach((row, index) => {
    row.classList.remove("motion-new-row");
    row.style.animationDelay = `${index * 55}ms`;
    void row.offsetWidth;
    row.classList.add("motion-new-row");
  });
}

function motionSwapTarget(detail) {
  const target = detail?.target;
  return target instanceof Element ? target : null;
}

function clearMotionSkeleton(target) {
  if (!target) return;
  const timer = motionSkeletonTimers.get(target);
  if (timer) clearTimeout(timer);
  motionSkeletonTimers.delete(target);
  target.querySelectorAll(":scope > .motion-skeleton").forEach(el => el.remove());
  target.classList.remove("motion-skeleton-host");
}

function scheduleMotionSkeleton(target) {
  if (!target || target.id === "page-content" || target.id === "collector-status" || reducedMotion()) return;
  if (!["DIV","SECTION","ARTICLE","MAIN"].includes(target.tagName)) return;
  const timer = setTimeout(() => {
    if (!target.isConnected || target.querySelector(":scope > .motion-skeleton")) return;
    const overlay = document.createElement("div");
    overlay.className = "motion-skeleton";
    overlay.setAttribute("aria-hidden", "true");
    overlay.innerHTML = "<i></i><i></i><i></i>";
    target.classList.add("motion-skeleton-host");
    target.appendChild(overlay);
  }, 180);
  motionSkeletonTimers.set(target, timer);
}

function setRouteProgress(active) {
  document.getElementById("motion-route-progress")?.classList.toggle("active", Boolean(active));
}

function currentHours() {
  const select = document.querySelector("#page-content [data-activity-window]");
  if (select?.value) return select.value;
  const page = document.querySelector("#page-content .page");
  if (page?.dataset.hours) return page.dataset.hours;
  return new URLSearchParams(window.location.search).get("hours")
      || localStorage.getItem("nzbview.activityHours")
      || "168";
}

function urlForPath(path, hours=currentHours()) {
  const url = new URL(path || "/", window.location.origin);
  url.searchParams.set("hours", hours);
  return url.pathname + url.search;
}

const metricHelp = new Map(Object.entries({
  "attempt success":"Successful SAB attempts divided by completed + failed attempts. Cancelled and unresolved attempts are not included.",
  "eventual success":"The share of unique releases that eventually completed, including releases that succeeded after an earlier failed attempt.",
  "final success":"The share of unique releases from this indexer whose final observed result completed.",
  "successful grabs":"Grabs from this indexer whose own SAB attempt completed successfully.",
  "eligible attempts":"Completed or failed SAB attempts that can safely be used for this indexer-quality calculation.",
  "repair needed":"The share of releases where SAB used one or more PAR repair blocks.",
  "heavy repair":"The share of releases where SAB used 10 or more PAR repair blocks.",
  "rescued from others":"Another indexer failed first, then this indexer supplied the release that completed.",
  "later rescued elsewhere":"This indexer failed first, then another indexer supplied the release that completed.",
  "same-index recoveries":"A failed attempt was later replaced successfully by another grab from the same indexer.",
  "exact / fallback":"How many releases were matched by exact title versus the conservative episode fallback matcher.",
  "query counter δ":"Change in Prowlarr's cumulative query counter across the selected time window.",
  "grab counter δ":"Change in Prowlarr's cumulative grab counter across the selected time window.",
  "query failures δ":"Change in Prowlarr's cumulative failed-query counter across the selected time window.",
  "grab failures δ":"Change in Prowlarr's cumulative failed-grab counter across the selected time window.",
  "all-time queries":"Prowlarr's current lifetime query counter for this indexer.",
  "all-time grabs":"Prowlarr's current lifetime grab counter for this indexer.",
  "availability":"Known-target searches where this indexer returned at least one wanted-quality NZB, divided by known-target searches where it was queried.",
  "queried searches":"How many of the selected logical searches actually queried this indexer.",
  "known-target searches":"Searches where nzbview can safely reconstruct what quality Sonarr or Radarr wanted.",
  "searches with match":"Known-target searches where this indexer returned at least one NZB matching the wanted quality.",
  "searches without match":"Known-target searches where this indexer returned no NZB matching the wanted quality.",
  "wanted-quality nzbs returned":"Individual NZB candidates that matched the wanted quality. One search can return many candidates.",
  "all nzbs returned":"All retained NZB candidates returned by the indexer before later Arr filtering. One search can return many candidates.",
  "2160p wanted nzbs":"Wanted-quality 2160p candidates returned across the selected searches.",
  "1080p wanted nzbs":"Wanted-quality 1080p candidates returned across the selected searches.",
  "search sessions":"Logical Sonarr/Radarr searches reconstructed from retained search evidence.",
  "searches with known target":"Searches where nzbview can identify the quality Sonarr/Radarr was actually looking for.",
  "no indexer had a match":"Known-target searches where none of the queried indexers returned the wanted quality.",
  "downloads analyzed":"Downloads included in the fixed recent sample after requiring retained provider contribution data.",
  "provider-data history":"How many retained terminal SAB downloads currently have a per-download provider split.",
  "data supplied":"Approximate data volume reconstructed from SAB's retained per-download provider split.",
  "provider use":"How many sampled downloads used more than one provider versus only one provider.",
  "share of observed data":"The provider's share of the approximate data volume in this sample.",
  "contributed to":"How many sampled downloads received at least some data from this provider.",
  "only provider":"Downloads where SAB recorded this provider as the only provider that supplied data.",
  "observed provider traffic":"Approximate sum of retained per-job provider contribution values inside the selected window.",
  "jobs with server breakdown":"Downloads where SAB History retained a provider/server contribution breakdown.",
  "downloads using multiple providers":"Downloads where two or more providers supplied data.",
  "downloads using one provider":"Downloads where only one provider supplied data.",
  "articles requested":"SAB's cumulative number of article requests sent to this provider.",
  "available counter":"Cumulative article requests SAB says were available from this provider.",
  "unavailable counter":"Cumulative article requests SAB says were unavailable from this provider.",
  "requested articles":"Article requests observed between the baseline and current provider snapshots for this window.",
  "api data delta":"Growth in SAB's cumulative data counter between the provider snapshots used for this window.",
  "observed snapshot span":"Actual time covered by the two provider snapshots used to calculate the selected-window figures.",
  "baseline snapshot":"The older provider snapshot used as the starting point for the selected-window calculation.",
  "observed contribution":"Approximate data this provider supplied to retained downloads inside the selected window.",
  "retry chain":"How many attempts nzbview grouped into the same acquisition chain for this release.",
  "repair":"The highest number of PAR repair blocks observed for that release or recovery chain.",
  "cross-indexer":"The failed attempt and successful replacement came from different indexers.",
  "same-indexer":"The failed attempt and successful replacement came from the same indexer.",
  "attribution confidence":"How safely nzbview could connect Arr/Prowlarr activity to the corresponding SAB release.",
  "reconstructed attempts":"SAB attempts rebuilt by combining retained logs, SAB History and release attribution evidence.",
  "provider snapshots":"Stored SAB provider/server counter snapshots used for availability and traffic-window calculations.",
  "log searches":"Search events reconstructed from retained Prowlarr/Sonarr/Radarr logs.",
  "pre-sab grab failures":"Release acquisition failures that happened before SAB received the NZB.",
  "explicit auto retries":"Automatic retry searches that have retained evidence of a previous failed download.",
  "sab unreachable events":"Times Sonarr/Radarr reported that SAB or the download client could not be reached.",
  "rotation continuity":"Whether retained rotating log segments still overlap the previously observed log family, reducing the chance of an evidence gap.",
  "release attribution":"How many release outcomes could be connected back to the correct Arr/Prowlarr release identity.",
  "collector freshness":"How long ago the collector last updated the nzbview database.",
  "retained log families":"Which SAB/Prowlarr/Sonarr/Radarr rotating log families currently have retained evidence in the database.",
  "no completed/failed result":"The grab could not be safely tied to a terminal SAB success/failure result, so it stays unknown rather than being counted as a failure.",
  "linked to sab":"How many selected grabs nzbview could safely connect to a retained SAB attempt.",
  "prowlarr api counter delta":"Changes in Prowlarr's cumulative API counters between stored snapshots around the selected window.",
  "sab history detail":"Retained SAB History API records used to enrich attempts with final state, timing, repair and provider details.",
  "prowlarr history":"Retained Prowlarr history records used for search/grab activity and indexer attribution.",
  "arr orchestration events":"Retained Sonarr/Radarr log events used to reconstruct searches, hand-offs, retries and imports.",
  "arr log segments":"Distinct retained Sonarr/Radarr rotating-log segments known to the collector.",
  "sab lifecycle events":"Parsed SAB log events such as download, verification, repair, unpack and final state.",
  "sab log segments":"Distinct retained SAB rotating-log segments known to the collector.",
  "prowlarr log segments":"Distinct retained Prowlarr rotating-log segments known to the collector.",
  "segments found last run":"How many SAB rotating-log segments the collector discovered on its latest run.",
  "segments read last run":"How many discovered SAB log segments were actually read on the latest run.",
  "retained lifecycle events":"Total parsed SAB lifecycle events currently retained in the database.",
  "retained overlap":"How many previously seen log segments still overlap the current rotating-log family.",
  "rss syncs observed":"Routine RSS-driven Arr search activity seen in the selected window.",
  "import successes":"Arr events confirming that completed media was successfully imported into the library.",
  "import failures":"Arr events reporting an import failure after download completion.",
  "arr history removals":"Arr history cleanup/removal actions retained from Sonarr/Radarr logs.",
  "queue-remove candidates":"SAB queue-removal events that matched the collector's cancellation-candidate pattern.",
  "directunpack abort confirms":"DirectUnpack abort evidence associated with a possible queue cancellation.",
  "accepted cancellations":"Cancellation candidates that passed the collector's retained-evidence checks.",
  "inserted / reclassified":"Cancellation records newly inserted or older records whose classification changed during the latest collector run."
}));

function normalizeMetricLabel(text) {
  return String(text || "").replace(/\?/g, "").replace(/\s+/g, " ").trim().toLowerCase();
}

function metricTipFloat() {
  let tip = document.getElementById("metric-tip-float");
  if (!tip) {
    tip = document.createElement("div");
    tip.id = "metric-tip-float";
    tip.className = "metric-tip-float";
    tip.hidden = true;
    document.body.appendChild(tip);
  }
  return tip;
}

function showMetricTipFloat(bubble) {
  const tip = metricTipFloat();
  tip.textContent = bubble.dataset.tip || "";
  tip.hidden = false;
  const rect = bubble.getBoundingClientRect();
  const width = Math.min(280, Math.max(210, window.innerWidth * .18));
  tip.style.width = `${width}px`;
  let left = rect.left + rect.width / 2 - width / 2;
  left = Math.max(8, Math.min(window.innerWidth - width - 8, left));
  let top = rect.top - tip.offsetHeight - 9;
  if (top < 8) top = rect.bottom + 9;
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function hideMetricTipFloat() {
  const tip = document.getElementById("metric-tip-float");
  if (tip) tip.hidden = true;
}

function installMetricHelpTips() {
  const selector = [
    ".metric-grid span", ".stat-card span", ".provider-overview-kpi span",
    ".provider-hero span", ".provider-contribution-head span", ".subsection-label", ".provider-subhead b", "table th",
    ".evidence-health-card span", ".activity-kpi span"
  ].join(",");
  document.querySelectorAll(selector).forEach(label => {
    if (label.querySelector?.(".help-tip") || label.closest(".help-page")) return;
    const key = normalizeMetricLabel(label.childNodes?.[0]?.textContent || label.textContent);
    const tip = metricHelp.get(key);
    if (!tip) return;
    const owner = label.parentElement;
    const descriptiveSmall = owner?.querySelector?.(":scope > small");
    if (descriptiveSmall && String(descriptiveSmall.textContent || "").trim().length >= 18) return;
    const bubble = document.createElement("i");
    bubble.className = "help-tip metric-help-tip";
    bubble.tabIndex = 0;
    bubble.dataset.tip = tip;
    bubble.textContent = "?";
    bubble.setAttribute("aria-label", `${String(label.textContent || "Metric").trim()} help`);
    bubble.addEventListener("pointerenter", () => showMetricTipFloat(bubble));
    bubble.addEventListener("pointerleave", hideMetricTipFloat);
    bubble.addEventListener("focus", () => showMetricTipFloat(bubble));
    bubble.addEventListener("blur", hideMetricTipFloat);
    label.append(" ", bubble);
  });
}

function metricOwnerName(el) {
  const owner = el.closest(".indexer-detail,.indexer-sample-card,.provider-identity-panel,.provider-sample-card,.stat-card,.provider-overview-kpi,.provider-subpanel,.sample-chart-card");
  if (!owner) return "metric";
  const heading = owner.querySelector("h1,h2,h3,.provider-subhead b,.subsection-label")?.textContent || "metric";
  return heading.replace(/\s+/g, " ").trim().slice(0, 90);
}

function installMetricMotion() {
  const selectors = [
    ".indexer-detail .hero-number", ".indexer-detail .successful-grab-badge strong", ".indexer-detail .metric-grid b",
    ".indexer-sample-card .indexer-sample-hero strong", ".indexer-sample-card .metric-grid b",
    ".stat-card strong", ".provider-overview-kpi strong", ".provider-hero strong", ".provider-metric-grid b",
    ".provider-contribution-head strong", ".provider-sample-card .provider-sample-hero strong", ".provider-sample-card .metric-grid b",
    ".sample-bar-value b"
  ];
  document.querySelectorAll(selectors.join(",")).forEach((el, index) => {
    if (el.hasAttribute("data-live-value")) return;
    const label = el.parentElement?.querySelector(":scope > span")?.textContent || el.parentElement?.querySelector(":scope > small")?.textContent || "value";
    el.setAttribute("data-live-value", "");
    el.dataset.liveKey = `${window.location.pathname}:${metricOwnerName(el)}:${normalizeMetricLabel(label)}:${index % 8}`;
  });
}

function renderInteractiveDonuts() {
  document.querySelectorAll(".interactive-donut").forEach(host => {
    if (host.dataset.rendered === "1") return;
    let items = [];
    try { items = JSON.parse(host.dataset.donutValues || "[]"); } catch (_) {}
    items = items.filter(x => Number(x?.value || 0) > 0);
    const total = Math.max(0, Number(host.dataset.donutTotal || items.reduce((a,x) => a + Number(x.value || 0), 0)));
    const toneColors = {completed:"#32c778", recovered:"#5f9eff", failed:"#ff6677", other:"#737b87"};
    const W = 220, H = 220, cx = 110, cy = 110, r = 72;
    let cumulative = 0;
    const segments = items.map((item, index) => {
      const value = Number(item.value || 0);
      const pct = total ? value * 100 / total : 0;
      const gap = items.length > 1 ? Math.min(1.2, pct * .08) : 0;
      const visible = Math.max(.01, pct - gap);
      const offset = -cumulative;
      cumulative += pct;
      const color = toneColors[item.tone] || chartColor(item.label || `segment-${index}`);
      return `<circle class="donut-segment" cx="${cx}" cy="${cy}" r="${r}" pathLength="100" fill="none" stroke="${color}" stroke-width="25" stroke-linecap="butt" stroke-dasharray="${visible} ${100-visible}" stroke-dashoffset="${offset}" data-donut-index="${index}" data-label="${chartEscape(item.label || "Result")}" data-value="${value}" data-pct="${pct.toFixed(3)}" tabindex="0" role="graphics-symbol" aria-label="${chartEscape(item.label || "Result")}: ${value} (${pct.toFixed(1)}%)"></circle>`;
    }).join("");
    host.innerHTML = `<div class="interactive-donut-stage"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${chartEscape(host.getAttribute("aria-label") || "Breakdown")}"><circle class="donut-track" cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke-width="25"/><g class="donut-segments" transform="rotate(-90 ${cx} ${cy})">${segments}</g><circle class="donut-hole" cx="${cx}" cy="${cy}" r="52"/></svg><div class="interactive-donut-center"><strong data-live-value data-live-key="${chartEscape(window.location.pathname)}:download-donut-total">${total.toLocaleString()}</strong><span>${chartEscape(host.dataset.donutLabel || "selected")}</span></div><div class="donut-tooltip" hidden></div></div>`;
    const segs = [...host.querySelectorAll(".donut-segment")];
    const tooltip = host.querySelector(".donut-tooltip");
    const clear = () => {
      segs.forEach(seg => seg.classList.remove("is-active", "is-muted"));
      if (tooltip) tooltip.hidden = true;
    };
    const show = (seg, event) => {
      segs.forEach(other => other.classList.toggle("is-muted", other !== seg));
      seg.classList.add("is-active");
      if (!tooltip) return;
      tooltip.innerHTML = `<strong>${chartEscape(seg.dataset.label || "Result")}</strong><span>${Number(seg.dataset.value || 0).toLocaleString()} items · ${Number(seg.dataset.pct || 0).toFixed(1)}%</span>`;
      tooltip.hidden = false;
      const rect = host.getBoundingClientRect();
      const x = event?.clientX ?? (rect.left + rect.width / 2);
      const y = event?.clientY ?? (rect.top + rect.height / 2);
      tooltip.style.left = `${Math.max(8, Math.min(rect.width - 168, x - rect.left + 12))}px`;
      tooltip.style.top = `${Math.max(8, Math.min(rect.height - 58, y - rect.top - 22))}px`;
    };
    segs.forEach((seg, index) => {
      seg.addEventListener("pointerenter", event => show(seg, event));
      seg.addEventListener("pointermove", event => show(seg, event));
      seg.addEventListener("pointerleave", clear);
      seg.addEventListener("focus", event => show(seg, event));
      seg.addEventListener("blur", clear);
      if (!reducedMotion() && seg.animate) {
        const target = seg.getAttribute("stroke-dasharray");
        seg.animate([{strokeDasharray:"0 100", opacity:.25},{strokeDasharray:target, opacity:1}], {duration:520, delay:index*80, easing:"cubic-bezier(.2,.8,.2,1)", fill:"both"});
      }
    });
    host.dataset.rendered = "1";
  });
}

function updateShell() {
  const page = document.querySelector("#page-content .page");
  if (page?.dataset.pageTitle) {
    const title = page.dataset.pageTitle;
    const h = document.getElementById("header-title");
    if (h) h.textContent = title;
    document.title = `${title} · SAB + Prowlarr Monitor`;
  }

  const rawPath = window.location.pathname;
  // Raw Search Results is a sub-view of Indexers. Keep the parent sidebar
  // item highlighted after HTMX navigation instead of clearing every item.
  const path = (rawPath === "/raw-search-results" || rawPath === "/search-coverage")
    ? "/indexers"
    : rawPath;
  document.querySelectorAll(".nav-link").forEach(link => {
    link.classList.toggle("active", link.dataset.path === path);
  });

  const hours = currentHours();
  localStorage.setItem("nzbview.activityHours", hours);
  syncWindowLinks(hours);
  syncTrendMetric();
  renderSparks();
  renderQualitySparks();
  renderMultiSeriesCharts();
  renderInteractiveDonuts();
  animateChartBars();
  installMetricHelpTips();
  installMetricMotion();
  syncScopeForms();
  reportHtmx();
  restoreSearchFocus();
  positionNavIndicator();
  const collectorRefresh = sessionStorage.getItem("nzbview.motionCollectorRefresh") === "1";
  animateLiveValues(collectorRefresh);
  animateProgressBars();
  primeMotionRows(collectorRefresh);
  if (collectorRefresh) sessionStorage.removeItem("nzbview.motionCollectorRefresh");
}

function syncWindowLinks(hours=currentHours()) {
  document.querySelectorAll("[data-window-nav]").forEach(link => {
    const path = link.dataset.path || link.getAttribute("href") || "/";
    link.setAttribute("href", urlForPath(path, hours));
  });
}

function restoreSearchFocus() {
  if (!searchRestore) return;
  const input = document.getElementById("release-search");
  if (!input) { searchRestore = null; return; }
  input.focus({preventScroll:true});
  const pos = Math.min(searchRestore.pos ?? input.value.length, input.value.length);
  try { input.setSelectionRange(pos, pos); } catch (_) {}
  searchRestore = null;
}

function reportHtmx() {
  const label = document.getElementById("htmx-label");
  const dot = document.getElementById("htmx-dot");
  if (!label || !dot) return;
  if (window.htmx) {
    label.textContent = `HTMX ${window.htmx.version || ""} active`.trim();
    dot.className = "runtime-dot good";
  } else {
    label.textContent = "HTMX unavailable";
    dot.className = "runtime-dot warn";
  }
}

function renderSparks() {
  document.querySelectorAll(".spark-chart").forEach(host => {
    if (host.dataset.rendered === "1") return;
    let rows = [];
    try { rows = JSON.parse(host.dataset.series || "[]"); } catch (_) {}
    const provider = host.dataset.provider;
    rows = rows.filter(x => x.provider === provider && x.ratio != null);

    if (rows.length < 2) {
      host.innerHTML = '<div class="empty-chart">Not enough snapshots inside this activity window yet.</div>';
      return;
    }

    const values = rows.map(x => Number(x.ratio));
    const lo = Math.min(...values), hi = Math.max(...values);
    const pad = Math.max((hi - lo) * .15, .2);
    const min = Math.max(0, lo - pad), max = Math.min(100, hi + pad);
    const W=700,H=210,L=48,R=15,T=18,B=30;
    const xx=i=>L+(W-L-R)*(i/(rows.length-1));
    const yy=v=>T+(H-T-B)*(1-(v-min)/Math.max(.0001,max-min));
    const pts=values.map((v,i)=>`${xx(i)},${yy(v)}`).join(" ");
    const dots=values.map((v,i)=>`<circle cx="${xx(i)}" cy="${yy(v)}" r="4" fill="currentColor"><title>${new Date(rows[i].time).toLocaleString()} · ${v.toFixed(3)}%</title></circle>`).join("");

    host.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="100%" preserveAspectRatio="none">
      <line x1="${L}" y1="${T}" x2="${L}" y2="${H-B}" stroke="#31353a"/>
      <line x1="${L}" y1="${H-B}" x2="${W-R}" y2="${H-B}" stroke="#31353a"/>
      <text x="5" y="${T+4}" fill="#8f96a1" font-size="10">${max.toFixed(2)}%</text>
      <text x="5" y="${H-B+4}" fill="#8f96a1" font-size="10">${min.toFixed(2)}%</text>
      <polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="2.5" vector-effect="non-scaling-stroke"/>
      ${dots}
    </svg>`;
    host.dataset.rendered = "1";
  });
}

const trendMetrics = {
  final_success: {label: "unique-release final success", field: "final_success", sample: "releases"},
  attempt_success: {label: "SAB attempt success", field: "attempt_success", sample: "attempts"},
  repair_rate: {label: "unique releases needing repair", field: "repair_rate", sample: "releases"},
  heavy_repair_rate: {label: "unique releases with heavy repair", field: "heavy_repair_rate", sample: "releases"},
};

function currentTrendMetric() {
  const select = document.querySelector("[data-trend-metric]");
  const stored = localStorage.getItem("nzbview.trendMetric");
  const candidate = select?.value || stored || "final_success";
  return trendMetrics[candidate] ? candidate : "final_success";
}

function syncTrendMetric() {
  const select = document.querySelector("[data-trend-metric]");
  if (!select) return;
  const stored = localStorage.getItem("nzbview.trendMetric");
  if (stored && trendMetrics[stored]) select.value = stored;
  const metric = currentTrendMetric();
  const meta = trendMetrics[metric];
  document.querySelectorAll("[data-trend-label]").forEach(el => {
    el.textContent = `${meta.label} · click to drill down`;
  });
}

function changeTrendMetric(metric) {
  if (!trendMetrics[metric]) return;
  localStorage.setItem("nzbview.trendMetric", metric);
  document.querySelectorAll(".quality-spark").forEach(host => {
    host.dataset.rendered = "";
  });
  syncTrendMetric();
  renderQualitySparks();
}

function renderQualitySparks() {
  const metric = currentTrendMetric();
  const meta = trendMetrics[metric];
  document.querySelectorAll(".quality-spark").forEach(host => {
    if (host.dataset.rendered === "1") return;
    let rows = [];
    try { rows = JSON.parse(host.dataset.series || "[]"); } catch (_) {}
    const indexer = host.dataset.indexer;
    rows = rows.filter(x => x.indexer === indexer && x[meta.field] != null);

    if (!rows.length) {
      host.innerHTML = `<div class="empty-chart">No populated ${meta.label} bucket in this window.</div>`;
      host.dataset.rendered = "1";
      return;
    }

    if (rows.length === 1) {
      const v = Number(rows[0][meta.field]);
      const sample = Number(rows[0][meta.sample] || 0);
      host.innerHTML = `<div class="single-trend">
        <small>${meta.label}</small>
        <strong>${v.toFixed(2)}%</strong>
        <span>${sample} ${meta.sample} in one populated bucket</span>
        <em>More populated buckets are needed for a line trend.</em>
      </div>`;
      host.dataset.rendered = "1";
      return;
    }

    const values = rows.map(x => Number(x[meta.field]));
    const W=620,H=160,L=38,R=12,T=14,B=24;
    const xx=i=>L+(W-L-R)*(i/(rows.length-1));
    const yy=v=>T+(H-T-B)*(1-v/100);
    const pts=values.map((v,i)=>`${xx(i)},${yy(v)}`).join(" ");
    const dots=values.map((v,i)=>{
      const sample = Number(rows[i][meta.sample] || 0);
      return `<circle cx="${xx(i)}" cy="${yy(v)}" r="4" fill="currentColor"><title>${new Date(rows[i].time).toLocaleString()} · ${v.toFixed(2)}% · ${sample} ${meta.sample}</title></circle>`;
    }).join("");
    host.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="100%" preserveAspectRatio="none">
      <line x1="${L}" y1="${T}" x2="${L}" y2="${H-B}" stroke="#31353a"/>
      <line x1="${L}" y1="${H-B}" x2="${W-R}" y2="${H-B}" stroke="#31353a"/>
      <line x1="${L}" y1="${yy(70)}" x2="${W-R}" y2="${yy(70)}" stroke="#2b3036" stroke-dasharray="4 5"/>
      <text x="2" y="${T+4}" fill="#8f96a1" font-size="10">100%</text>
      <text x="10" y="${H-B+4}" fill="#8f96a1" font-size="10">0%</text>
      <polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="2.5" vector-effect="non-scaling-stroke"/>
      ${dots}
    </svg>`;
    host.dataset.rendered = "1";
  });
}


function chartEscape(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

const chartPalette = [
  "#4cc9f0", "#f59e0b", "#a78bfa", "#34d399", "#fb7185",
  "#60a5fa", "#f472b6", "#facc15", "#2dd4bf", "#c084fc"
];
const chartColorAssignments = new Map();

function chartColor(name) {
  const semantic = {
    "Completed": "#35d07f",
    "Recovered": "#5aa7ff",
    "Failed-only": "#ff6677",
    "altHUB": "#4cc9f0",
    "NZBgeek": "#f59e0b",
    "NzbPlanet": "#a78bfa",
    "Newsdemon": "#34d399",
    "Newshosting": "#22d3ee",
  };
  if (semantic[name]) return semantic[name];
  if (chartColorAssignments.has(name)) return chartColorAssignments.get(name);
  const used = new Set(chartColorAssignments.values());
  let hash = 0;
  for (const ch of String(name || "series")) hash = ((hash << 5) - hash + ch.charCodeAt(0)) | 0;
  const startIndex = Math.abs(hash) % chartPalette.length;
  let color = chartPalette[startIndex];
  for (let i=0; i<chartPalette.length; i++) {
    const candidate = chartPalette[(startIndex + i) % chartPalette.length];
    if (!used.has(candidate)) { color = candidate; break; }
  }
  chartColorAssignments.set(name, color);
  return color;
}

function chartNumber(value, format) {
  const n = Number(value || 0);
  if (format === "percent3") return `${n.toFixed(3)}%`;
  if (format === "percent") return `${n.toFixed(1)}%`;
  if (format === "bytes") {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let x = n, i = 0;
    while (Math.abs(x) >= 1024 && i < units.length - 1) { x /= 1024; i++; }
    return i === 0 ? `${Math.round(x)} ${units[i]}` : `${x.toFixed(1)} ${units[i]}`;
  }
  return Math.round(n).toLocaleString();
}

function chartTimeLabel(value, spanMs) {
  const d = new Date(value);
  if (!Number.isFinite(d.getTime())) return "";
  if (spanMs <= 36 * 3600 * 1000) return d.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
  return d.toLocaleDateString([], {month:"short", day:"numeric"});
}

function chartDateKey(value) {
  const d = new Date(value);
  if (!Number.isFinite(d.getTime())) return "";
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
}

function chartTimeAxisTicks(times, spanMs) {
  if (!Array.isArray(times) || !times.length) return [];
  if (spanMs <= 36 * 3600 * 1000) {
    return times.map(ms => ({ms, label:chartTimeLabel(ms, spanMs)}));
  }

  // Once a chart spans multiple days, show every calendar date that actually
  // contains retained data. This avoids silently dropping dates just because
  // the chart has more than six buckets. Multiple buckets on the same day share
  // one centered date label.
  const groups = [];
  for (const ms of times) {
    const key = chartDateKey(ms);
    let group = groups[groups.length - 1];
    if (!group || group.key !== key) {
      group = {key, times:[]};
      groups.push(group);
    }
    group.times.push(ms);
  }

  return groups.map((group, index) => {
    const ms = group.times.reduce((sum, value) => sum + value, 0) / group.times.length;
    const d = new Date(group.times[Math.floor(group.times.length / 2)]);
    let label;
    if (groups.length <= 16) {
      label = d.toLocaleDateString([], {month:"short", day:"numeric"});
    } else {
      const prev = index ? new Date(groups[index - 1].times[0]) : null;
      const monthChanged = !prev || prev.getMonth() !== d.getMonth() || prev.getFullYear() !== d.getFullYear();
      label = monthChanged
        ? d.toLocaleDateString([], {month:"short", day:"numeric"})
        : d.toLocaleDateString([], {day:"numeric"});
    }
    return {ms, label};
  });
}


function renderGroupedBarChart(host) {
  let rows = [];
  try { rows = JSON.parse(host.dataset.series || "[]"); } catch (_) {}

  const seriesKey = host.dataset.seriesKey || "series";
  const valueKey = host.dataset.valueKey || "value";
  const format = host.dataset.valueFormat || "count";
  const scale = host.dataset.scale || "zero";
  const sampleLabel = String(host.dataset.sampleLabel || "").trim();
  const seriesLabelOverride = String(host.dataset.seriesLabel || "").trim();
  rows = rows
    .map(r => ({...r, _time:new Date(r.time), _series:seriesLabelOverride || String(r[seriesKey] ?? "Series"), _value:Number(r[valueKey])}))
    .filter(r => Number.isFinite(r._time.getTime()) && Number.isFinite(r._value));

  if (!rows.length) {
    host.innerHTML = '<div class="empty-chart">Not enough retained evidence in this activity window yet.</div>';
    host.dataset.rendered = "1";
    return;
  }

  const names = [...new Set(rows.map(r => r._series))].sort((a,b) => a.localeCompare(b));
  if (!(host._chartHiddenSeries instanceof Set)) host._chartHiddenSeries = new Set();
  const hidden = host._chartHiddenSeries;
  for (const name of [...hidden]) if (!names.includes(name)) hidden.delete(name);
  const visibleNames = names.filter(name => !hidden.has(name));
  const activeRows = rows.filter(r => !hidden.has(r._series));

  const legendHtml = names.map(name => {
    const color = chartColor(name);
    const off = hidden.has(name);
    return `<button type="button" class="chart-legend-item${off ? " is-hidden" : ""}" data-chart-series="${chartEscape(name)}" aria-pressed="${off ? "false" : "true"}" title="${off ? "Show" : "Hide"} ${chartEscape(name)}"><i style="background:${color}"></i><span>${chartEscape(name)}</span></button>`;
  }).join("");

  if (!activeRows.length) {
    host.innerHTML = `<div class="chart-legend">${legendHtml}</div><div class="empty-chart chart-all-hidden">All series are hidden. Click a legend item to show one.</div>`;
    host.querySelectorAll("[data-chart-series]").forEach(button => {
      button.addEventListener("click", () => {
        hidden.delete(button.dataset.chartSeries || "");
        host.dataset.rendered = "";
        renderGroupedBarChart(host);
      });
    });
    host.dataset.rendered = "1";
    return;
  }

  const allTimes = [...new Set(rows.map(r => r._time.getTime()))].sort((a,b) => a-b);
  const values = activeRows.map(r => r._value);
  let yMin = 0, yMax = Math.max(...values, 1);
  if (scale === "percent") {
    yMin = 0; yMax = 100;
  } else if (scale === "zoom-percent") {
    const lo = Math.min(...values), hi = Math.max(...values);
    const spread = Math.max(hi - lo, 0.5);
    const pad = Math.max(spread * 0.25, 0.25);
    yMin = Math.max(0, lo - pad);
    yMax = Math.min(100, hi + pad);
    if (yMax - yMin < 1) {
      const mid = (yMax + yMin) / 2;
      yMin = Math.max(0, mid - .5);
      yMax = Math.min(100, mid + .5);
    }
  } else {
    yMin = 0;
    yMax = Math.ceil(Math.max(...values, 1) * 1.15);
  }

  const W = 820, H = 250, L = 56, R = 18, T = 34, B = 42;
  const plotW = W - L - R, plotH = H - T - B;
  const groupW = plotW / Math.max(1, allTimes.length);
  const yy = v => T + plotH * (1 - (v - yMin) / Math.max(0.0001, yMax - yMin));
  const baseY = yy(yMin);
  const yTicks = 4;
  let grid = "";
  for (let i=0; i<=yTicks; i++) {
    const v = yMin + (yMax - yMin) * (i / yTicks);
    const y = yy(v);
    grid += `<line x1="${L}" y1="${y}" x2="${W-R}" y2="${y}" class="chart-grid-line"/>`;
    grid += `<text x="${L-8}" y="${y+4}" text-anchor="end" class="chart-axis-text">${chartEscape(chartNumber(v, format))}</text>`;
  }

  const timeMin = allTimes[0], timeMax = allTimes[allTimes.length - 1];
  const spanMs = Math.max(1, timeMax - timeMin);
  const xTickCount = Math.min(6, allTimes.length);
  const chosen = [];
  for (let i=0; i<xTickCount; i++) {
    const idx = xTickCount === 1 ? 0 : Math.round((allTimes.length - 1) * i / (xTickCount - 1));
    if (!chosen.includes(idx)) chosen.push(idx);
  }
  let xLabels = "";
  for (const idx of chosen) {
    const x = L + groupW * (idx + .5);
    xLabels += `<text x="${x}" y="${H-15}" text-anchor="middle" class="chart-axis-text">${chartEscape(chartTimeLabel(allTimes[idx], spanMs))}</text>`;
  }

  const rowsByKey = new Map();
  for (const r of activeRows) rowsByKey.set(`${r._time.getTime()}\u0000${r._series}`, r);
  const nSeries = Math.max(1, visibleNames.length);
  const usableGroupW = Math.min(groupW * .78, 190);
  const gap = nSeries > 1 ? Math.min(6, usableGroupW * .06) : 0;
  const barW = Math.max(2, Math.min(52, (usableGroupW - gap * (nSeries - 1)) / nSeries));
  const clusterW = barW * nSeries + gap * (nSeries - 1);
  let bars = "";
  let labels = "";

  allTimes.forEach((ms, ti) => {
    const center = L + groupW * (ti + .5);
    const clusterLeft = center - clusterW / 2;
    visibleNames.forEach((name, si) => {
      const r = rowsByKey.get(`${ms}\u0000${name}`);
      if (!r) return;
      const x = clusterLeft + si * (barW + gap);
      const y = yy(r._value);
      const h = Math.max(1.2, baseY - y);
      const color = chartColor(name);
      const id = `bar-${ti}-${si}`;
      const aria = `${name}, ${chartNumber(r._value, format)}, ${new Date(ms).toLocaleString()}`;
      bars += `<rect id="${id}" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barW.toFixed(2)}" height="${h.toFixed(2)}" rx="2" fill="${color}" class="chart-bar" tabindex="0" role="graphics-symbol" aria-label="${chartEscape(aria)}" data-chart-bar data-time="${ms}" data-series="${chartEscape(name)}" data-value="${r._value}" data-label-id="${id}-label"/>`;
      labels += `<text id="${id}-label" x="${(x + barW / 2).toFixed(2)}" y="${Math.max(13, y - 7).toFixed(2)}" text-anchor="middle" class="chart-bar-hover-value" visibility="hidden">${chartEscape(chartNumber(r._value, format))}</text>`;
    });
  });

  host.innerHTML = `<div class="chart-legend">${legendHtml}</div>
    <div class="chart-stage">
      <svg viewBox="0 0 ${W} ${H}" width="100%" height="100%" preserveAspectRatio="none" role="img" aria-label="${chartEscape(host.dataset.chartTitle || "Bar chart")}">
        ${grid}${xLabels}${bars}${labels}
      </svg>
      <div class="chart-tooltip" role="status" aria-live="polite" hidden></div>
    </div>`;

  host.querySelectorAll("[data-chart-series]").forEach(button => {
    button.addEventListener("click", () => {
      const name = button.dataset.chartSeries || "";
      if (hidden.has(name)) hidden.delete(name); else hidden.add(name);
      host.dataset.rendered = "";
      renderGroupedBarChart(host);
    });
  });

  const tooltip = host.querySelector(".chart-tooltip");
  const clearHover = () => {
    host.querySelectorAll("[data-chart-bar].is-hovered").forEach(el => el.classList.remove("is-hovered"));
    host.querySelectorAll(".chart-bar-hover-value").forEach(el => el.setAttribute("visibility", "hidden"));
    if (tooltip) tooltip.hidden = true;
  };
  const showBar = (bar, event) => {
    clearHover();
    bar.classList.add("is-hovered");
    const label = document.getElementById(bar.dataset.labelId || "");
    if (label) label.setAttribute("visibility", "visible");
    if (!tooltip) return;
    const ms = Number(bar.dataset.time || 0);
    const name = bar.dataset.series || "Series";
    const value = Number(bar.dataset.value || 0);
    const r = rowsByKey.get(`${ms}\u0000${name}`);
    const sample = sampleLabel && r?.sample != null
      ? `<small>${Number(r.sample).toLocaleString()} ${chartEscape(sampleLabel)}</small>` : "";
    const extra = r?.bytes_human ? `<small>${chartEscape(r.bytes_human)}</small>` : "";
    tooltip.innerHTML = `<strong>${chartEscape(new Date(ms).toLocaleString())}</strong><div class="chart-tooltip-row"><i style="background:${chartColor(name)}"></i><span>${chartEscape(name)}${sample}${extra}</span><b>${chartEscape(chartNumber(value, format))}</b></div>`;
    tooltip.hidden = false;
    const hostRect = host.getBoundingClientRect();
    const barRect = bar.getBoundingClientRect();
    const tooltipWidth = Math.min(280, Math.max(190, hostRect.width * .36));
    let left = barRect.left - hostRect.left + barRect.width / 2 + 12;
    if (left + tooltipWidth > hostRect.width - 8) left = barRect.left - hostRect.left - tooltipWidth - 12;
    left = Math.max(8, left);
    const pointerY = event?.clientY ?? barRect.top;
    const top = Math.max(34, Math.min(hostRect.height - 105, pointerY - hostRect.top - 20));
    tooltip.style.width = `${tooltipWidth}px`;
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  };

  host.querySelectorAll("[data-chart-bar]").forEach(bar => {
    bar.addEventListener("pointerenter", event => showBar(bar, event));
    bar.addEventListener("pointermove", event => showBar(bar, event));
    bar.addEventListener("pointerleave", clearHover);
    bar.addEventListener("focus", event => showBar(bar, event));
    bar.addEventListener("blur", clearHover);
  });

  host.dataset.rendered = "1";
}

function renderMultiSeriesChart(host) {
  const chartStyle = (host.dataset.chartStyle || "line").toLowerCase();
  if (chartStyle === "bar") {
    renderGroupedBarChart(host);
    return;
  }
  let rows = [];
  try { rows = JSON.parse(host.dataset.series || "[]"); } catch (_) {}

  const filterKey = host.dataset.filterKey || "";
  const filterValue = host.dataset.filterValue || "";
  if (filterKey && filterValue) rows = rows.filter(r => String(r?.[filterKey] ?? "") === filterValue);

  const seriesKey = host.dataset.seriesKey || "series";
  const valueKey = host.dataset.valueKey || "value";
  const format = host.dataset.valueFormat || "count";
  const scale = host.dataset.scale || "zero";
  const sampleLabel = String(host.dataset.sampleLabel || "").trim();
  const seriesLabelOverride = String(host.dataset.seriesLabel || "").trim();
  rows = rows
    .map(r => ({...r, _time:new Date(r.time), _series:seriesLabelOverride || String(r[seriesKey] ?? "Series"), _value:Number(r[valueKey])}))
    .filter(r => Number.isFinite(r._time.getTime()) && Number.isFinite(r._value));

  if (!rows.length) {
    host.innerHTML = '<div class="empty-chart">Not enough retained evidence in this activity window yet.</div>';
    host.dataset.rendered = "1";
    return;
  }

  const names = [...new Set(rows.map(r => r._series))].sort((a,b) => a.localeCompare(b));
  if (!(host._chartHiddenSeries instanceof Set)) host._chartHiddenSeries = new Set();
  const hidden = host._chartHiddenSeries;
  // Drop stale hidden names after a window/data change.
  for (const name of [...hidden]) if (!names.includes(name)) hidden.delete(name);
  const visibleNames = names.filter(name => !hidden.has(name));
  const activeRows = rows.filter(r => !hidden.has(r._series));

  const legendHtml = names.map(name => {
    const color = chartColor(name);
    const off = hidden.has(name);
    return `<button type="button" class="chart-legend-item${off ? " is-hidden" : ""}" data-chart-series="${chartEscape(name)}" aria-pressed="${off ? "false" : "true"}" title="${off ? "Show" : "Hide"} ${chartEscape(name)}"><i style="background:${color}"></i><span>${chartEscape(name)}</span></button>`;
  }).join("");

  if (!activeRows.length) {
    host.innerHTML = `<div class="chart-legend">${legendHtml}</div><div class="empty-chart chart-all-hidden">All series are hidden. Click a legend item to show one.</div>`;
    host.querySelectorAll("[data-chart-series]").forEach(button => {
      button.addEventListener("click", () => {
        hidden.delete(button.dataset.chartSeries || "");
        host.dataset.rendered = "";
        renderMultiSeriesChart(host);
      });
    });
    host.dataset.rendered = "1";
    return;
  }

  // Keep the x-axis stable while toggling series so comparisons do not jump.
  const allTimes = [...new Set(rows.map(r => r._time.getTime()))].sort((a,b) => a-b);
  const activeTimes = [...new Set(activeRows.map(r => r._time.getTime()))].sort((a,b) => a-b);
  const timeMin = allTimes[0], timeMax = allTimes[allTimes.length - 1];
  const spanMs = Math.max(1, timeMax - timeMin);
  const values = activeRows.map(r => r._value);
  let yMin = 0, yMax = Math.max(...values, 1);

  if (scale === "percent") {
    yMin = 0; yMax = 100;
  } else if (scale === "zoom-percent") {
    const lo = Math.min(...values), hi = Math.max(...values);
    const spread = Math.max(hi - lo, 0.5);
    const pad = Math.max(spread * 0.25, 0.25);
    yMin = Math.max(0, lo - pad);
    yMax = Math.min(100, hi + pad);
    if (yMax - yMin < 1) {
      const mid = (yMax + yMin) / 2;
      yMin = Math.max(0, mid - .5);
      yMax = Math.min(100, mid + .5);
    }
  } else {
    yMin = 0;
    yMax = Math.max(...values, 1);
    yMax = Math.ceil(yMax * 1.15);
  }

  const W = 820, H = 250, L = 56, R = 18, T = 26, B = 42;
  const plotW = W - L - R, plotH = H - T - B;
  const xx = ms => allTimes.length === 1 ? L + plotW / 2 : L + plotW * ((ms - timeMin) / spanMs);
  const yy = v => T + plotH * (1 - (v - yMin) / Math.max(0.0001, yMax - yMin));
  const yTicks = 4;
  let grid = "";
  for (let i=0; i<=yTicks; i++) {
    const v = yMin + (yMax - yMin) * (i / yTicks);
    const y = yy(v);
    grid += `<line x1="${L}" y1="${y}" x2="${W-R}" y2="${y}" class="chart-grid-line"/>`;
    grid += `<text x="${L-8}" y="${y+4}" text-anchor="end" class="chart-axis-text">${chartEscape(chartNumber(v, format))}</text>`;
  }

  let xLabels = "";
  const axisTicks = chartTimeAxisTicks(allTimes, spanMs);
  for (const tick of axisTicks) {
    const x = xx(tick.ms);
    xLabels += `<text x="${x}" y="${H-15}" text-anchor="middle" class="chart-axis-text chart-axis-time-text">${chartEscape(tick.label)}</text>`;
  }

  let paths = "";
  let defs = "";
  const areaMode = chartStyle === "area";
  for (const [seriesIndex, name] of visibleNames.entries()) {
    const color = chartColor(name);
    const points = rows.filter(r => r._series === name).sort((a,b) => a._time - b._time);
    if (!points.length) continue;
    const coords = points.map(r => `${xx(r._time.getTime()).toFixed(1)},${yy(r._value).toFixed(1)}`).join(" ");
    const safeId = `area-${seriesIndex}-${Math.abs([...name].reduce((a,c)=>((a*31)+c.charCodeAt(0))|0,0))}`;
    if (areaMode && points.length > 1) {
      defs += `<linearGradient id="${safeId}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${color}" stop-opacity=".24"/><stop offset="100%" stop-color="${color}" stop-opacity=".015"/></linearGradient>`;
      const firstX = xx(points[0]._time.getTime()).toFixed(1);
      const lastX = xx(points[points.length-1]._time.getTime()).toFixed(1);
      paths += `<polygon points="${firstX},${yy(yMin)} ${coords} ${lastX},${yy(yMin)}" fill="url(#${safeId})" class="chart-area" data-chart-series-shape="${chartEscape(name)}"/>`;
    }
    if (points.length > 1) {
      paths += `<polyline points="${coords}" fill="none" stroke="${color}" stroke-width="2.8" vector-effect="non-scaling-stroke" class="chart-line" data-chart-series-shape="${chartEscape(name)}"/>`;
    }
    points.forEach(r => {
      paths += `<circle cx="${xx(r._time.getTime())}" cy="${yy(r._value)}" r="3.6" fill="${color}" class="chart-point" data-chart-series-shape="${chartEscape(name)}"/>`;
    });
  }

  host.innerHTML = `<div class="chart-legend">${legendHtml}</div>
    <div class="chart-stage">
      <svg viewBox="0 0 ${W} ${H}" width="100%" height="100%" preserveAspectRatio="none" role="img" aria-label="${chartEscape(host.dataset.chartTitle || "Trend chart")}">
        <defs>${defs}</defs>${grid}${xLabels}${paths}
        <line x1="${L}" y1="${T}" x2="${L}" y2="${H-B}" class="chart-hover-line" visibility="hidden"/>
        <g class="chart-hover-points"></g>
        <rect x="${L}" y="${T}" width="${plotW}" height="${plotH}" class="chart-hit-area"/>
      </svg>
      <div class="chart-tooltip" role="status" aria-live="polite" hidden></div>
    </div>`;

  host.querySelectorAll("[data-chart-series]").forEach(button => {
    button.addEventListener("click", () => {
      const name = button.dataset.chartSeries || "";
      if (hidden.has(name)) hidden.delete(name); else hidden.add(name);
      host.dataset.rendered = "";
      renderMultiSeriesChart(host);
    });
  });

  const svg = host.querySelector("svg");
  const hit = host.querySelector(".chart-hit-area");
  const hoverLine = host.querySelector(".chart-hover-line");
  const hoverPoints = host.querySelector(".chart-hover-points");
  const tooltip = host.querySelector(".chart-tooltip");
  if (svg && hit && hoverLine && hoverPoints) {
    const nearestTime = target => {
      let best = activeTimes[0], dist = Math.abs(activeTimes[0] - target);
      for (let i=1; i<activeTimes.length; i++) {
        const d = Math.abs(activeTimes[i] - target);
        if (d < dist) { best = activeTimes[i]; dist = d; }
      }
      return best;
    };

    let lastHoverKey = "";
    const clearHover = () => {
      lastHoverKey = "";
      hoverLine.setAttribute("visibility", "hidden");
      hoverPoints.innerHTML = "";
      if (tooltip) tooltip.hidden = true;
    };

    const showHover = event => {
      const svgRect = svg.getBoundingClientRect();
      if (!svgRect.width || !svgRect.height) return;
      const viewX = (event.clientX - svgRect.left) / svgRect.width * W;
      const clampedX = Math.max(L, Math.min(W - R, viewX));
      const targetMs = allTimes.length === 1 ? allTimes[0] : timeMin + ((clampedX - L) / plotW) * spanMs;
      const ms = nearestTime(targetMs);
      const x = xx(ms);

      // A hover selection belongs to the whole time bucket, not one series.
      // Highlight every visible value at that timestamp and show all of them in
      // one stable tooltip.  This keeps comparisons intact while avoiding the
      // old per-point pop animation.
      const atTime = activeRows
        .filter(r => r._time.getTime() === ms)
        .sort((a,b) => names.indexOf(a._series) - names.indexOf(b._series));
      if (!atTime.length) { clearHover(); return; }

      const key = String(ms);
      if (key === lastHoverKey) return;
      lastHoverKey = key;

      hoverLine.setAttribute("x1", x);
      hoverLine.setAttribute("x2", x);
      hoverLine.setAttribute("visibility", "visible");
      hoverPoints.innerHTML = atTime.map(r =>
        `<circle cx="${x}" cy="${yy(r._value)}" r="6.4" fill="${chartColor(r._series)}" class="chart-hover-dot"/>`
      ).join("");

      if (tooltip) {
        const rowsHtml = atTime.map(r => {
          const sample = sampleLabel && Number.isFinite(Number(r.sample))
            ? `<small>${Number(r.sample).toLocaleString()} ${chartEscape(sampleLabel)}</small>` : "";
          const extra = r?.bytes_human ? `<small>${chartEscape(r.bytes_human)}</small>` : "";
          return `<div class="chart-tooltip-row"><i style="background:${chartColor(r._series)}"></i><span>${chartEscape(r._series)}${sample}${extra}</span><b>${chartEscape(chartNumber(r._value, format))}</b></div>`;
        }).join("");
        tooltip.innerHTML = `<strong>${chartEscape(new Date(ms).toLocaleString())}</strong>${rowsHtml}`;
        tooltip.hidden = false;

        const stage = tooltip.parentElement;
        const stageRect = stage?.getBoundingClientRect();
        if (stageRect?.width) {
          const pixelX = (x / W) * stageRect.width;
          const tooltipWidth = Math.min(300, Math.max(205, stageRect.width * .34));
          let left = pixelX + 14;
          if (left + tooltipWidth > stageRect.width - 8) left = pixelX - tooltipWidth - 14;
          left = Math.max(8, left);
          tooltip.style.width = `${tooltipWidth}px`;
          tooltip.style.left = `${left}px`;
          tooltip.style.top = `42px`;
        }
      }
    };

    hit.addEventListener("pointermove", showHover);
    hit.addEventListener("pointerenter", showHover);
    hit.addEventListener("pointerleave", clearHover);
  }

  host.dataset.rendered = "1";
}

function renderMultiSeriesCharts() {
  document.querySelectorAll(".multi-series-chart").forEach(host => {
    if (host.dataset.rendered === "1") return;
    renderMultiSeriesChart(host);
  });
}

function currentScopeState() {
  // The swappable analytics workspace is the freshest state after a partial
  // scope change. Fall back to the outer page for first render / older pages.
  const state = document.querySelector("#page-content [data-scope-state]")
      || document.querySelector("#page-content .page");
  const mode = state?.dataset.scopeMode === "sample" ? "sample" : "time";
  const type = state?.dataset.sampleType || "downloads";
  const n = /^\d+$/.test(state?.dataset.sampleN || "") ? state.dataset.sampleN : "100";
  return {mode, type, n};
}

function syncScopeFormVisibility(form) {
  if (!form?.matches?.("form.scope-form")) return;
  const mode = form.querySelector('[name="scope"]')?.value === "sample" ? "sample" : "time";
  form.querySelectorAll("[data-scope-time]").forEach(el => { el.hidden = mode !== "time"; });
  form.querySelectorAll("[data-scope-sample]").forEach(el => { el.hidden = mode !== "sample"; });
}

function syncScopeForms() {
  // Scope forms are rendered inside the workspace they control.  Their action
  // therefore comes from the server route that rendered that workspace; do not
  // rewrite it from window.location during an HTMX push-state race.
  document.querySelectorAll("form.scope-form").forEach(syncScopeFormVisibility);
}

function setHiddenField(form, name, value) {
  let input = form.querySelector(`input[type="hidden"][name="${name}"]`);
  if (!input) {
    input = document.createElement("input");
    input.type = "hidden";
    input.name = name;
    form.appendChild(input);
  }
  input.value = value;
}

function syncScopeIntoForm(form) {
  if (!form?.matches?.("form.smart-filter")) return;
  const state = currentScopeState();
  if (state.mode === "sample") {
    setHiddenField(form, "scope", "sample");
    setHiddenField(form, "sample_type", state.type);
    setHiddenField(form, "sample_n", state.n);
  } else {
    ["scope", "sample_type", "sample_n"].forEach(name => {
      form.querySelectorAll(`input[type="hidden"][name="${name}"]`).forEach(x => x.remove());
    });
  }
}

function submitSmartFilter(form) {
  syncScopeIntoForm(form);
  if (window.htmx) window.htmx.trigger(form, "submit");
  else form.requestSubmit();
}

function htmxNavigate(target, replace=false) {
  if (!window.htmx) {
    window.location.href = target;
    return;
  }
  if (replace) window.history.replaceState({}, "", target);
  else window.history.pushState({}, "", target);
  window.htmx.ajax("GET", target, {target:"#page-content", swap:"innerHTML"});
}

function refreshCurrentView() {
  const url = new URL(window.location.href);
  if (!url.searchParams.has("hours")) url.searchParams.set("hours", currentHours());
  const target = url.pathname + url.search;
  if (window.htmx) window.htmx.ajax("GET", target, {target:"#page-content", swap:"innerHTML"});
  else window.location.reload();
}

function changeActivityWindow(hours) {
  localStorage.setItem("nzbview.activityHours", hours);
  syncWindowLinks(hours); // update immediately, before the HTMX response arrives
  const url = new URL(window.location.href);
  url.searchParams.set("hours", hours);
  url.searchParams.delete("page");
  htmxNavigate(url.pathname + url.search);
}

function changeScopeMode(mode, defaultType="downloads") {
  const url = new URL(window.location.href);
  const state = currentScopeState();
  url.searchParams.delete("page");
  if (mode === "sample") {
    // The rendered page is the source of truth. This avoids losing Recent sample
    // state when the browser URL is momentarily behind an HTMX swap.
    url.searchParams.set("scope", "sample");
    url.searchParams.set("sample_type", state.type || defaultType || "downloads");
    url.searchParams.set("sample_n", state.n || "100");
  } else {
    url.searchParams.delete("scope");
    url.searchParams.delete("sample_type");
    url.searchParams.delete("sample_n");
    if (!url.searchParams.has("hours")) url.searchParams.set("hours", currentHours());
  }
  htmxNavigate(url.pathname + url.search);
}

function changeSamplePopulation(population) {
  const url = new URL(window.location.href);
  const state = currentScopeState();
  url.searchParams.set("scope", "sample");
  url.searchParams.set("sample_type", population);
  url.searchParams.set("sample_n", state.n || "100");
  url.searchParams.delete("page");
  htmxNavigate(url.pathname + url.search);
}

function changeSampleSize(size) {
  const url = new URL(window.location.href);
  const state = currentScopeState();
  url.searchParams.set("scope", "sample");
  url.searchParams.set("sample_type", state.type || "downloads");
  url.searchParams.set("sample_n", size);
  url.searchParams.delete("page");
  htmxNavigate(url.pathname + url.search);
}

function initializePersistentWindow() {
  const params = new URLSearchParams(window.location.search);
  const stored = localStorage.getItem("nzbview.activityHours");
  if (params.has("hours")) {
    localStorage.setItem("nzbview.activityHours", params.get("hours"));
    return false;
  }
  if (stored && /^\d+$/.test(stored)) {
    const url = new URL(window.location.href);
    url.searchParams.set("hours", stored);
    htmxNavigate(url.pathname + url.search, true);
    return true;
  }
  localStorage.setItem("nzbview.activityHours", currentHours());
  return false;
}

function openDrawer() {
  const backdrop = document.getElementById("drawer-backdrop");
  const drawer = document.getElementById("release-drawer");
  if (!backdrop || !drawer) return;
  backdrop.hidden = false;
  backdrop.classList.remove("closing");
  drawer.classList.remove("closing");
  requestAnimationFrame(() => {
    backdrop.classList.add("open");
    drawer.classList.add("open");
  });
  drawer.setAttribute("aria-hidden", "false");
  document.body.style.overflow = "hidden";
}

function closeDrawer() {
  const backdrop = document.getElementById("drawer-backdrop");
  const drawer = document.getElementById("release-drawer");
  if (!backdrop || !drawer || drawer.getAttribute("aria-hidden") === "true") return;
  backdrop.classList.remove("open");
  backdrop.classList.add("closing");
  drawer.classList.remove("open");
  drawer.classList.add("closing");
  drawer.setAttribute("aria-hidden", "true");
  document.body.style.overflow = "";
  const finish = () => {
    backdrop.hidden = true;
    backdrop.classList.remove("closing");
    drawer.classList.remove("closing");
  };
  if (reducedMotion()) finish();
  else setTimeout(finish, 240);
}

async function loadReleaseDrawer(button) {
  const title = button.dataset.releaseTitle;
  const hours = button.dataset.hours || currentHours();
  const anchorTime = button.dataset.releaseTime || "";
  if (!title) return;
  const body = document.getElementById("drawer-body");
  body.innerHTML = '<div class="empty-state">Loading release journey…</div>';
  openDrawer();
  const url = `/release-detail?hours=${encodeURIComponent(hours)}&title=${encodeURIComponent(title)}${anchorTime ? `&anchor_time=${encodeURIComponent(anchorTime)}` : ""}`;
  try {
    const response = await fetch(url, {headers: {"HX-Request": "true"}});
    body.innerHTML = await response.text();
    animateDrawerContent();
  } catch (err) {
    body.innerHTML = `<div class="empty-state">Could not load release details: ${chartEscape(String(err))}</div>`;
  }
}

function toast(message, kind="info", timeout=3500) {
  const el = document.getElementById("toast");
  if (!el) return;
  clearTimeout(el._timer);
  clearTimeout(motionToastHideTimer);
  el.textContent = message;
  el.className = `toast ${kind}`;
  el.hidden = false;
  requestAnimationFrame(() => el.classList.add("show"));
  el._timer = setTimeout(() => {
    el.classList.remove("show");
    el.classList.add("hiding");
    const finish = () => {
      el.hidden = true;
      el.classList.remove("hiding");
    };
    if (reducedMotion()) finish();
    else motionToastHideTimer = setTimeout(finish, 230);
  }, timeout);
}

async function runCollector() {
  const button = document.getElementById("run-collector");
  if (!button || button.disabled) return;
  button.disabled = true;
  button.textContent = "Starting…";
  document.body.classList.add("collector-running");
  try {
    const response = await fetch("/api/collector/run", {method:"POST", headers:{"X-Nzbview-Request":"1"}});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || data.message || "Collector could not start.");
    collectorWasRunning = true;
    button.textContent = "Collector running · 0s";
    toast("Collector started.", "info");
    scheduleCollectorPoll(500);
  } catch (err) {
    button.disabled = false;
    button.textContent = "Run Collector";
    document.body.classList.remove("collector-running");
    toast(String(err), "error", 6000);
  }
}

function scheduleCollectorPoll(delay=1000) {
  clearTimeout(collectorPollTimer);
  collectorPollTimer = setTimeout(pollCollector, delay);
}

async function pollCollector() {
  const button = document.getElementById("run-collector");
  try {
    const response = await fetch("/api/collector/status");
    const data = await response.json();
    if (!button) return;

    if (!data.configured) {
      button.disabled = true;
      button.textContent = "Run Collector";
      button.title = data.config_message || "Collector is not configured.";
      return;
    }

    if (data.running) {
      collectorWasRunning = true;
      document.body.classList.add("collector-running");
      button.disabled = true;
      const seconds = Math.round(Number(data.duration_seconds || 0));
      button.textContent = `Collector running · ${seconds}s`;
      scheduleCollectorPoll();
      return;
    }

    document.body.classList.remove("collector-running");
    button.disabled = false;
    button.textContent = "Run Collector";

    if (collectorWasRunning) {
      collectorWasRunning = false;
      if (data.status === "success" || data.status === "partial") {
        const seconds = Number(data.duration_seconds || 0).toFixed(1);
        const attempts = Number(data.delta_attempts || 0);
        const grabs = Number(data.delta_grab_events || 0);
        const arr = Number(data.delta_arr_events || 0);
        const arrPart = arr ? ` · +${arr} Arr events` : "";
        toast(data.status === "partial" ? data.message : `Collector finished in ${seconds}s · +${attempts} attempts · +${grabs} grab events${arrPart}`, data.status === "partial" ? "info" : "success", 5500);
        sessionStorage.setItem("nzbview.motionCollectorRefresh", "1");
        setTimeout(refreshCurrentView, 250);
      } else if (data.status === "error") {
        toast(data.message || "Collector failed.", "error", 7000);
      }
    }
  } catch (_) {
    document.body.classList.remove("collector-running");
    if (button) {
      button.disabled = false;
      button.textContent = "Run Collector";
    }
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const restoringWindow = initializePersistentWindow();
  if (!restoringWindow) updateShell();
  pollCollector();
});

document.addEventListener("htmx:beforeRequest", e => {
  const target = motionSwapTarget(e.detail);
  if (target?.id === "page-content") setRouteProgress(true);
  else scheduleMotionSkeleton(target);
});

document.addEventListener("htmx:beforeSwap", e => {
  const target = motionSwapTarget(e.detail);
  clearMotionSkeleton(target);
});

document.addEventListener("htmx:afterSwap", e => {
  const target = motionSwapTarget(e.detail);
  clearMotionSkeleton(target);
  setRouteProgress(false);
  if (target && target.id !== "page-content" && target.id !== "collector-status" && !reducedMotion()) {
    target.classList.remove("motion-swap-in");
    void target.offsetWidth;
    target.classList.add("motion-swap-in");
    setTimeout(() => target.classList.remove("motion-swap-in"), 320);
  }
  updateShell();
});

document.addEventListener("htmx:afterRequest", e => {
  clearMotionSkeleton(motionSwapTarget(e.detail));
  setRouteProgress(false);
});
document.addEventListener("htmx:responseError", e => {
  clearMotionSkeleton(motionSwapTarget(e.detail));
  setRouteProgress(false);
  toast("Could not refresh that view.", "error", 4500);
});
document.addEventListener("htmx:historyRestore", () => { updateShell(); setRouteProgress(false); });
window.addEventListener("resize", () => requestAnimationFrame(positionNavIndicator));

document.addEventListener("change", e => {
  const scopeForm = e.target.closest("form.scope-form");
  if (scopeForm) {
    // Scope-capable pages own their Activity Window control too. Persist it
    // immediately so sidebar/page navigation uses the newly selected window even
    // though the HTMX request only swaps this page's analytics workspace.
    if (e.target.matches('[name="hours"]')) {
      localStorage.setItem("nzbview.activityHours", e.target.value);
      syncWindowLinks(e.target.value);
    }
    syncScopeFormVisibility(scopeForm);
    return; // the form's own hx-trigger=change submits the request
  }

  const trendSelect = e.target.closest("[data-trend-metric]");
  if (trendSelect) {
    changeTrendMetric(trendSelect.value);
    return;
  }
  const windowSelect = e.target.closest("[data-activity-window]");
  if (windowSelect) {
    changeActivityWindow(windowSelect.value);
    return;
  }
  const el = e.target.closest("[data-auto-submit]");
  if (!el) return;
  if (el.matches('input[type="checkbox"]') && el.closest('details.multi-filter')) return;
  const form = el.closest("form");
  if (form) submitSmartFilter(form);
});

document.addEventListener("submit", e => {
  const form = e.target.closest?.("form.smart-filter");
  if (form) syncScopeIntoForm(form);
}, true);

document.addEventListener("htmx:configRequest", e => {
  const elt = e.detail?.elt;
  const form = elt?.matches?.("form.smart-filter") ? elt : elt?.closest?.("form.smart-filter");
  if (!form) return;
  const state = currentScopeState();
  if (state.mode !== "sample") return;
  // Last line of defence: force the active count-based scope into every HTMX
  // filter request even if the browser URL or a preserved form is stale.
  const params = e.detail.parameters;
  if (typeof params?.set === "function") {
    params.set("scope", "sample");
    params.set("sample_type", state.type);
    params.set("sample_n", state.n);
  } else if (params) {
    params.scope = "sample";
    params.sample_type = state.type;
    params.sample_n = state.n;
  }
});

document.addEventListener("input", e => {
  const el = e.target.closest("[data-search]");
  if (!el) return;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    searchRestore = {pos: el.selectionStart ?? el.value.length};
    const form = el.closest("form");
    if (form) submitSmartFilter(form);
  }, 500);
});

document.addEventListener("click", e => {
  // Checkbox/menu clicks must never trigger navigation or collapse their <details>.
  // Filters are applied only with the explicit Apply button.
  if (e.target.closest(".multi-filter-menu") && !e.target.closest("[data-apply-filter]")) {
    e.stopPropagation();
    return;
  }
  const windowNav = e.target.closest("[data-window-nav]");
  if (windowNav) {
    e.preventDefault();
    const target = urlForPath(windowNav.dataset.path || windowNav.getAttribute("href"), currentHours());
    htmxNavigate(target);
    return;
  }



  const expandDownloads = e.target.closest("[data-download-expand-all]");
  if (expandDownloads) {
    e.preventDefault();
    const workspace = expandDownloads.closest("#downloads-workspace") || document;
    workspace.querySelectorAll("[data-download-tree] details").forEach(d => { d.open = true; });
    return;
  }

  const collapseDownloads = e.target.closest("[data-download-collapse-all]");
  if (collapseDownloads) {
    e.preventDefault();
    const workspace = collapseDownloads.closest("#downloads-workspace") || document;
    workspace.querySelectorAll("[data-download-tree] details").forEach(d => { d.open = false; });
    return;
  }

  const applyFilter = e.target.closest("[data-apply-filter]");
  if (applyFilter) {
    e.preventDefault();
    const form = applyFilter.closest("form");
    if (!form) return;
    const details = applyFilter.closest("details.multi-filter");
    if (details) details.open = false;
    submitSmartFilter(form);
    return;
  }

  const pageJump = e.target.closest("[data-page-jump-button]");
  if (pageJump) {
    e.preventDefault();
    const wrap = pageJump.closest("[data-page-jump]");
    const input = wrap?.querySelector("input[data-page-number]");
    if (!input) return;
    let page = parseInt(input.value || "1", 10);
    const max = parseInt(input.max || "1", 10);
    if (!Number.isFinite(page)) page = 1;
    page = Math.max(1, Math.min(max, page));
    const url = new URL(window.location.href);
    url.searchParams.set("page", String(page));
    const target = wrap?.dataset.pageTarget || "#page-content";
    const select = wrap?.dataset.pageSelect || "";
    const dest = url.pathname + url.search;
    if (window.htmx && target !== "#page-content") {
      window.history.pushState({}, "", dest);
      const opts = {target, swap: select ? "outerHTML" : "innerHTML"};
      if (select) opts.select = select;
      window.htmx.ajax("GET", dest, opts);
    } else {
      htmxNavigate(dest);
    }
    return;
  }

  const multiSummary = e.target.closest("details.multi-filter > summary");
  if (multiSummary) {
    const current = multiSummary.parentElement;
    document.querySelectorAll("details.multi-filter[open]").forEach(d => { if (d !== current) d.open = false; });
  }

  const toggle = e.target.closest(".sidebar-toggle");
  if (toggle) document.querySelector(".sidebar")?.classList.toggle("open");

  if (e.target.closest("[data-refresh-current]")) {
    e.preventDefault();
    refreshCurrentView();
  }

  if (e.target.closest("[data-run-collector]")) {
    e.preventDefault();
    runCollector();
  }

  const release = e.target.closest(".release-link");
  if (release) {
    e.preventDefault();
    loadReleaseDrawer(release);
  }

  const reset = e.target.closest("[data-reset-filters]");
  if (reset) {
    const form = reset.closest("form");
    if (!form) return;
    form.querySelectorAll('input:not([type=hidden])').forEach(x => {
      if (x.type === 'checkbox' || x.type === 'radio') x.checked = false;
      else x.value = '';
    });
    form.querySelectorAll("select").forEach(x => x.value = "all");
    form.querySelectorAll("details.multi-filter").forEach(x => x.open = false);
    submitSmartFilter(form);
  }

  if (e.target.closest("#drawer-close") || e.target.id === "drawer-backdrop") closeDrawer();
});

document.addEventListener("keydown", e => {
  if (e.key === "Enter" && e.target.matches("input[data-page-number]")) {
    e.preventDefault();
    e.target.closest("[data-page-jump]")?.querySelector("[data-page-jump-button]")?.click();
    return;
  }
  if (e.key === "Escape") closeDrawer();
});
