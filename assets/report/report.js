(function () {
  const payload = window.REPORT_PAYLOAD || {};
  const meta = window.REPORT_META || {};
  const rounds = Array.isArray(payload.rounds) ? payload.rounds : [];
  const runIndex = Array.isArray(payload.run_index) ? payload.run_index : [];
  const chartExports = (payload.chart_exports && typeof payload.chart_exports === "object") ? payload.chart_exports : {};
  const parseWarnings = Array.isArray(payload.parse_warnings) ? payload.parse_warnings : [];
  const viewScope = payload.view_scope === "run" ? "run" : "index";
  const reportView = (typeof meta.report_view === "string" && meta.report_view)
    ? meta.report_view
    : ((typeof payload.report_view === "string" && payload.report_view) ? payload.report_view : "");
  const reportViewLinks = Array.isArray(meta.report_view_links)
    ? meta.report_view_links
    : (Array.isArray(payload.report_view_links) ? payload.report_view_links : []);
  const ARCHIVE_LABELS = {
    candidates: "Prune candidates",
    size_streak: "Size-streak pruned",
  };
  const OTU_ASSIGNMENT_MIN_READS = 5;
  const RUN_EVOLUTION_FIGURE_IDS = new Set([
    "reads_time",
    "reads_cumulative_log",
    "otu_tax_time",
    "otu_frozen_tax_time",
    "consensus_tax_time",
    "consensus_consolidated_tax_time",
  ]);
  function num(v) {
    return typeof v === "number" && Number.isFinite(v) ? v : 0;
  }

  function flatMapCompat(items, iteratee) {
    const list = Array.isArray(items) ? items : [];
    const out = [];
    list.forEach((item, idx) => {
      const mapped = typeof iteratee === "function" ? iteratee(item, idx) : [];
      if (Array.isArray(mapped)) {
        mapped.forEach((value) => out.push(value));
      } else if (mapped != null) {
        out.push(mapped);
      }
    });
    return out;
  }

  function numAny(v) {
    if (typeof v === "number" && Number.isFinite(v)) return v;
    if (typeof v === "string") {
      const trimmed = v.trim();
      if (trimmed !== "") {
        const parsed = Number(trimmed);
        return Number.isFinite(parsed) ? parsed : 0;
      }
    }
    return 0;
  }

  function isSupportedOtuAssignment(row) {
    return numAny(row && row.reads_total) >= OTU_ASSIGNMENT_MIN_READS;
  }

  function finiteNumberOrNull(v) {
    if (typeof v === "number" && Number.isFinite(v)) return v;
    if (typeof v === "string") {
      const trimmed = v.trim();
      if (trimmed !== "") {
        const parsed = Number(trimmed);
        if (Number.isFinite(parsed)) return parsed;
      }
    }
    return null;
  }

  function canonicalMarkerToken(value) {
    const raw = (value || "").toString().trim();
    if (!raw) return "";
    if (/-COI\b/i.test(raw) || /\bCOI\b/i.test(raw)) return "COI";
    if (/-ITS(?:1|2)?\b/i.test(raw) || /\bITS(?:1|2)?\b/i.test(raw)) return "ITS2";
    return raw.toUpperCase();
  }

  function markerSlug(marker) {
    return canonicalMarkerToken(marker).toLowerCase().replace(/[^a-z0-9._-]+/g, "_").replace(/_+/g, "_").replace(/^_+|_+$/g, "");
  }

  function markerOrderFromData(items) {
    const list = Array.isArray(items) ? items : (items ? [items] : []);
    const ordered = [];
    const seen = new Set();
    const add = (marker) => {
      const canonical = canonicalMarkerToken(marker);
      if (!canonical || canonical === "OTHER" || seen.has(canonical)) return;
      seen.add(canonical);
      ordered.push(canonical);
    };
    list.forEach((row) => {
      const markers = row && row.markers && typeof row.markers === "object" ? row.markers : null;
      if (markers && Array.isArray(markers.order)) markers.order.forEach(add);
    });
    list.forEach((row) => {
      const readFate = row && row.read_fate && typeof row.read_fate === "object" ? row.read_fate : {};
      const markerCounts = readFate && typeof readFate.marker_counts === "object" ? readFate.marker_counts : {};
      Object.values(markerCounts).forEach((bucket) => {
        if (bucket && typeof bucket === "object" && !Array.isArray(bucket)) Object.keys(bucket).forEach(add);
      });
      const sampleMetrics = row && row.sample_metrics && typeof row.sample_metrics === "object" ? row.sample_metrics : {};
      Object.values(sampleMetrics).forEach((sample) => {
        const demuxMap = sample && typeof sample.reads_demux_by_marker === "object" ? sample.reads_demux_by_marker : null;
        if (demuxMap) Object.keys(demuxMap).forEach(add);
      });
      ["otu", "consensus"].forEach((sourceKey) => {
        const subKey = sourceKey === "otu" ? "active_by_marker_taxon" : "emitted_by_marker_taxon";
        const source = row && row[sourceKey] && typeof row[sourceKey] === "object" ? row[sourceKey] : {};
        const bucket = source && typeof source[subKey] === "object" ? source[subKey] : {};
        ["assigned", "unassigned"].forEach((nestedKey) => {
          const nested = bucket && typeof bucket[nestedKey] === "object" ? bucket[nestedKey] : null;
          if (nested) Object.keys(nested).forEach(add);
        });
        const assignments = source && typeof source.assignments_by_level === "object" ? source.assignments_by_level : {};
        Object.values(assignments).forEach((rows) => {
          (Array.isArray(rows) ? rows : []).forEach((entry) => add(entry && entry.marker));
        });
      });
    });
    return ordered.length ? ordered : ["COI", "ITS2"];
  }

  function markerColorsFromData(items) {
    const colors = new Map([
      ["COI", "#2c6e49"],
      ["ITS2", "#1d4e89"],
    ]);
    const list = Array.isArray(items) ? items : (items ? [items] : []);
    list.forEach((row) => {
      const colorMap = row && row.markers && typeof row.markers.color_by_marker === "object" ? row.markers.color_by_marker : null;
      if (!colorMap) return;
      Object.entries(colorMap).forEach(([marker, color]) => {
        const canonical = canonicalMarkerToken(marker);
        if (canonical && typeof color === "string" && color.trim()) colors.set(canonical, color);
      });
    });
    const palette = ["#8c564b", "#bcbd22", "#17becf", "#e45756", "#54a24b", "#f58518", "#4c78a8", "#9c755f", "#bab0ac", "#ff9da6"];
    let idx = 0;
    markerOrderFromData(items).forEach((marker) => {
      if (!colors.has(marker)) {
        colors.set(marker, palette[idx % palette.length]);
        idx += 1;
      }
    });
    return colors;
  }

  function lightenHex(color, factor = 0.4) {
    const hex = (color || "").toString().replace(/^#/, "");
    if (!/^[0-9a-fA-F]{6}$/.test(hex)) return color || "#9aa39b";
    const mix = (start) => {
      const value = parseInt(hex.slice(start, start + 2), 16);
      return Math.round(value + (255 - value) * factor).toString(16).padStart(2, "0");
    };
    return `#${mix(0)}${mix(2)}${mix(4)}`;
  }

  function otuThresholdForLevel(round, level, marker, fallbackThresholds = null) {
    const configured = get(round, ["assignment_thresholds", "otu", level], null);
    const levelMap = (configured && typeof configured === "object")
      ? configured
      : (fallbackThresholds && typeof fallbackThresholds[level] === "object" ? fallbackThresholds[level] : null);
    if (!levelMap || Array.isArray(levelMap)) return null;
    const canonical = canonicalMarkerToken(marker);
    if (canonical) {
      const direct = finiteNumberOrNull(levelMap[canonical]);
      if (direct != null) return direct;
      const upper = finiteNumberOrNull(levelMap[canonical.toUpperCase()]);
      if (upper != null) return upper;
    }
    return finiteNumberOrNull(levelMap._default);
  }

  function otuRowMeetsConfiguredThreshold(round, level, row, fallbackThresholds = null) {
    const threshold = otuThresholdForLevel(round, level, row && row.marker, fallbackThresholds);
    if (threshold == null) return true;
    const perc = finiteNumberOrNull(row && row.perc_id_min);
    return perc != null && perc >= threshold;
  }

  function clamp(v, minV, maxV) {
    const x = Number.isFinite(v) ? v : 0;
    if (x < minV) return minV;
    if (x > maxV) return maxV;
    return x;
  }

  function maybeAddSegmentLabel(segEl, value, pctOfFull) {
    if (!segEl) return;
    if (!Number.isFinite(value) || value <= 0) return;
    if (!Number.isFinite(pctOfFull) || pctOfFull < 5) return;
    const label = document.createElement("span");
    label.className = "bar-segment-label";
    label.textContent = String(value);
    segEl.appendChild(label);
  }

  function get(obj, path, fallback) {
    let cur = obj;
    for (const key of path) {
      if (!cur || typeof cur !== "object" || !(key in cur)) return fallback;
      cur = cur[key];
    }
    return cur == null ? fallback : cur;
  }

  function makeAssetLink(label, href) {
    if (typeof href !== "string" || href.trim() === "") return null;
    const a = document.createElement("a");
    a.className = "chart-download";
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = label;
    return a;
  }

  function makeChartDownloads(title, tsv, chartId, pageIndex, includeTsv) {
    const downloads = document.createElement("div");
    downloads.className = "download-links";
    if (includeTsv !== false && typeof tsv === "string" && tsv) {
      downloads.appendChild(makeDownloadLink(title, tsv));
    }
    const pdfLink = makeAssetLink("Download PDF", getChartPdfPath(chartId || "", pageIndex));
    if (pdfLink) downloads.appendChild(pdfLink);
    return downloads;
  }

  function getChartPdfPath(chartId, pageIndex) {
    if (!chartId) return "";
    const entry = chartExports[chartId];
    if (!entry || typeof entry !== "object") return "";
    if (Array.isArray(entry.pages)) {
      const page = Number.isInteger(pageIndex) ? pageIndex : 0;
      const hit = entry.pages.find((item) => item && item.page === page && typeof item.pdf_path === "string");
      return hit ? hit.pdf_path : "";
    }
    return typeof entry.pdf_path === "string" ? entry.pdf_path : "";
  }

  const cards = document.getElementById("summary-cards");
  const summarySection = document.getElementById("summary-section");
  const warningsUl = document.getElementById("warnings");
  const warningsSection = document.getElementById("warnings-section");
  const runTableHead = document.querySelector("#runs-table thead");
  const runTableBody = document.querySelector("#runs-table tbody");
  const charts = document.getElementById("charts");
  const figures = document.getElementById("figures");
  const figuresSection = document.getElementById("figures-section");
  const runsInfoSection = document.getElementById("runs-info-section");
  const runIndexPager = document.getElementById("run-index-pager");
  const sampleIndex = document.getElementById("sample-index");
  const sampleIndexSection = document.getElementById("sample-index-section");
  const sampleDetails = document.getElementById("sample-details");
  const sampleDetailsSection = document.getElementById("sample-details-section");
  const roundsSection = document.getElementById("rounds-section");
  const indexLink = document.getElementById("report-index-link");
  const runNameEl = document.getElementById("run-name");
  const runLinksEl = document.getElementById("run-links");
  const modal = document.getElementById("figure-modal");
  const modalImg = document.getElementById("figure-modal-img");
  const modalCaption = document.getElementById("figure-modal-caption");
  const modalTitle = document.getElementById("figure-modal-title");
  const modalClose = modal ? modal.querySelector(".figure-modal-close") : null;
  let lastFocus = null;
  let warningsInitialized = false;

  function getRunId() {
    if (viewScope !== "run") return "";
    const first = rounds.length ? rounds[0] : null;
    return first && first.run_id ? String(first.run_id) : "";
  }

  function getFailureInfo(source) {
    if (!source || typeof source !== "object") return null;
    const status = source.round_status || source.last_round_status || "";
    if (status !== "failed") return null;
    return {
      reason: source.failure_reason || source.last_round_failure_reason || "Round failed",
      roundBarcode: source.round_barcode || source.last_round_barcode || "",
    };
  }

  function appendFailureBadge(container, failureInfo, text) {
    if (!container || !failureInfo) return;
    const span = document.createElement("span");
    span.className = "run-status run-status-red";
    span.textContent = text || "Latest round failed";
    const titleParts = [];
    if (failureInfo.roundBarcode) titleParts.push(failureInfo.roundBarcode);
    if (failureInfo.reason) titleParts.push(failureInfo.reason);
    if (titleParts.length) span.title = titleParts.join(": ");
    container.appendChild(span);
  }

  function renderRunHeader() {
    if (!runNameEl || !runLinksEl) return;
    if (viewScope !== "run") {
      runNameEl.textContent = "";
      runLinksEl.textContent = "";
      return;
    }
    const runId = getRunId();
    if (!runId) {
      runNameEl.textContent = "";
      runLinksEl.textContent = "";
      return;
    }
    const m = window.REPORT_META || {};
    const activeView = reportViewLinks.find((item) => item && item.is_active)
      || reportViewLinks.find((item) => item && item.view_id === reportView)
      || null;
    const activeViewLabel = (typeof m.report_view_label === "string" && m.report_view_label)
      ? m.report_view_label
      : (activeView && activeView.label ? String(activeView.label) : "");
    runNameEl.textContent = activeViewLabel ? `Run: ${runId} · ${activeViewLabel}` : `Run: ${runId}`;
    const latestRound = rounds.length ? rounds[rounds.length - 1] : null;
    const failureInfo = getFailureInfo(latestRound);
    if (failureInfo) {
      runNameEl.appendChild(document.createTextNode(" "));
      appendFailureBadge(runNameEl, failureInfo, "Latest round failed");
    }

    const rootPrefix = viewScope === "run" ? "../../" : "../";
    const links = [
      { label: "Pod5 Directory",  href: m.pod5_dir_url  || `${rootPrefix}pod5/${runId}/README.html` },
      { label: "Results Directory", href: m.state_dir_url || `${rootPrefix}current/state/${runId}/README.html` },
      { label: "Figures",         href: m.figures_dir_url || "./figures/README.html" },
      { label: "Samples Info",    href: m.sample_info_url || `${rootPrefix}sample_info/${runId}/README.html` },
      { label: "Run Config",      href: m.run_config_url  || `${rootPrefix}${runId}_config/README.html` },
    ];
    runLinksEl.innerHTML = "";
    if (reportViewLinks.length) {
      const viewNav = document.createElement("nav");
      viewNav.className = "run-view-links-list";
      reportViewLinks.forEach((item) => {
        if (!item || !item.label) return;
        if (item.is_active) {
          const active = document.createElement("span");
          active.className = "run-view-link active";
          active.textContent = item.label;
          viewNav.appendChild(active);
          return;
        }
        const a = document.createElement("a");
        a.className = "run-view-link";
        a.href = item.href || "report.html";
        a.textContent = item.label;
        viewNav.appendChild(a);
      });
      runLinksEl.appendChild(viewNav);
    }
    const nav = document.createElement("nav");
    nav.className = "run-links-list";
    links.forEach((item) => {
      const a = document.createElement("a");
      a.href = item.href;
      a.textContent = item.label;
      if (item.download) a.setAttribute("download", "");
      nav.appendChild(a);
    });
    runLinksEl.appendChild(nav);
  }

  function appendWarning(msg) {
    if (!warningsUl) return;
    if (!warningsInitialized) {
      while (warningsUl.firstChild) {
        warningsUl.removeChild(warningsUl.firstChild);
      }
      warningsInitialized = true;
    }
    const first = warningsUl.firstChild;
    if (first && first.textContent === "none") {
      warningsUl.removeChild(first);
    }
    const li = document.createElement("li");
    li.textContent = msg;
    warningsUl.appendChild(li);
  }

  const totals = rounds.reduce((acc, r) => {
    acc.rounds += 1;
    acc.reads += num(get(r, ["reads", "total"], 0));
    acc.onTarget += num(get(r, ["reads", "on_target"], 0));
    return acc;
  }, { rounds: 0, reads: 0, onTarget: 0 });
  const lastRound = rounds.length ? rounds[rounds.length - 1] : null;
  const reportIdentityMode = (lastRound && lastRound.identity_mode === "track") ? "track" : "collapse";
  const groupViewMode = (viewScope === "run" && reportIdentityMode === "track" && reportView === "track_detail")
    ? "track_detail"
    : ((viewScope === "run" && reportIdentityMode === "track" && reportView === "replicate")
      ? "replicate"
      : "sample");
  const consensusLatest = num(get(lastRound, ["consensus", "emitted"], 0));

  renderRunHeader();

  if (viewScope === "run") {
    const cardDefs = [
      ["Rounds", totals.rounds],
      ["Reads", totals.reads],
      ["On-target Reads", totals.onTarget],
      ["Consensus Emitted (Latest Round)", consensusLatest],
    ];

    cardDefs.forEach(([label, value]) => {
      const div = document.createElement("div");
      div.className = "card";
      const labelNode = document.createElement("div");
      labelNode.className = "label";
      labelNode.textContent = label;
      const valueNode = document.createElement("div");
      valueNode.className = "value";
      valueNode.textContent = String(value);
      div.appendChild(labelNode);
      div.appendChild(valueNode);
      cards.appendChild(div);
    });
  }

  function clearNode(node) {
    if (!node) return;
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function formatTimestamp(ts) {
    if (typeof ts !== "string" || !ts) return "";
    const parsed = Date.parse(ts);
    if (!Number.isFinite(parsed)) return ts;
    const d = new Date(parsed);
    return d.toLocaleString(undefined, {
      timeZone: "UTC",
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }) + " UTC";
  }

  function renderRunTable(runsPage) {
    if (!runTableHead || !runTableBody) return;
    clearNode(runTableHead);
    clearNode(runTableBody);
    const runCols = [
      { key: "run_id", label: "Run ID" },
      { key: "last_round_barcode", label: "Last Round" },
      { key: "last_updated_utc", label: "Last Updated (UTC)", format: formatTimestamp },
      { key: "rounds_count", label: "Rounds" },
      { key: "_status", label: "Status" },
      { key: "_report", label: "Report" },
    ];
    const runHeader = document.createElement("tr");
    runCols.forEach((c) => {
      const th = document.createElement("th");
      th.textContent = c.label;
      runHeader.appendChild(th);
    });
    runTableHead.appendChild(runHeader);

    if (!runsPage.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.textContent = "none";
      td.colSpan = runCols.length;
      tr.appendChild(td);
      runTableBody.appendChild(tr);
      return;
    }
    runsPage.forEach((r) => {
      const tr = document.createElement("tr");
      const values = [
        r.run_id || "",
        r.last_round_barcode || "",
        r.last_updated_utc || "",
        r.rounds_count != null ? r.rounds_count : "",
      ];
      values.forEach((v, idx) => {
        const td = document.createElement("td");
        const col = runCols[idx];
        const formatted = col && col.format ? col.format(String(v)) : String(v);
        td.textContent = formatted;
        tr.appendChild(td);
      });
      const statusCell = document.createElement("td");
      const statusInfo = computeRunStatus(r);
      const statusLabel = statusInfo.label;
      if (statusLabel) {
        const span = document.createElement("span");
        span.className = "run-status";
        span.textContent = statusLabel;
        if (statusInfo.color) {
          span.classList.add(`run-status-${statusInfo.color}`);
        }
        if (statusInfo.ageSeconds != null && statusInfo.cadenceSeconds != null) {
          span.title = `age=${statusInfo.ageSeconds}s cadence=${statusInfo.cadenceSeconds}s`;
        }
        statusCell.appendChild(span);
      } else {
        statusCell.textContent = "";
      }
      const failureInfo = getFailureInfo(r);
      if (failureInfo) {
        if (statusCell.childNodes.length) {
          statusCell.appendChild(document.createTextNode(" "));
        }
        appendFailureBadge(statusCell, failureInfo, "Latest round failed");
      }
      tr.appendChild(statusCell);
      const linkCell = document.createElement("td");
      const link = r.report_rel_path || r.report_url || "";
      if (link) {
        const a = document.createElement("a");
        a.href = link;
        a.textContent = "report";
        linkCell.appendChild(a);
      } else {
        linkCell.textContent = "-";
      }
      tr.appendChild(linkCell);
      runTableBody.appendChild(tr);
    });
  }

  function parseUtcSeconds(ts) {
    if (typeof ts !== "string" || !ts) return null;
    const parsed = Date.parse(ts);
    return Number.isFinite(parsed) ? Math.floor(parsed / 1000) : null;
  }

  function computeRunStatus(r) {
    const labelFallback = r.status_label || r.status || "";
    const colorFallback = r.status_color || "";
    const lastTs = parseUtcSeconds(r.last_round_timestamp_utc);
    const cadence = (r.status_cadence_seconds != null) ? Number(r.status_cadence_seconds) : null;
    if (!Number.isFinite(lastTs) || !Number.isFinite(cadence) || cadence <= 0) {
      return {
        label: labelFallback,
        color: colorFallback,
        ageSeconds: r.status_age_seconds != null ? Number(r.status_age_seconds) : null,
        cadenceSeconds: cadence,
      };
    }
    const nowSec = Math.floor(Date.now() / 1000);
    const age = Math.max(0, nowSec - lastTs);
    const ratio = age / cadence;
    if (ratio <= 1.5) {
      return { label: "Fresh", color: "green", ageSeconds: age, cadenceSeconds: cadence };
    }
    if (ratio <= 3.0) {
      return { label: "Aging", color: "orange", ageSeconds: age, cadenceSeconds: cadence };
    }
    return { label: "Stale", color: "red", ageSeconds: age, cadenceSeconds: cadence };
  }

  if (indexLink) {
    if (viewScope === "run") {
      indexLink.href = "../../report.html";
      indexLink.style.display = "inline-block";
    } else {
      indexLink.style.display = "none";
    }
  }

  function setHidden(el, hidden) {
    if (!el) return;
    el.style.display = hidden ? "none" : "";
  }

  function ensureBarTooltip() {
    let tip = document.getElementById("bar-segment-tooltip");
    if (tip) return tip;
    tip = document.createElement("div");
    tip.id = "bar-segment-tooltip";
    tip.className = "bar-tooltip";
    tip.style.display = "none";
    document.body.appendChild(tip);
    return tip;
  }

  function showBarTooltip(text, evt) {
    const tip = ensureBarTooltip();
    tip.textContent = text;
    tip.style.display = "block";
    positionBarTooltip(evt);
  }

  function positionBarTooltip(evt) {
    const tip = document.getElementById("bar-segment-tooltip");
    if (!tip || tip.style.display === "none") return;
    const offset = 12;
    const x = evt.clientX + offset;
    const y = evt.clientY + offset;
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  }

  function hideBarTooltip() {
    const tip = document.getElementById("bar-segment-tooltip");
    if (tip) tip.style.display = "none";
  }

  function makeTsv(columns, rows) {
    const sanitize = (v) => {
      if (v == null) return "";
      return String(v).replace(/\t/g, " ").replace(/\r?\n/g, " ");
    };
    const lines = [columns.map(sanitize).join("\t")];
    rows.forEach((row) => {
      lines.push(row.map(sanitize).join("\t"));
    });
    return `${lines.join("\n")}\n`;
  }

  function makeNamedDownloadLink(title, body, ext, mimeType, label) {
    const safe = (title || "chart").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
    const filename = `${safe || "chart"}.${ext || "tsv"}`;
    const blob = new Blob([body], { type: mimeType || "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.className = "chart-download";
    a.href = url;
    a.download = filename;
    a.textContent = label || "Download";
    a.addEventListener("click", () => {
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    });
    return a;
  }

  function makeDownloadLink(title, tsv) {
    return makeNamedDownloadLink(title, tsv, "tsv", "text/tab-separated-values", "Download TSV");
  }

  function drawStackedBars(title, segments, items, opts) {
    const options = opts && typeof opts === "object" ? opts : {};
    const data = Array.isArray(items) ? items : rounds;
    const roundValues = data.map((r) => segments.map((seg) => {
      let v = null;
      if (typeof seg.compute === "function") {
        v = seg.compute(r, get);
      } else if (seg.path) {
        v = get(r, seg.path, null);
      }
      return (typeof v === "number" && Number.isFinite(v) && v >= 0) ? v : null;
    }));
    const totalsByRound = roundValues.map((vals) => vals.reduce((acc, v) => acc + (v == null ? 0 : v), 0));
    const presentTotals = totalsByRound.filter((v) => Number.isFinite(v));
    const maxTotal = Math.max(1, ...(presentTotals.length ? presentTotals : [0]));

    const card = document.createElement("div");
    card.className = "chart-card";
    const header = document.createElement("div");
    header.className = "chart-header";
    const h = document.createElement("h3");
    h.textContent = title;
    header.appendChild(h);
    const rowLabel = options.rowLabel || "round";
    const columns = [rowLabel, ...segments.map((s) => s.label), "total"];
    const tsvRows = data.map((r, idx) => {
      const vals = roundValues[idx];
      const allMissing = vals.every((v) => v == null);
      const hasMissing = vals.some((v) => v == null);
      const total = vals.reduce((acc, v) => acc + (v == null ? 0 : v), 0);
      return [
        r.round_barcode || "-",
        ...vals.map((v) => (v == null ? "N/A" : v)),
        allMissing ? "N/A" : (hasMissing ? `${total} (partial)` : total),
      ];
    });
    const downloads = makeChartDownloads(title, makeTsv(columns, tsvRows), options.chartId || "", options.pageIndex);
    header.appendChild(downloads);
    card.appendChild(header);

    const legend = document.createElement("div");
    legend.className = "chart-legend";
    segments.forEach((seg) => {
      const item = document.createElement("span");
      item.className = "chart-legend-item";
      const swatch = document.createElement("span");
      swatch.className = "chart-legend-swatch";
      swatch.style.background = seg.color;
      const label = document.createElement("span");
      label.textContent = seg.label;
      item.appendChild(swatch);
      item.appendChild(label);
      legend.appendChild(item);
    });
    card.appendChild(legend);

    data.forEach((r, idx) => {
      const vals = roundValues[idx];
      const total = totalsByRound[idx];
      const allMissing = vals.every((v) => v == null);
      const hasMissing = vals.some((v) => v == null);

      const rowDiv = document.createElement("div");
      rowDiv.className = "bar-row";
      const label = document.createElement("span");
      label.textContent = r.round_barcode || "-";
      const bar = document.createElement("span");
      bar.className = "bar";

      if (!allMissing && total > 0) {
        const stack = document.createElement("span");
        stack.className = "bar-stack";
        stack.style.width = `${Math.round((total / maxTotal) * 100)}%`;
        vals.forEach((v, sIdx) => {
          if (v == null || v <= 0) return;
          const seg = document.createElement("span");
          seg.className = "bar-segment";
          const pct = (v / total) * 100;
          const pctOfFull = (v / maxTotal) * 100;
          seg.style.flex = `0 0 ${pct}%`;
          seg.style.background = segments[sIdx].color;
          maybeAddSegmentLabel(seg, v, pctOfFull);
          const tooltipText = `${segments[sIdx].label}: ${v}`;
          seg.addEventListener("mouseenter", (e) => showBarTooltip(tooltipText, e));
          seg.addEventListener("mousemove", positionBarTooltip);
          seg.addEventListener("mouseleave", hideBarTooltip);
          const prev = segments[sIdx - 1];
          if (prev && prev.group && segments[sIdx].group && prev.group !== segments[sIdx].group) {
            seg.classList.add("bar-segment-transition");
          }
          stack.appendChild(seg);
        });
        bar.appendChild(stack);
      }

      const value = document.createElement("span");
      if (allMissing) {
        value.textContent = "N/A";
      } else if (hasMissing) {
        value.textContent = `${total} (partial)`;
      } else {
        value.textContent = String(total);
      }
      rowDiv.appendChild(label);
      rowDiv.appendChild(bar);
      rowDiv.appendChild(value);
      card.appendChild(rowDiv);
    });

    charts.appendChild(card);
  }

  function buildRunLike(r) {
    const summary = (r && r.run_summary && typeof r.run_summary === "object") ? r.run_summary : null;
    if (!summary) {
      return { round_barcode: r && r.run_id ? r.run_id : "-", _summary_missing: true };
    }
    return {
      round_barcode: r.run_id || "-",
      reads: summary.reads || {},
      read_fate: summary.read_fate || {},
      otu: summary.otu || {},
      consensus: summary.consensus || {},
      _summary_missing: false,
    };
  }

  function buildRunStatusReadFateLike(r) {
    const readFate = (r && r.run_status_read_fate && typeof r.run_status_read_fate === "object") ? r.run_status_read_fate : null;
    if (!readFate) {
      return { round_barcode: r && r.run_id ? r.run_id : "-", _summary_missing: true };
    }
    return {
      round_barcode: r.run_id || "-",
      read_fate: readFate,
      _summary_missing: false,
    };
  }

  function drawActiveOtuByMarker(items, opts) {
    const options = opts && typeof opts === "object" ? opts : {};
    const data = Array.isArray(items) ? items : rounds;
    const markers = markerOrderFromData(data);
    const markerColors = markerColorsFromData(data);
    const segments = [];
    markers.forEach((marker) => {
      const lower = marker.toLowerCase();
      const color = markerColors.get(marker) || "#9aa39b";
      segments.push({
        label: `${marker} assigned`,
        color,
        group: "assigned",
        compute: (r) => get(r, ["otu", "active_by_marker_taxon", "assigned", marker], get(r, ["otu", "active_by_marker_taxon", `${lower}_assigned`], null)),
      });
      segments.push({
        label: `${marker} unassigned`,
        color: lightenHex(color, 0.5),
        group: "unassigned",
        compute: (r) => get(r, ["otu", "active_by_marker_taxon", "unassigned", marker], get(r, ["otu", "active_by_marker_taxon", `${lower}_unassigned`], null)),
      });
    });
    const anyOther = data.some((r) => {
      const a = get(r, ["otu", "active_by_marker_taxon", "assigned", "OTHER"], get(r, ["otu", "active_by_marker_taxon", "other_assigned"], null));
      const u = get(r, ["otu", "active_by_marker_taxon", "unassigned", "OTHER"], get(r, ["otu", "active_by_marker_taxon", "other_unassigned"], null));
      return (typeof a === "number" && a > 0) || (typeof u === "number" && u > 0);
    });
    if (anyOther) {
      segments.push(
        { label: "Other assigned", color: "#6b5b95", group: "assigned", compute: (r) => get(r, ["otu", "active_by_marker_taxon", "assigned", "OTHER"], get(r, ["otu", "active_by_marker_taxon", "other_assigned"], null)) },
        { label: "Other unassigned", color: "#b39cd0", group: "unassigned", compute: (r) => get(r, ["otu", "active_by_marker_taxon", "unassigned", "OTHER"], get(r, ["otu", "active_by_marker_taxon", "other_unassigned"], null)) },
      );
    }
    drawStackedBars("Informative OTUs (assigned vs unassigned)", segments, data, options);
  }

  function drawConsensusEmittedByMarker(items, opts) {
    const options = opts && typeof opts === "object" ? opts : {};
    const data = Array.isArray(items) ? items : rounds;
    const markers = markerOrderFromData(data);
    const markerColors = markerColorsFromData(data);
    const segments = [];
    markers.forEach((marker) => {
      const lower = marker.toLowerCase();
      const color = markerColors.get(marker) || "#9aa39b";
      segments.push({
        label: `${marker} assigned`,
        color,
        group: "assigned",
        compute: (r) => get(r, ["consensus", "emitted_by_marker_taxon", "assigned", marker], get(r, ["consensus", "emitted_by_marker_taxon", `${lower}_assigned`], null)),
      });
      segments.push({
        label: `${marker} unassigned`,
        color: lightenHex(color, 0.5),
        group: "unassigned",
        compute: (r) => get(r, ["consensus", "emitted_by_marker_taxon", "unassigned", marker], get(r, ["consensus", "emitted_by_marker_taxon", `${lower}_unassigned`], null)),
      });
    });
    const anyOther = data.some((r) => {
      const a = get(r, ["consensus", "emitted_by_marker_taxon", "assigned", "OTHER"], get(r, ["consensus", "emitted_by_marker_taxon", "other_assigned"], null));
      const u = get(r, ["consensus", "emitted_by_marker_taxon", "unassigned", "OTHER"], get(r, ["consensus", "emitted_by_marker_taxon", "other_unassigned"], null));
      return (typeof a === "number" && a > 0) || (typeof u === "number" && u > 0);
    });
    if (anyOther) {
      segments.push(
        { label: "Other assigned", color: "#6b5b95", group: "assigned", compute: (r) => get(r, ["consensus", "emitted_by_marker_taxon", "assigned", "OTHER"], get(r, ["consensus", "emitted_by_marker_taxon", "other_assigned"], null)) },
        { label: "Other unassigned", color: "#b39cd0", group: "unassigned", compute: (r) => get(r, ["consensus", "emitted_by_marker_taxon", "unassigned", "OTHER"], get(r, ["consensus", "emitted_by_marker_taxon", "other_unassigned"], null)) },
      );
    }
    drawStackedBars("Consensus Emitted (assigned vs unassigned)", segments, data, options);
  }

  // Demultiplexing section: one stacked bar per sample showing COI vs ITS2 reads.
  // Inserted before mountBefore (inside sample-details-section); falls back to #charts.
  function drawDemuxByMarkerPerSample(ordered, mountBefore) {
    if (!ordered || !ordered.length) return;
    if (!detectDemuxEnabled()) return;
    const hasAny   = ordered.some((s) => s.totals.reads_demux > 0);
    if (!hasAny) return;
    const markers = markerOrderFromData(rounds);
    const markerColors = markerColorsFromData(rounds);
    const hasMarker = new Map(markers.map((marker) => [marker, ordered.some((s) => num(get(s, ["totals", "reads_demux_by_marker", marker], 0)) > 0)]));
    const hasUnspec = ordered.some((s) => {
      const mapped = markers.reduce((acc, marker) => acc + num(get(s, ["totals", "reads_demux_by_marker", marker], 0)), 0);
      return s.totals.reads_demux > mapped;
    });

    const maxTotal = Math.max(1, ...ordered.map((s) => s.totals.reads_demux));

    const card = document.createElement("div");
    card.className = "chart-card";
    const hdr = document.createElement("div");
    hdr.className = "chart-header";
    const h = document.createElement("h3");
    h.textContent = `Demultiplexing Reads per ${groupEntityLabel()}`;
    hdr.appendChild(h);
    const downloads = document.createElement("div");
    downloads.className = "download-links";
    const pdfLink = makeAssetLink("Download PDF", getChartPdfPath("run_demultiplex_reads_by_marker", 0));
    if (pdfLink) downloads.appendChild(pdfLink);
    if (downloads.childNodes.length) hdr.appendChild(downloads);
    card.appendChild(hdr);

    const legend = document.createElement("div");
    legend.className = "chart-legend";
    markers.forEach((marker) => {
      if (!hasMarker.get(marker)) return;
      const i = document.createElement("span");
      i.className = "chart-legend-item";
      const sw = document.createElement("span");
      sw.className = "chart-legend-swatch";
      sw.style.background = markerColors.get(marker) || "#9aa39b";
      const lt = document.createElement("span");
      lt.textContent = marker;
      i.appendChild(sw);
      i.appendChild(lt);
      legend.appendChild(i);
    });
    if (hasUnspec){ const i = document.createElement("span"); i.className = "chart-legend-item"; const sw = document.createElement("span"); sw.className = "chart-legend-swatch"; sw.style.background = "#9e9e9e"; const lt = document.createElement("span"); lt.textContent = "Unspecified"; i.appendChild(sw); i.appendChild(lt); legend.appendChild(i); }
    card.appendChild(legend);

    ordered.forEach((s) => {
      const total = s.totals.reads_demux;
      const markerValues = markers.map((marker) => [marker, num(get(s, ["totals", "reads_demux_by_marker", marker], 0))]);
      const mappedTotal = markerValues.reduce((acc, entry) => acc + entry[1], 0);
      const unspec = Math.max(0, total - mappedTotal);
      const rowDiv = document.createElement("div");
      rowDiv.className = "bar-row";
      const rowLbl = total > 0 ? document.createElement("a") : document.createElement("span");
      rowLbl.textContent = s.label;
      if (total > 0) {
        rowLbl.href = `#sample-${s.sample_id}`;
        rowLbl.className = "bar-row-sample-link";
      }
      const bar = document.createElement("span");
      bar.className = "bar";
      if (total > 0) {
        const stack = document.createElement("span");
        stack.className = "bar-stack";
        stack.style.width = `${Math.round((total / maxTotal) * 100)}%`;
        markerValues.concat([["Unspecified", unspec]]).forEach(([name, v]) => {
          if (!v || v <= 0) return;
          const seg = document.createElement("span");
          seg.className = "bar-segment";
          seg.style.flex = `0 0 ${(v / total) * 100}%`;
          seg.style.background = name === "Unspecified" ? "#9e9e9e" : (markerColors.get(name) || "#9aa39b");
          maybeAddSegmentLabel(seg, v, (v / maxTotal) * 100);
          seg.addEventListener("mouseenter", (e) => showBarTooltip(`${name}: ${v}`, e));
          seg.addEventListener("mousemove", positionBarTooltip);
          seg.addEventListener("mouseleave", hideBarTooltip);
          stack.appendChild(seg);
        });
        bar.appendChild(stack);
      }
      const valueSpan = document.createElement("span");
      valueSpan.textContent = total > 0 ? String(total) : "N/A";
      rowDiv.appendChild(rowLbl);
      rowDiv.appendChild(bar);
      rowDiv.appendChild(valueSpan);
      card.appendChild(rowDiv);
    });


    if (mountBefore && mountBefore.parentNode) {
      mountBefore.parentNode.insertBefore(card, mountBefore);
    } else if (charts) {
      charts.appendChild(card);
    }
  }

  function drawReadsFate(title, items, opts) {
    const options = opts && typeof opts === "object" ? opts : {};
    const data = Array.isArray(items) ? items : rounds;
    const markers = markerOrderFromData(data);
    const markerColors = markerColorsFromData(data);
    const READ_FATE_COLORS = new Map();
    const READ_FATE_ORDER = [];
    markers.forEach((marker) => {
      const base = markerColors.get(marker) || "#9aa39b";
      [["BLAST-assigned", base], ["BLAST-unassigned", lightenHex(base, 0.45)], ["BLAST skipped", lightenHex(base, 0.7)]].forEach(([prefix, color]) => {
        const label = `${prefix} ${marker}`;
        READ_FATE_ORDER.push(label);
        READ_FATE_COLORS.set(label, color);
      });
    });
    READ_FATE_ORDER.push("On-target not demultiplexed", "Off-target");
    READ_FATE_COLORS.set("On-target not demultiplexed", "#f4a261");
    READ_FATE_COLORS.set("Off-target", "#D55E00");
    function hasValue(v) {
      return v !== null && v !== undefined;
    }
    const segmentsByRound = data.map((r) => {
      if (r && r._summary_missing) {
        return READ_FATE_ORDER.map((label) => ({ label, value: null, color: READ_FATE_COLORS.get(label), group: "mid" }));
      }
      const readFate = (r && typeof r.read_fate === "object" && r.read_fate) ? r.read_fate : {};
      const status = readFate.marker_split_status;
      const isValid = status === "ok";
      if (!isValid) {
        const dataReasons = Array.isArray(readFate.data_reason_codes) && readFate.data_reason_codes.length
          ? readFate.data_reason_codes.join(",")
          : "none";
        const chartReasons = Array.isArray(readFate.chart_reason_codes) && readFate.chart_reason_codes.length
          ? readFate.chart_reason_codes.join(",")
          : "none";
        const invalidReads = numAny(readFate.marker_split_invalid_read_count);
        r._readFateInlineStatus = `Read fate status: invalid (data_reasons: ${dataReasons}; chart_reasons: ${chartReasons}; invalid_reads: ${invalidReads})`;
      } else {
        r._readFateInlineStatus = null;
      }
      const markerCounts = readFate && typeof readFate.marker_counts === "object" ? readFate.marker_counts : {};
      const assignedMap = markerCounts && typeof markerCounts.chart_blast_assigned === "object" ? markerCounts.chart_blast_assigned : {};
      const unassignedMap = markerCounts && typeof markerCounts.chart_blast_unassigned === "object" ? markerCounts.chart_blast_unassigned : {};
      const skippedMap = markerCounts && typeof markerCounts.chart_blast_skipped === "object" ? markerCounts.chart_blast_skipped : {};
      const segments = [];
      markers.forEach((marker) => {
        const lower = marker.toLowerCase();
        segments.push({ label: `BLAST-assigned ${marker}`, value: isValid ? (assignedMap[marker] != null ? assignedMap[marker] : readFate[`chart_blast_assigned_${lower}`]) : null, color: READ_FATE_COLORS.get(`BLAST-assigned ${marker}`), group: "mid" });
        segments.push({ label: `BLAST-unassigned ${marker}`, value: isValid ? (unassignedMap[marker] != null ? unassignedMap[marker] : readFate[`chart_blast_unassigned_${lower}`]) : null, color: READ_FATE_COLORS.get(`BLAST-unassigned ${marker}`), group: "mid" });
        segments.push({ label: `BLAST skipped ${marker}`, value: isValid ? (skippedMap[marker] != null ? skippedMap[marker] : readFate[`chart_blast_skipped_${lower}`]) : null, color: READ_FATE_COLORS.get(`BLAST skipped ${marker}`), group: "mid" });
      });
      segments.push({ label: "On-target not demultiplexed", value: isValid && hasValue(readFate.chart_on_target_not_demultiplexed) ? readFate.chart_on_target_not_demultiplexed : null, color: READ_FATE_COLORS.get("On-target not demultiplexed"), group: "mid" });
      segments.push({ label: "Off-target", value: isValid && hasValue(readFate.chart_off_target) ? readFate.chart_off_target : null, color: READ_FATE_COLORS.get("Off-target"), group: "mid" });
      return segments;
    });

    const totalsByRound = segmentsByRound.map((segs) =>
      segs.reduce((acc, seg) => acc + (Number.isFinite(seg.value) ? seg.value : 0), 0)
    );
    const maxTotal = Math.max(1, ...totalsByRound);

    const card = document.createElement("div");
    card.className = "chart-card";
    const header = document.createElement("div");
    header.className = "chart-header";
    const h = document.createElement("h3");
    h.textContent = title;
    header.appendChild(h);
    const rowLabel = options.rowLabel || "round";
    const columns = [rowLabel]
      .concat(flatMapCompat(markers, (marker) => {
        const slug = markerSlug(marker);
        return [`blast_assigned_${slug}`, `blast_unassigned_${slug}`, `blast_skipped_${slug}`];
      }))
      .concat(["on_target_not_demultiplexed", "off_target"]);
    const tsvRows = data.map((r, idx) => {
      const segs = segmentsByRound[idx];
      const allMissing = segs.every((seg) => seg.value == null);
      const v = (i) => (segs[i] && Number.isFinite(segs[i].value)) ? segs[i].value : (allMissing ? "N/A" : 0);
      return [r.round_barcode || "-"].concat(segs.map((seg) => Number.isFinite(seg.value) ? seg.value : (allMissing ? "N/A" : 0)));
    });
    const downloads = document.createElement("div");
    downloads.className = "download-links";
    downloads.appendChild(makeDownloadLink(title, makeTsv(columns, tsvRows)));
    const pdfLink = makeAssetLink("Download PDF", getChartPdfPath(options.chartId || "", options.pageIndex));
    if (pdfLink) downloads.appendChild(pdfLink);
    header.appendChild(downloads);
    card.appendChild(header);

    const legend = document.createElement("div");
    legend.className = "chart-legend";
    const legendSet = new Set();
    segmentsByRound.forEach((segs) => {
      segs.forEach((seg) => {
        if (seg.value > 0) legendSet.add(seg.label);
      });
    });
    READ_FATE_ORDER.forEach((labelText) => {
      if (!legendSet.has(labelText)) return;
      const color = READ_FATE_COLORS.get(labelText) || "#9aa39b";
      const item = document.createElement("span");
      item.className = "chart-legend-item";
      const swatch = document.createElement("span");
      swatch.className = "chart-legend-swatch";
      swatch.style.background = color;
      const label = document.createElement("span");
      label.textContent = labelText;
      item.appendChild(swatch);
      item.appendChild(label);
      legend.appendChild(item);
    });
    card.appendChild(legend);

    data.forEach((r, idx) => {
      const segs = segmentsByRound[idx];
      const total = totalsByRound[idx];
      const allMissing = segs.every((seg) => seg.value == null);
      const row = document.createElement("div");
      row.className = "bar-row";
      const label = document.createElement("span");
      label.textContent = r._readFateInlineStatus ? `${r.round_barcode || "-"} (${r._readFateInlineStatus})` : (r.round_barcode || "-");
      const bar = document.createElement("span");
      bar.className = "bar";
      if (!allMissing && total > 0) {
        const stack = document.createElement("span");
        stack.className = "bar-stack";
        stack.style.width = `${Math.round((total / maxTotal) * 100)}%`;
        segs.forEach((seg, sIdx) => {
          if (!seg.value || seg.value <= 0) return;
          const segSpan = document.createElement("span");
          segSpan.className = "bar-segment";
          const pct = (seg.value / total) * 100;
          segSpan.style.flex = `0 0 ${pct}%`;
          segSpan.style.background = seg.color;
          maybeAddSegmentLabel(segSpan, seg.value, pct);
          const tooltipText = seg.note
            ? `${seg.label}: ${seg.value} (${seg.note})`
            : `${seg.label}: ${seg.value}`;
          segSpan.addEventListener("mouseenter", (e) => showBarTooltip(tooltipText, e));
          segSpan.addEventListener("mousemove", positionBarTooltip);
          segSpan.addEventListener("mouseleave", hideBarTooltip);
          const prev = segs[sIdx - 1];
          if (prev && prev.group !== seg.group) {
            segSpan.classList.add("bar-segment-transition");
          }
          stack.appendChild(segSpan);
        });
        bar.appendChild(stack);
      }
      const value = document.createElement("span");
      value.textContent = allMissing ? "N/A" : String(total);
      row.appendChild(label);
      row.appendChild(bar);
      row.appendChild(value);
      card.appendChild(row);
    });

    charts.appendChild(card);
  }

  const PAGE_SIZE = 10;
  let currentRunPage = 0;

  function renderRunPager(totalPages) {
    if (!runIndexPager) return;
    clearNode(runIndexPager);
    if (totalPages <= 1) return;
    const totalRuns = runIndex.length;
    const startIdx = currentRunPage * PAGE_SIZE + 1;
    const endIdx = Math.min(totalRuns, (currentRunPage + 1) * PAGE_SIZE);
    const range = document.createElement("span");
    range.className = "pager-range";
    range.textContent = `Showing runs ${startIdx}-${endIdx} of ${totalRuns}`;
    const prev = document.createElement("button");
    prev.textContent = "Prev";
    prev.disabled = currentRunPage <= 0;
    prev.addEventListener("click", () => {
      currentRunPage = Math.max(0, currentRunPage - 1);
      renderIndexView();
    });
    const next = document.createElement("button");
    next.textContent = "Next";
    next.disabled = currentRunPage >= totalPages - 1;
    next.addEventListener("click", () => {
      currentRunPage = Math.min(totalPages - 1, currentRunPage + 1);
      renderIndexView();
    });
    const label = document.createElement("span");
    label.className = "pager-label";
    label.textContent = `Page ${currentRunPage + 1} / ${totalPages}`;
    runIndexPager.appendChild(range);
    runIndexPager.appendChild(prev);
    runIndexPager.appendChild(label);
    runIndexPager.appendChild(next);
  }

  function renderIndexCharts(runsPage) {
    if (!charts) return;
    clearNode(charts);
    const runLike = runsPage.map(buildRunLike);
    const runStatusReadFateLike = runsPage.map(buildRunStatusReadFateLike);
    drawReadsFate("Reads Fate (Current run status)", runStatusReadFateLike, { breakdownTitle: "Per-run numeric breakdown", rowLabel: "run", chartId: "index_reads_fate", pageIndex: currentRunPage });
    drawActiveOtuByMarker(runLike, { breakdownTitle: "Per-run numeric breakdown", rowLabel: "run", chartId: "index_informative_otu", pageIndex: currentRunPage });
    drawConsensusEmittedByMarker(runLike, { breakdownTitle: "Per-run numeric breakdown", rowLabel: "run", chartId: "index_consensus_emitted", pageIndex: currentRunPage });
  }

  function renderIndexView() {
    const totalPages = Math.max(1, Math.ceil(runIndex.length / PAGE_SIZE));
    if (currentRunPage >= totalPages) {
      currentRunPage = Math.max(0, totalPages - 1);
    }
    const start = currentRunPage * PAGE_SIZE;
    const runsPage = runIndex.slice(start, start + PAGE_SIZE);
    renderRunTable(runsPage);
    renderIndexCharts(runsPage);
    renderRunPager(totalPages);
  }

  if (viewScope === "index") {
    setHidden(summarySection, true);
    setHidden(figuresSection, true);
    setHidden(roundsSection, true);
    setHidden(runsInfoSection, true);
    setHidden(sampleIndexSection, true);
    setHidden(sampleDetailsSection, true);
    setHidden(warningsSection, true);
    renderIndexView();
  } else {
    setHidden(runIndexPager, true);
    drawReadsFate("Reads Fate Per Round (Round intake)", null, { chartId: "run_reads_fate", pageIndex: 0 });
    drawStackedBars("OTU Fate Per Round", [
      { label: "Informative dynamic", path: ["otu", "canonical", "informative_dynamic"], color: "#59a14f", group: "canonical" },
      { label: ARCHIVE_LABELS.candidates, path: ["otu", "pruned", "prune_candidates"], color: "#9aa39b", group: "pruned" },
      { label: ARCHIVE_LABELS.size_streak, path: ["otu", "pruned", "size_streak"], color: "#f28e2b", group: "pruned" },
      { label: "BLAST-unassigned", path: ["otu", "pruned", "blast_unassigned"], color: "#CC79A7", group: "pruned" },
    ], null, { chartId: "run_otu_fate", pageIndex: 0 });
    drawActiveOtuByMarker(null, { chartId: "run_informative_otu", pageIndex: 0 });
    drawConsensusEmittedByMarker(null, { chartId: "run_consensus_emitted", pageIndex: 0 });
  }

  function renderOtuAssignmentsTable(round, mountNode = charts) {
    if (!mountNode) return;
    const levels = ["species", "genus", "family"];
    const rawRoot = round && round.otu && round.otu.assignments_by_level ? round.otu.assignments_by_level : {};
    const dataRoot = {};
    levels.forEach((level) => {
      const rows = Array.isArray(rawRoot[level]) ? rawRoot[level] : [];
      dataRoot[level] = rows.filter((row) => isSupportedOtuAssignment(row) && otuRowMeetsConfiguredThreshold(round, level, row));
    });
    if (rawRoot && rawRoot.species_interest_enabled != null) {
      dataRoot.species_interest_enabled = rawRoot.species_interest_enabled;
    }
    renderAssignmentsTable({
      title: "OTU Assignments",
      dataRoot,
      countKey: "otu_count",
      countLabel: "OTUs",
      emptyText: "No assignments available for this level.",
      mountNode,
      extraCols: [
        { header: "Frozen OTUs", key: "frozen_otu_count" },
      ],
    });
  }

  function renderConsensusAssignmentsTable(round, mountNode = charts) {
    if (!mountNode) return;
    const dataRoot = round && round.consensus && round.consensus.assignments_by_level ? round.consensus.assignments_by_level : {};
    renderAssignmentsTable({
      title: "Consensus Assignments",
      dataRoot,
      countKey: "consensus_count",
      countLabel: "Consensus",
      emptyText: "No consensus assignments available for this level.",
      mountNode,
      extraCols: [
        { header: "Consolidated Consensus", key: "consolidated_consensus_count" },
      ],
    });
  }

  function formatFastaSequence(sequence) {
    const raw = typeof sequence === "string" ? sequence.replace(/\s+/g, "").trim() : "";
    if (!raw) return "";
    const lines = [];
    for (let idx = 0; idx < raw.length; idx += 80) {
      lines.push(raw.slice(idx, idx + 80));
    }
    return lines.join("\n");
  }

  function encodeFastaHeaderValue(value) {
    return String(value || "")
      .trim()
      .split(/\s*>\s*/)
      .map((part) => part.trim().replace(/\s+/g, "_").replace(/\|+/g, "/"))
      .filter(Boolean)
      .join(";");
  }

  function buildConsensusFastaHeader(row) {
    const baseHeader = row && row.header ? String(row.header).replace(/^>+/, "").trim() : "";
    if (!baseHeader) return "";
    const suffix = [];
    if (row && row.otu_assignment) {
      suffix.push(`otu_assign=${encodeFastaHeaderValue(row.otu_assignment)}`);
    }
    if (row && row.consensus_assignment) {
      suffix.push(`cons_assign=${encodeFastaHeaderValue(row.consensus_assignment)}`);
    }
    if (!suffix.length) return baseHeader;
    return `${baseHeader}|${suffix.join("|")}`;
  }

  function makeFasta(records) {
    return (Array.isArray(records) ? records : [])
      .map((row) => {
        const header = buildConsensusFastaHeader(row);
        const seq = formatFastaSequence(row && row.sequence ? row.sequence : "");
        if (!header || !seq) return "";
        return `>${header}\n${seq}`;
      })
      .filter(Boolean)
      .join("\n");
  }

  function renderConsensusSequenceTable(round, mountNode = charts, options = {}) {
    if (!mountNode) return;
    const title = options && options.title ? String(options.title) : "Consensus Sequences (FASTA)";
    const rowsKey = options && options.rowsKey ? String(options.rowsKey) : "sequence_rows";
    const emptyText = options && options.emptyText ? String(options.emptyText) : "No consensus sequences available.";
    const downloadBaseName = options && options.downloadBaseName ? String(options.downloadBaseName) : "Consensus Sequences";
    const rows = [];
    const sourceRows = round && round.consensus && Array.isArray(round.consensus[rowsKey])
      ? round.consensus[rowsKey]
      : [];
    const filteredRows = sourceRows.filter(Boolean);
    rows.push(...filteredRows);
    const fastaRepLabels = collectReplicateLabels(rows);

    const card = document.createElement("div");
    card.className = "chart-card";
    const header = document.createElement("div");
    header.className = "chart-header";
    const h = document.createElement("h3");
    h.textContent = title;
    header.appendChild(h);

    if (rows.length) {
      const downloads = document.createElement("div");
      downloads.className = "download-links";
      const tsvCols = ["sample", "marker", "consensus_id", "otu_key", "otu_assignment", "consensus_assignment", "reads_used", "length", ...fastaRepLabels];
      const tsvRows = rows.map((row) => [
        row.sample || "",
        row.marker || "",
        row.consensus_id || "",
        row.otu_key || "",
        row.otu_assignment || "",
        row.consensus_assignment || "",
        row.reads_used != null ? row.reads_used : "",
        row.length != null ? row.length : "",
        ...fastaRepLabels.map((label) => replicateCountForLabel(row, label)),
      ]);
      downloads.appendChild(makeDownloadLink(downloadBaseName, makeTsv(tsvCols, tsvRows)));
      downloads.appendChild(
        makeNamedDownloadLink(
          downloadBaseName,
          makeFasta(rows),
          "fasta",
          "text/plain",
          "Download FASTA",
        ),
      );
      header.appendChild(downloads);
    }

    card.appendChild(header);

    const tableWrap = document.createElement("div");
    tableWrap.className = "otu-assign-table";
    const pager = document.createElement("div");
    pager.className = "otu-assign-pager";
    card.appendChild(tableWrap);
    card.appendChild(pager);
    mountNode.appendChild(card);

    if (!rows.length) {
      const empty = document.createElement("p");
      empty.className = "otu-assign-empty";
      empty.textContent = emptyText;
      tableWrap.appendChild(empty);
      return;
    }

    let page = 0;
    const pageSize = 10;

    function renderPage() {
      clearNode(tableWrap);
      clearNode(pager);
      const totalPages = Math.max(1, Math.ceil(rows.length / pageSize));
      if (page >= totalPages) page = totalPages - 1;
      const start = page * pageSize;
      const pageRows = rows.slice(start, start + pageSize);

      const table = document.createElement("table");
      table.className = "otu-assign-table-inner";
      const thead = document.createElement("thead");
      const trh = document.createElement("tr");
      ["Sample", "Marker", "Consensus ID", "OTU", "OTU Assignment", "Consensus Assignment", "Reads Used", "Length"].forEach((col) => {
        const th = document.createElement("th");
        th.textContent = col;
        trh.appendChild(th);
      });
      fastaRepLabels.forEach((label) => {
        const th = document.createElement("th");
        th.textContent = label;
        th.title = "OTU reads by replicate (total cluster members, may exceed reads used for consensus)";
        trh.appendChild(th);
      });
      thead.appendChild(trh);
      table.appendChild(thead);

      const tbody = document.createElement("tbody");
      pageRows.forEach((row) => {
        const tr = document.createElement("tr");
        [
          row.sample || "",
          row.marker || "",
          row.consensus_id || "",
          row.otu_key || "",
          row.otu_assignment || "",
          row.consensus_assignment || "",
          row.reads_used != null ? row.reads_used : "",
          row.length != null ? row.length : "",
        ].forEach((value) => {
          const td = document.createElement("td");
          td.textContent = String(value);
          tr.appendChild(td);
        });
        fastaRepLabels.forEach((label) => {
          const td = document.createElement("td");
          td.textContent = String(replicateCountForLabel(row, label));
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      tableWrap.appendChild(table);

      const total = rows.length;
      const startIdx = start + 1;
      const endIdx = Math.min(total, start + pageSize);
      const range = document.createElement("span");
      range.className = "otu-assign-range";
      range.textContent = `Showing ${startIdx}-${endIdx} of ${total}`;
      const prev = document.createElement("button");
      prev.textContent = "Prev";
      prev.disabled = page <= 0;
      prev.addEventListener("click", () => {
        page = Math.max(0, page - 1);
        renderPage();
      });
      const next = document.createElement("button");
      next.textContent = "Next";
      next.disabled = page >= totalPages - 1;
      next.addEventListener("click", () => {
        page = Math.min(totalPages - 1, page + 1);
        renderPage();
      });
      const label = document.createElement("span");
      label.className = "otu-assign-page";
      label.textContent = `Page ${page + 1} / ${totalPages}`;
      pager.appendChild(range);
      pager.appendChild(prev);
      pager.appendChild(label);
      pager.appendChild(next);
    }

    renderPage();
  }

  // Return sorted unique rep_N labels across all rows that have >=2 replicates.
  function collectReplicateLabels(rows) {
    if (!Array.isArray(rows)) return [];
    const labels = new Set();
    rows.forEach((row) => {
      if (Array.isArray(row.replicate_reads) && row.replicate_reads.length > 1) {
        row.replicate_reads.forEach((r) => { if (r && r.label) labels.add(r.label); });
      }
    });
    return Array.from(labels).sort((a, b) => {
      const na = parseInt((a.match(/_(\d+)$/) || [])[1] || "0", 10);
      const nb = parseInt((b.match(/_(\d+)$/) || [])[1] || "0", 10);
      return na - nb || a.localeCompare(b);
    });
  }

  // Return the read count for a specific replicate label from a row's replicate_reads array.
  function replicateCountForLabel(row, label) {
    if (!Array.isArray(row.replicate_reads)) return 0;
    const entry = row.replicate_reads.find((r) => r && r.label === label);
    return entry ? (entry.count || 0) : 0;
  }

  function renderAssignmentsTable({ title, dataRoot, countKey, countLabel, emptyText, extraCols = [], mountNode = charts }) {
    if (!mountNode) return;
    const levels = ["species", "genus", "family"];
    const card = document.createElement("div");
    card.className = "chart-card";

    const header = document.createElement("div");
    header.className = "chart-header";
    const h = document.createElement("h3");
    h.textContent = title;
    header.appendChild(h);
    const tabBar = document.createElement("div");
    tabBar.className = "otu-assign-tabs";
    const tableWrap = document.createElement("div");
    tableWrap.className = "otu-assign-table";
    const pager = document.createElement("div");
    pager.className = "otu-assign-pager";

    const pageState = {};
    levels.forEach((lvl) => {
      pageState[lvl] = 0;
    });

    function rowsForLevel(level) {
      const rows = Array.isArray(dataRoot[level]) ? dataRoot[level] : [];
      return rows;
    }

    function renderTable(level) {
      clearNode(tableWrap);
      clearNode(pager);
      const rows = rowsForLevel(level);
      if (!rows.length) {
        const empty = document.createElement("p");
        empty.className = "otu-assign-empty";
        empty.textContent = emptyText || "No assignments available for this level.";
        tableWrap.appendChild(empty);
        return;
      }
      const pageSize = 10;
      const totalPages = Math.max(1, Math.ceil(rows.length / pageSize));
      if (pageState[level] >= totalPages) pageState[level] = totalPages - 1;
      const start = pageState[level] * pageSize;
      const pageRows = rows.slice(start, start + pageSize);

      const repLabels = collectReplicateLabels(rows);
      const table = document.createElement("table");
      table.className = "otu-assign-table-inner";
      const thead = document.createElement("thead");
      const trh = document.createElement("tr");
      const showInterest = dataRoot && dataRoot.species_interest_enabled === true;
      const cols = [
        "Taxon",
        "Sample",
        "Marker",
        countLabel,
        ...extraCols.map((c) => c.header),
        "Reads",
        "Min %ID",
        "Max %ID",
        "Min Aln Length",
        "Max Aln Length",
      ];
      if (showInterest) cols.push("Species of Interest");
      cols.forEach((col) => {
        const th = document.createElement("th");
        th.textContent = col;
        trh.appendChild(th);
      });
      repLabels.forEach((label) => {
        const th = document.createElement("th");
        th.textContent = label;
        trh.appendChild(th);
      });
      thead.appendChild(trh);
      table.appendChild(thead);
      const tbody = document.createElement("tbody");
      pageRows.forEach((row) => {
        const tr = document.createElement("tr");
        const values = [
          row.taxon || "",
          row.sample || "",
          row.marker || "",
          row[countKey] != null ? row[countKey] : "N/A",
          ...extraCols.map((c) => (row[c.key] != null ? row[c.key] : "")),
          row.reads_total != null ? row.reads_total : "N/A",
          (row.perc_id_min != null && row.perc_id_min >= 0 && row.perc_id_min <= 100) ? row.perc_id_min : "",
          (row.perc_id_max != null && row.perc_id_max >= 0 && row.perc_id_max <= 100) ? row.perc_id_max : "",
          (row.aln_length_min != null && row.aln_length_min >= 1) ? row.aln_length_min : "",
          (row.aln_length_max != null && row.aln_length_max >= 1) ? row.aln_length_max : "",
        ];
        if (showInterest) {
          let flag = "";
          if (row.species_interest === true) flag = "TRUE";
          else if (row.species_interest === false) flag = "FALSE";
          values.push(flag);
        }
        values.forEach((v) => {
          const td = document.createElement("td");
          td.textContent = String(v);
          tr.appendChild(td);
        });
        repLabels.forEach((label) => {
          const td = document.createElement("td");
          td.textContent = String(replicateCountForLabel(row, label));
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      tableWrap.appendChild(table);

      const total = rows.length;
      const startIdx = start + 1;
      const endIdx = Math.min(total, start + pageSize);
      const range = document.createElement("span");
      range.className = "otu-assign-range";
      range.textContent = `Showing ${startIdx}-${endIdx} of ${total}`;
      const prev = document.createElement("button");
      prev.textContent = "Prev";
      prev.disabled = pageState[level] <= 0;
      prev.addEventListener("click", () => {
        pageState[level] = Math.max(0, pageState[level] - 1);
        renderTable(level);
      });
      const next = document.createElement("button");
      next.textContent = "Next";
      next.disabled = pageState[level] >= totalPages - 1;
      next.addEventListener("click", () => {
        pageState[level] = Math.min(totalPages - 1, pageState[level] + 1);
        renderTable(level);
      });
      const label = document.createElement("span");
      label.className = "otu-assign-page";
      label.textContent = `Page ${pageState[level] + 1} / ${totalPages}`;
      pager.appendChild(range);
      pager.appendChild(prev);
      pager.appendChild(label);
      pager.appendChild(next);
    }

    let activeLevel = "species";

    levels.forEach((lvl) => {
      const btn = document.createElement("button");
      btn.className = "otu-assign-tab";
      btn.textContent = lvl.charAt(0).toUpperCase() + lvl.slice(1);
      if (lvl === activeLevel) btn.classList.add("active");
      btn.addEventListener("click", () => {
        activeLevel = lvl;
        Array.from(tabBar.children).forEach((child) => child.classList.remove("active"));
        btn.classList.add("active");
        renderTable(activeLevel);
      });
      tabBar.appendChild(btn);
    });

    const allLevels = ["species", "genus", "family"];
    const allRepLabels = collectReplicateLabels(
      flatMapCompat(allLevels, (lvl) => Array.isArray(dataRoot[lvl]) ? dataRoot[lvl] : [])
    );
    const combinedRows = flatMapCompat(allLevels, (lvl) => {
      const rows = Array.isArray(dataRoot[lvl]) ? dataRoot[lvl] : [];
      return rows.map((row) => {
        const extra = {};
        extraCols.forEach((c) => { extra[c.key] = row[c.key] != null ? row[c.key] : ""; });
        return {
          level: lvl,
          taxon: row.taxon || "",
          family: row.family || "",
          genus: row.genus || "",
          species: row.species || "",
          sample: row.sample || "",
          marker: row.marker || "",
          count: row[countKey] != null ? row[countKey] : "",
          ...extra,
          reads_total: row.reads_total != null ? row.reads_total : "",
          perc_id_min: row.perc_id_min != null ? row.perc_id_min : "",
          perc_id_max: row.perc_id_max != null ? row.perc_id_max : "",
          aln_length_min: row.aln_length_min != null ? row.aln_length_min : "",
          aln_length_max: row.aln_length_max != null ? row.aln_length_max : "",
          species_interest: row.species_interest === true ? "TRUE" : (row.species_interest === false ? "FALSE" : ""),
          _row_ref: row,
        };
      });
    });
    const tsvCols = [
      "level",
      "taxon",
      "family",
      "genus",
      "species",
      "sample",
      "marker",
      countLabel,
      ...extraCols.map((c) => c.header),
      "reads",
      "%id min",
      "%id max",
      "aln min",
      "aln max",
      "species of interest",
      ...allRepLabels,
    ];
    const tsvRows = combinedRows.map((row) => [
      row.level,
      row.taxon,
      row.family,
      row.genus,
      row.species,
      row.sample,
      row.marker,
      row.count,
      ...extraCols.map((c) => row[c.key]),
      row.reads_total,
      row.perc_id_min,
      row.perc_id_max,
      row.aln_length_min,
      row.aln_length_max,
      row.species_interest,
      ...allRepLabels.map((label) => replicateCountForLabel(row._row_ref, label)),
    ]);
    header.appendChild(makeDownloadLink(title, makeTsv(tsvCols, tsvRows)));
    card.appendChild(header);
    card.appendChild(tabBar);
    card.appendChild(tableWrap);
    card.appendChild(pager);
    mountNode.appendChild(card);
    renderTable(activeLevel);
  }

  function detectDemuxEnabled() {
    return rounds.some((r) => {
      const demuxTotal = get(r, ["read_fate", "demux_total_reads"], null);
      const noAdapter = get(r, ["read_fate", "no_adapter_reads"], null);
      const demuxEnabled = get(r, ["read_fate", "demux_enabled"], null);
      if (demuxEnabled === true) {
        return true;
      }
      if (demuxEnabled === false) return false;
      if (demuxTotal != null && noAdapter != null) return Number(demuxTotal) > Number(noAdapter);
      return false;
    });
  }

  function appendFigureCard(container, f) {
    if (!container || !f) return;
    const card = document.createElement("div");
    card.className = "figure-card";
    const title = document.createElement("h3");
    title.textContent = f.title || f.id || "Figure";
    const desc = document.createElement("p");
    desc.textContent = f.description || "";
    let img;
    const isReady = f.exists === true;
    if (isReady) {
      img = document.createElement("img");
      img.alt = f.title || f.id || "Figure";
      img.loading = "lazy";
      img.className = "figure-thumb";
      img.dataset.figTitle = title.textContent;
      img.dataset.figDesc = desc.textContent;
      img.src = f.path || "";
      img.tabIndex = 0;
      img.setAttribute("role", "button");
    } else {
      img = document.createElement("div");
      img.className = "figure-placeholder figure-missing";
      img.setAttribute("aria-disabled", "true");
      const placeholder = document.createElement("span");
      placeholder.textContent = "No plot yet";
      img.appendChild(placeholder);
    }
    card.appendChild(title);
    if (desc.textContent) {
      card.appendChild(desc);
    }
    card.appendChild(img);
    if (f.pdf_exists === true && typeof f.pdf_path === "string" && f.pdf_path) {
      const downloads = document.createElement("div");
      downloads.className = "download-links figure-downloads";
      const pdfLink = makeAssetLink("Download PDF", f.pdf_path);
      if (pdfLink) downloads.appendChild(pdfLink);
      card.appendChild(downloads);
    }
    if (!isReady) {
      const note = document.createElement("p");
      note.className = "figure-note";
      note.textContent = "Not generated yet";
      card.appendChild(note);
    }
    container.appendChild(card);
  }

  function renderFigureGallery(container, inputFigures, emptyText) {
    if (!container) return;
    const demuxOn = detectDemuxEnabled();
    const shown = Array.isArray(inputFigures) ? inputFigures.filter((f) => f) : [];
    if (!shown.length) {
      const empty = document.createElement("p");
      empty.className = "figure-empty";
      empty.textContent = emptyText || "No figures available for this round yet.";
      container.appendChild(empty);
      return;
    }
    const SECTION_ORDER = [
      "Global Overview",
      "Read QC / FAST",
      "Demultiplexing",
      "OTU Definition",
      "BLAST / Pre-Taxonomy",
      "Consensus",
      "Other",
    ];
    const sectionRank = new Map(SECTION_ORDER.map((s, i) => [s, i]));
    const groups = new Map();
    shown.forEach((f) => {
      if (!demuxOn && (f.section || "") === "Demultiplexing") return;
      const section = (f.section || "Other").toString();
      if (!groups.has(section)) {
        groups.set(section, []);
      }
      groups.get(section).push(f);
    });
    const sortedSections = Array.from(groups.keys()).sort((a, b) => {
      const ra = sectionRank.has(a) ? sectionRank.get(a) : 999;
      const rb = sectionRank.has(b) ? sectionRank.get(b) : 999;
      if (ra !== rb) return ra - rb;
      return a.localeCompare(b);
    });
    sortedSections.forEach((section) => {
      const header = document.createElement("h3");
      header.className = "figure-section";
      header.textContent = section;
      container.appendChild(header);
      const items = groups.get(section).slice().sort((a, b) => {
        const oa = typeof a.order === "number" ? a.order : 999;
        const ob = typeof b.order === "number" ? b.order : 999;
        if (oa !== ob) return oa - ob;
        const ta = (a.title || a.id || "").toString();
        const tb = (b.title || b.id || "").toString();
        return ta.localeCompare(tb);
      });
      items.forEach((f) => appendFigureCard(container, f));
    });
  }

  function renderRunEvolutionSection(inputFigures) {
    if (viewScope !== "run") return;
    const globalOverview = document.getElementById("global-overview");
    if (!globalOverview || !runTableBody) return;
    const existing = document.getElementById("run-evolution-section");
    if (existing) return existing;
    const allFigures = Array.isArray(inputFigures) ? inputFigures.filter((f) => f) : [];
    const byId = new Map(allFigures.map((fig) => [fig.id, fig]));
    const selected = [
      "reads_time",
      "reads_cumulative_log",
      "otu_tax_time",
      "otu_frozen_tax_time",
      "consensus_tax_time",
      "consensus_consolidated_tax_time",
    ].map((id) => byId.get(id)).filter(Boolean);
    if (!selected.length) return;

    const section = document.createElement("section");
    section.id = "run-evolution-section";
    const header = document.createElement("h3");
    header.textContent = "Run evolution";
    section.appendChild(header);
    const grid = document.createElement("div");
    grid.className = "figures-grid run-evolution-grid";
    selected.forEach((fig) => appendFigureCard(grid, fig));
    section.appendChild(grid);

    const runsSection = runTableBody.closest("section");
    const currentResultsSection = document.getElementById("current-run-results-section");
    if (currentResultsSection && currentResultsSection.parentNode === globalOverview) {
      globalOverview.insertBefore(section, currentResultsSection);
    } else if (runsSection && runsSection.parentNode === globalOverview) {
      globalOverview.insertBefore(section, runsSection);
    } else {
      globalOverview.appendChild(section);
    }
    return section;
  }

  function ensureCurrentRunResultsSection() {
    if (viewScope !== "run") return null;
    const globalOverview = document.getElementById("global-overview");
    if (!globalOverview || !runTableBody) return null;
    const existing = document.getElementById("current-run-results-section");
    if (existing) return existing;

    const section = document.createElement("section");
    section.id = "current-run-results-section";
    const header = document.createElement("h3");
    header.textContent = "Current Run Results";
    section.appendChild(header);
    const intro = document.createElement("p");
    intro.className = "species-sample-help";
    intro.textContent = "Current-state pipeline outputs at the latest round, including assignments carried forward from frozen and consolidated OTUs and consensus.";
    section.appendChild(intro);

    const runsSection = runTableBody.closest("section");
    const evolutionSection = document.getElementById("run-evolution-section");
    if (evolutionSection && evolutionSection.parentNode === globalOverview) {
      globalOverview.insertBefore(section, evolutionSection.nextSibling);
    } else if (runsSection && runsSection.parentNode === globalOverview) {
      globalOverview.insertBefore(section, runsSection);
    } else {
      globalOverview.appendChild(section);
    }
    return section;
  }

  function safeRenderSection(label, renderFn) {
    if (typeof renderFn !== "function") return;
    try {
      renderFn();
    } catch (err) {
      const detail = err && err.message ? err.message : String(err || "unknown error");
      appendWarning(`render_failed:${label}: ${detail}`);
    }
  }

  if (viewScope === "run") {
    const allRunFigures = Array.isArray(payload.figures) ? payload.figures : [];
    const assetSnapshotPolicy = get(lastRound, ["asset_snapshot_policy"], "");
    if (assetSnapshotPolicy === "latest_only" && figuresSection) {
      const note = document.createElement("p");
      note.className = "subtitle";
      note.textContent = "Round-linked figures, tables, and sequences reflect the latest completed round only.";
      figuresSection.insertBefore(note, figuresSection.firstChild);
    }
    renderRunEvolutionSection(allRunFigures);
    renderFigureGallery(
      figures,
      allRunFigures.filter((f) => f && !RUN_EVOLUTION_FIGURE_IDS.has(f.id)),
      "No figures available for this round yet.",
    );
    const currentRunResultsSection = ensureCurrentRunResultsSection();
    const lastRoundFailure = getFailureInfo(lastRound);
    if (lastRoundFailure) {
      const prefix = lastRoundFailure.roundBarcode || "latest_round_failed";
      appendWarning(`${prefix}: ${lastRoundFailure.reason}`);
    }
    safeRenderSection("run_sunbursts", () => renderRunSunburstCards(lastRound, currentRunResultsSection));
    safeRenderSection("assignments_by_sample", () => renderSpeciesSampleTable(currentRunResultsSection));
    safeRenderSection("otu_assignments", () => renderOtuAssignmentsTable(lastRound, currentRunResultsSection));
    safeRenderSection("consensus_assignments", () => renderConsensusAssignmentsTable(lastRound, currentRunResultsSection));
    safeRenderSection("consensus_sequences", () => renderConsensusSequenceTable(lastRound, currentRunResultsSection));
    safeRenderSection("consolidated_consensus_sequences", () => renderConsensusSequenceTable(lastRound, currentRunResultsSection, {
      title: "Consolidated Consensus Sequences (FASTA)",
      rowsKey: "consolidated_sequence_rows",
      emptyText: "No consolidated consensus sequences available.",
      downloadBaseName: "Consolidated Consensus Sequences",
    }));

    rounds.forEach((r) => {
      const ws = Array.isArray(r.warnings) ? r.warnings : [];
      ws.forEach((w) => {
        appendWarning(`${r.round_barcode}: ${w}`);
      });
    });
  }

  // ── Treemap helpers ──────────────────────────────────────────────────────
  // Mirror Perl SampleLabel::normalize_sample_base: strip replicate/marker
  // suffixes so row.sample ("adapter_1_COI_1") matches s.label ("adapter_1").
  function normalizeBase(raw) {
    let s = (raw || "").replace(/^\s+|\s+$/g, "");
    if (!s) return "";
    if (/^no_adapter(?:_\d+)?$/i.test(s)) return "no_adapter";
    s = s.replace(/_\d+$/, "");             // strip trailing replicate _N
    const configuredSuffix = markerOrderFromData(rounds)
      .map((marker) => marker.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
      .filter(Boolean)
      .join("|");
    if (configuredSuffix) {
      s = s.replace(new RegExp(`_(?:${configuredSuffix})$`, "i"), "");
    } else {
      s = s.replace(/_(?:COI|ITS)\d*$/i, "");
    }
    s = s.replace(/_\d+_/, "_");            // strip embedded _N_
    return s.replace(/^\s+|\s+$/g, "");
  }

  function groupEntityLabel() {
    if (groupViewMode === "replicate") return "Primer";
    if (groupViewMode === "track_detail") return "Replicate";
    return "Sample";
  }

  function groupEntityPlural() {
    if (groupViewMode === "replicate") return "Primers";
    if (groupViewMode === "track_detail") return "Replicates";
    return "Samples";
  }

  function currentGroupLabel(raw) {
    const text = (raw || "").toString().trim();
    if (!text) return "";
    if (reportIdentityMode === "track" && groupViewMode === "track_detail") return text;
    if (reportIdentityMode === "track" && groupViewMode === "replicate") return text;
    return normalizeBase(text);
  }

  function hasTrackUnitMetrics(round) {
    return !!(round && typeof round.track_unit_metrics === "object" && !Array.isArray(round.track_unit_metrics));
  }

  function trackSampleReplicateLabel(raw) {
    if (!raw || typeof raw !== "object") return "";
    const direct = (raw.track_sample_replicate_label || "").toString().trim();
    if (direct) return direct;
    const sampleLabel = (raw.track_sample_label || "").toString().trim();
    const repNum = raw.track_replicate_number != null ? String(raw.track_replicate_number).trim() : "";
    if (sampleLabel && repNum) return `${sampleLabel}_${repNum}`;
    const fallbacks = [
      raw.track_replicate_label,
      raw.track_replicate_id,
      raw.label,
      raw.sample,
      raw.track_unit_id,
    ];
    for (let i = 0; i < fallbacks.length; i += 1) {
      const text = (fallbacks[i] || "").toString().trim();
      if (!text) continue;
      const match = text.match(/^(.*?_\d+)_[^_]+$/);
      if (match) return match[1];
      return text;
    }
    return "";
  }

  function metricGroupLabel(raw) {
    if (!raw || typeof raw !== "object") return "";
    if (reportIdentityMode === "track" && hasTrackUnitMetrics(rounds[rounds.length - 1] || {})) {
      if (groupViewMode === "track_detail") return trackSampleReplicateLabel(raw);
      if (groupViewMode === "replicate") return (raw.track_replicate_label || raw.track_replicate_id || "").toString().trim();
      if (groupViewMode === "sample") return (raw.track_sample_label || "").toString().trim();
    }
    return currentGroupLabel(raw.label || raw.sample_id || "");
  }

  function assignmentGroupLabel(row) {
    if (!row || typeof row !== "object") return "";
    if (reportIdentityMode === "track") {
      if (groupViewMode === "track_detail") {
        return trackSampleReplicateLabel(row) || (row.sample || "").toString().trim();
      }
      if (groupViewMode === "replicate") {
        return (row.track_replicate_label || row.track_replicate_id || row.sample || "").toString().trim();
      }
      if (groupViewMode === "sample") {
        return (row.track_sample_label || currentGroupLabel(row.sample || "")).toString().trim();
      }
    }
    return currentGroupLabel(row.sample || "");
  }

  function stableDomId(prefix, value) {
    const text = (value || "").toString();
    let out = `${prefix}_`;
    for (let i = 0; i < text.length; i += 1) {
      out += text.charCodeAt(i).toString(16).padStart(2, "0");
    }
    return out;
  }

  function currentGroupId(label) {
    const prefix = groupViewMode === "replicate"
      ? "primer"
      : (groupViewMode === "track_detail" ? "replicate" : "sample");
    return stableDomId(prefix, label || "unknown");
  }

  function mergeMarkerCounts(target, source) {
    const out = target && typeof target === "object" ? target : {};
    const incoming = source && typeof source === "object" ? source : {};
    Object.keys(incoming).forEach((marker) => {
      const canonical = canonicalMarkerToken(marker);
      if (!canonical) return;
      out[canonical] = num(out[canonical]) + num(incoming[marker]);
    });
    return out;
  }

  function getGroupedSampleMetricEntries(round, sampleLabel) {
    const useTrackUnits = reportIdentityMode === "track" && hasTrackUnitMetrics(round);
    const sampleMetrics = useTrackUnits
      ? round.track_unit_metrics
      : ((round && typeof round.sample_metrics === "object" && !Array.isArray(round.sample_metrics))
        ? round.sample_metrics
        : {});
    const expectedLabel = (sampleLabel || "").toString();
    return Object.values(sampleMetrics).filter((raw) => {
      if (!raw || typeof raw !== "object") return false;
      const label = useTrackUnits ? metricGroupLabel(raw) : currentGroupLabel(raw.label || raw.sample_id || "");
      return label === expectedLabel;
    });
  }

  function aggregateSampleMetricEntries(entries, sampleId, sampleLabel) {
    const list = Array.isArray(entries) ? entries.filter((raw) => raw && typeof raw === "object") : [];
    if (!list.length) return null;
    const keepManifestFigures = reportIdentityMode !== "track";
    const aggregate = {
      sample_id: sampleId || currentGroupId(sampleLabel),
      label: sampleLabel || "",
      reads_demux: 0,
      reads_demux_coi: undefined,
      reads_demux_its2: undefined,
      reads_demux_by_marker: {},
      reads_blast_assigned: 0,
      otu_active: 0,
      otu_total: undefined,
      consensus_emitted: 0,
      consensus_total: undefined,
      figures: [],
      replicates: {},
    };
    list.forEach((raw) => {
      aggregate.reads_demux += num(raw.reads_demux);
      if (raw.reads_demux_coi != null) aggregate.reads_demux_coi = num(aggregate.reads_demux_coi) + num(raw.reads_demux_coi);
      if (raw.reads_demux_its2 != null) aggregate.reads_demux_its2 = num(aggregate.reads_demux_its2) + num(raw.reads_demux_its2);
      mergeMarkerCounts(aggregate.reads_demux_by_marker, raw.reads_demux_by_marker);
      aggregate.reads_blast_assigned += num(raw.reads_blast_assigned);
      aggregate.otu_active += num(raw.otu_active);
      if (raw.otu_total != null) aggregate.otu_total = num(aggregate.otu_total) + num(raw.otu_total);
      aggregate.consensus_emitted += num(raw.consensus_emitted);
      if (raw.consensus_total != null) aggregate.consensus_total = num(aggregate.consensus_total) + num(raw.consensus_total);
      if (keepManifestFigures && !aggregate.figures.length) {
        const sampleFigs = Array.isArray(raw.figures) ? raw.figures.filter(Boolean) : [];
        if (sampleFigs.length) aggregate.figures = sampleFigs;
      }
    });
    if (aggregate.reads_demux_coi == null) aggregate.reads_demux_coi = num(aggregate.reads_demux_by_marker.COI);
    if (aggregate.reads_demux_its2 == null) aggregate.reads_demux_its2 = num(aggregate.reads_demux_by_marker.ITS2);
    return aggregate;
  }

  const TREEMAP_PALETTE = [
    "#4c956c","#2c6e49","#1d5c3a","#1d4e89","#3a7fad","#6baed6",
    "#8b4513","#9B5090","#e07b39","#d4a017","#9a7d0a","#6b4226",
    "#7b2d8b","#b5508a","#c78b5e","#3f6f3f","#558b6e","#7d6e83",
    "#5d7c6e","#9b7653",
  ];
  const tmFamilyColorMap = new Map();
  let tmFamilyColorIdx = 0;
  function tmFamilyColor(family) {
    if (!family) return "#9aa39b";
    if (!tmFamilyColorMap.has(family)) {
      tmFamilyColorMap.set(family, TREEMAP_PALETTE[tmFamilyColorIdx % TREEMAP_PALETTE.length]);
      tmFamilyColorIdx++;
    }
    return tmFamilyColorMap.get(family);
  }

  function tmLayout(items, x, y, w, h, total) {
    if (!items.length) return [];
    if (items.length === 1) return [{ ...items[0], x, y, w, h }];
    let half = total / 2, cum = 0, split = items.length - 1;
    for (let i = 0; i < items.length - 1; i++) {
      cum += items[i].reads_total;
      if (cum >= half) { split = i + 1; break; }
    }
    const a = items.slice(0, split);
    const b = items.slice(split);
    const aTotal = a.reduce((s, d) => s + d.reads_total, 0);
    const ratio = aTotal / total;
    if (w >= h) {
      const sw = w * ratio;
      return [
        ...tmLayout(a, x, y, sw, h, aTotal),
        ...tmLayout(b, x + sw, y, w - sw, h, total - aTotal),
      ];
    }
    const sh = h * ratio;
    return [
      ...tmLayout(a, x, y, w, sh, aTotal),
      ...tmLayout(b, x, y + sh, w, h - sh, total - aTotal),
    ];
  }

  function buildTreemapData(sampleLabel, level, sourceKey = "otu") {
    // Use the latest round's cumulative assignments.
    // row.sample is the raw label from the BLAST report; normalizeBase() strips
    // replicate/marker suffixes so it matches sampleLabel (= sample_metrics.label).
    const lastRound = rounds.length > 0 ? rounds[rounds.length - 1] : null;
    const lvlRows = get(lastRound, [sourceKey, "assignments_by_level", level], []);
    if (!Array.isArray(lvlRows)) return [];
    const byTaxon = new Map();
    lvlRows.forEach((row) => {
      if (!row || assignmentGroupLabel(row) !== sampleLabel) return;
      const taxon = row.taxon || row[level] || "";
      const family = row.family || "";
      const reads = num(row.reads_total);
      if (!taxon) return;
      if (!byTaxon.has(taxon)) byTaxon.set(taxon, { taxon, family, reads_total: 0 });
      byTaxon.get(taxon).reads_total += reads;
    });
    return Array.from(byTaxon.values()).filter((d) => d.reads_total > 0)
      .sort((a, b) => b.reads_total - a.reads_total);
  }

  function drawTreemap(container, items) {
    clearNode(container);
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "treemap-empty";
      empty.textContent = "No assignments.";
      container.appendChild(empty);
      return;
    }
    const total = items.reduce((s, d) => s + d.reads_total, 0);
    if (total <= 0) return;
    const W = 1000, H = 200;
    const tiles = tmLayout(items, 0, 0, W, H, total);
    tiles.forEach((tile) => {
      const div = document.createElement("div");
      div.className = "treemap-tile";
      div.style.left = `${(tile.x / W) * 100}%`;
      div.style.top = `${(tile.y / H) * 100}%`;
      div.style.width = `${(tile.w / W) * 100}%`;
      div.style.height = `${(tile.h / H) * 100}%`;
      div.style.background = tmFamilyColor(tile.family);
      const label = document.createElement("div");
      label.className = "treemap-tile-label";
      label.textContent = tile.taxon;
      div.appendChild(label);
      div.title = `${tile.taxon} (${tile.family || "unknown family"}): ${tile.reads_total} reads`;
      container.appendChild(div);
    });
  }

  function renderSampleTreemapCard(panel, sampleLabel, sampleId, sourceKey, titleText) {
    const levels = ["species", "genus", "family"];
    const data = {};
    levels.forEach((lvl) => { data[lvl] = buildTreemapData(sampleLabel, lvl, sourceKey); });
    const hasData = levels.some((lvl) => data[lvl].length > 0);

    const card = document.createElement("div");
    card.className = "chart-card";
    const header = document.createElement("div");
    header.className = "chart-header";
    const title = document.createElement("h4");
    title.className = "sample-figures-title";
    title.textContent = titleText;
    header.appendChild(title);
    const downloads = document.createElement("div");
    downloads.className = "download-links";
    header.appendChild(downloads);
    card.appendChild(header);

    if (!hasData) {
      const empty = document.createElement("p");
      empty.className = "treemap-empty";
      empty.textContent = "No taxonomy assignments available.";
      card.appendChild(empty);
      panel.appendChild(card);
      return;
    }

    const tabBar = document.createElement("div");
    tabBar.className = "treemap-tabs";
    const container = document.createElement("div");
    container.className = "treemap-container";
    let activeLevel = levels.find((lvl) => data[lvl].length > 0) || "species";
    function refreshDownloads() {
      clearNode(downloads);
      const chartId = `sample_${sampleId}_${sourceKey}_treemap_${activeLevel}`;
      const pdfLink = makeAssetLink("Download PDF", getChartPdfPath(chartId, 0));
      if (pdfLink) downloads.appendChild(pdfLink);
    }
    const tabBtns = levels.map((lvl) => {
      const btn = document.createElement("button");
      btn.className = "treemap-tab" + (lvl === activeLevel ? " active" : "");
      btn.textContent = lvl.charAt(0).toUpperCase() + lvl.slice(1);
      btn.addEventListener("click", () => {
        activeLevel = lvl;
        tabBtns.forEach((b, i) => b.classList.toggle("active", levels[i] === lvl));
        drawTreemap(container, data[lvl]);
        refreshDownloads();
      });
      tabBar.appendChild(btn);
      return btn;
    });

    card.appendChild(tabBar);
    card.appendChild(container);
    refreshDownloads();
    drawTreemap(container, data[activeLevel]);
    panel.appendChild(card);
  }

  function renderSampleTreemaps(panel, sampleLabel, sampleId) {
    renderSampleTreemapCard(panel, sampleLabel, sampleId, "otu", "Taxonomic Treemaps (OTUs)");
    renderSampleTreemapCard(panel, sampleLabel, sampleId, "consensus", "Taxonomic Treemaps (Consensus)");
  }

  function assignmentSunburstHighlightSpec(sourceKey) {
    if (sourceKey === "otu") {
      return { field: "frozen_otu_count", label: "frozen OTUs", tone: "frozen" };
    }
    if (sourceKey === "consensus") {
      return { field: "consolidated_consensus_count", label: "consolidated consensus sequences", tone: "consolidated" };
    }
    return { field: "", label: "", tone: "" };
  }

  function buildAssignmentTaxonomyTree(assignments, marker, includeRow, highlightSpec) {
    const markerName = canonicalMarkerToken(marker);
    const highlightField = highlightSpec && highlightSpec.field ? highlightSpec.field : "";
    const highlightLabel = highlightSpec && highlightSpec.label ? highlightSpec.label : "";
    const highlightTone = highlightSpec && highlightSpec.tone ? highlightSpec.tone : "";
    if (!assignments || typeof assignments !== "object") {
      return { name: markerName, value: 0, specialValue: 0, highlight: false, highlightLabel, highlightTone, children: [] };
    }

    function keyFor(parts) {
      return parts.join("\u0001");
    }

    function addToMap(map, key, reads) {
      map.set(key, num(map.get(key)) + reads);
    }

    function ensureFamily(rootNode, family) {
      if (!rootNode.children.has(family)) {
        rootNode.children.set(family, { name: family, leafValue: 0, leafSpecialValue: 0, children: new Map() });
      }
      return rootNode.children.get(family);
    }

    function ensureGenus(familyNode, genus) {
      if (!familyNode.children.has(genus)) {
        familyNode.children.set(genus, { name: genus, leafValue: 0, leafSpecialValue: 0, children: new Map() });
      }
      return familyNode.children.get(genus);
    }

    const root = { name: markerName, leafValue: 0, leafSpecialValue: 0, children: new Map() };
    const familyTotals = new Map();
    const genusTotals = new Map();
    const speciesTotals = new Map();
    const familySpecialTotals = new Map();
    const genusSpecialTotals = new Map();
    const speciesSpecialTotals = new Map();

    ["family", "genus", "species"].forEach((level) => {
      const rows = assignments[level];
      if (!Array.isArray(rows)) return;
      rows.forEach((row) => {
        if (!row || (typeof includeRow === "function" && !includeRow(row))) return;
        if (canonicalMarkerToken(row.marker) !== markerName) return;
        const reads = num(row.reads_total);
        if (reads <= 0) return;
        const highlightCount = highlightField ? num(row[highlightField]) : 0;
        const family = (row.family || "").toString().trim() || "Unassigned family";
        const genus = (row.genus || "").toString().trim() || "Unassigned genus";
        const species = (row.species || "").toString().trim() || "Unassigned species";
        if (level === "family") {
          addToMap(familyTotals, family, reads);
          if (highlightField) addToMap(familySpecialTotals, family, highlightCount);
          return;
        }
        if (level === "genus") {
          addToMap(genusTotals, keyFor([family, genus]), reads);
          if (highlightField) addToMap(genusSpecialTotals, keyFor([family, genus]), highlightCount);
          return;
        }
        addToMap(speciesTotals, keyFor([family, genus, species]), reads);
        if (highlightField) addToMap(speciesSpecialTotals, keyFor([family, genus, species]), highlightCount);
      });
    });

    speciesTotals.forEach((reads, key) => {
      const [family, genus, species] = key.split("\u0001");
      const familyNode = ensureFamily(root, family);
      const genusNode = ensureGenus(familyNode, genus);
      if (!genusNode.children.has(species)) {
        genusNode.children.set(species, { name: species, leafValue: 0, leafSpecialValue: 0, children: new Map() });
      }
      const speciesNode = genusNode.children.get(species);
      speciesNode.leafValue += reads;
      speciesNode.leafSpecialValue += num(speciesSpecialTotals.get(key));
    });

    const coveredByGenus = new Map();
    const coveredByGenusSpecial = new Map();
    speciesTotals.forEach((reads, key) => {
      const [family, genus] = key.split("\u0001");
      addToMap(coveredByGenus, keyFor([family, genus]), reads);
      if (highlightField) addToMap(coveredByGenusSpecial, keyFor([family, genus]), num(speciesSpecialTotals.get(key)));
    });
    genusTotals.forEach((reads, key) => {
      const [family, genus] = key.split("\u0001");
      const familyNode = ensureFamily(root, family);
      const genusNode = ensureGenus(familyNode, genus);
      const remainder = Math.max(0, reads - num(coveredByGenus.get(key)));
      genusNode.leafValue += remainder;
      if (highlightField) {
        const specialRemainder = Math.max(0, num(genusSpecialTotals.get(key)) - num(coveredByGenusSpecial.get(key)));
        genusNode.leafSpecialValue += specialRemainder;
      }
    });

    const coveredByFamily = new Map();
    const coveredByFamilySpecial = new Map();
    genusTotals.forEach((reads, key) => {
      const [family] = key.split("\u0001");
      addToMap(coveredByFamily, family, reads);
      if (highlightField) addToMap(coveredByFamilySpecial, family, num(genusSpecialTotals.get(key)));
    });
    familyTotals.forEach((reads, family) => {
      const familyNode = ensureFamily(root, family);
      const remainder = Math.max(0, reads - num(coveredByFamily.get(family)));
      familyNode.leafValue += remainder;
      if (highlightField) {
        const specialRemainder = Math.max(0, num(familySpecialTotals.get(family)) - num(coveredByFamilySpecial.get(family)));
        familyNode.leafSpecialValue += specialRemainder;
      }
    });

    function finalize(node, depth) {
      const children = Array.from(node.children.values()).map((child) => finalize(child, depth + 1));
      children.sort((a, b) => b.value - a.value || a.name.localeCompare(b.name));
      const value = num(node.leafValue) + children.reduce((sum, child) => sum + child.value, 0);
      const specialValue = num(node.leafSpecialValue) + children.reduce((sum, child) => sum + num(child.specialValue), 0);
      return {
        name: node.name,
        depth,
        value,
        specialValue,
        highlight: specialValue > 0,
        children,
      };
    }

    const finalTree = finalize(root, 0);
    finalTree.highlightLabel = highlightLabel;
    finalTree.highlightTone = highlightTone;
    return finalTree;
  }

  function buildSampleTaxonomyTree(sampleLabel, sourceKey, marker, includeRow) {
    const lastRound = rounds.length > 0 ? rounds[rounds.length - 1] : null;
    const assignments = get(lastRound, [sourceKey, "assignments_by_level"], null);
    const expectedLabel = (sampleLabel || "").toString();
    return buildAssignmentTaxonomyTree(
      assignments,
      marker,
      (row) => assignmentGroupLabel(row) === expectedLabel && (typeof includeRow !== "function" || includeRow(row)),
      assignmentSunburstHighlightSpec(sourceKey),
    );
  }

  function buildRunTaxonomyTree(round, sourceKey, marker, includeRow) {
    const assignments = get(round, [sourceKey, "assignments_by_level"], null);
    return buildAssignmentTaxonomyTree(
      assignments,
      marker,
      typeof includeRow === "function" ? includeRow : () => true,
      assignmentSunburstHighlightSpec(sourceKey),
    );
  }

  function polarToCartesian(cx, cy, r, angleDeg) {
    const angleRad = (angleDeg - 90) * Math.PI / 180;
    return {
      x: cx + r * Math.cos(angleRad),
      y: cy + r * Math.sin(angleRad),
    };
  }

  function annularSectorPath(cx, cy, innerR, outerR, startAngle, endAngle) {
    const span = Math.max(0, endAngle - startAngle);
    if (span >= 359.999) {
      const midAngle = startAngle + 180;
      const outerStart = polarToCartesian(cx, cy, outerR, startAngle);
      const outerMid = polarToCartesian(cx, cy, outerR, midAngle);
      const innerStart = polarToCartesian(cx, cy, innerR, startAngle);
      const innerMid = polarToCartesian(cx, cy, innerR, midAngle);
      return [
        `M ${outerStart.x} ${outerStart.y}`,
        `A ${outerR} ${outerR} 0 1 0 ${outerMid.x} ${outerMid.y}`,
        `A ${outerR} ${outerR} 0 1 0 ${outerStart.x} ${outerStart.y}`,
        `L ${innerStart.x} ${innerStart.y}`,
        `A ${innerR} ${innerR} 0 1 1 ${innerMid.x} ${innerMid.y}`,
        `A ${innerR} ${innerR} 0 1 1 ${innerStart.x} ${innerStart.y}`,
        "Z",
      ].join(" ");
    }
    const startOuter = polarToCartesian(cx, cy, outerR, endAngle);
    const endOuter = polarToCartesian(cx, cy, outerR, startAngle);
    const startInner = polarToCartesian(cx, cy, innerR, startAngle);
    const endInner = polarToCartesian(cx, cy, innerR, endAngle);
    const largeArc = span > 180 ? 1 : 0;
    return [
      `M ${startOuter.x} ${startOuter.y}`,
      `A ${outerR} ${outerR} 0 ${largeArc} 0 ${endOuter.x} ${endOuter.y}`,
      `L ${startInner.x} ${startInner.y}`,
      `A ${innerR} ${innerR} 0 ${largeArc} 1 ${endInner.x} ${endInner.y}`,
      "Z",
    ].join(" ");
  }

  function getSunburstNodeByPath(tree, path) {
    let node = tree;
    if (!node || !Array.isArray(path)) return node;
    for (let i = 0; i < path.length; i += 1) {
      if (!node || !Array.isArray(node.children)) return null;
      node = node.children.find((child) => child && child.name === path[i]) || null;
    }
    return node;
  }

  function getSunburstPathNodes(tree, path) {
    const nodes = [tree];
    let node = tree;
    if (!node || !Array.isArray(path)) return nodes.filter(Boolean);
    for (let i = 0; i < path.length; i += 1) {
      if (!node || !Array.isArray(node.children)) break;
      node = node.children.find((child) => child && child.name === path[i]) || null;
      if (!node) break;
      nodes.push(node);
    }
    return nodes.filter(Boolean);
  }

  function pathsEqual(a, b) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    for (let i = 0; i < a.length; i += 1) {
      if (a[i] !== b[i]) return false;
    }
    return true;
  }

  function isPathPrefix(prefix, path) {
    if (!Array.isArray(prefix) || !Array.isArray(path) || prefix.length > path.length) return false;
    for (let i = 0; i < prefix.length; i += 1) {
      if (prefix[i] !== path[i]) return false;
    }
    return true;
  }

  function collectSunburstDescendants(node, depth = 0, acc = []) {
    if (!node || !Array.isArray(node.children)) return acc;
    node.children.forEach((child) => {
      acc.push({ name: child.name || "", depth, value: num(child.value) });
      collectSunburstDescendants(child, depth + 1, acc);
    });
    return acc;
  }

  function treeHasHighlight(node) {
    if (!node) return false;
    if (node.highlight) return true;
    if (!Array.isArray(node.children)) return false;
    return node.children.some((child) => treeHasHighlight(child));
  }

  function renderSunburstSvg(tree) {
    const wrap = document.createElement("div");
    wrap.className = "sample-sunburst-wrap";
    if (!tree || num(tree.value) <= 0) {
      const empty = document.createElement("p");
      empty.className = "treemap-empty";
      empty.textContent = "No taxonomy assignments available.";
      wrap.appendChild(empty);
      return wrap;
    }

    const controls = document.createElement("div");
    controls.className = "sample-sunburst-controls";
    wrap.appendChild(controls);

    const hint = document.createElement("p");
    hint.className = "sample-sunburst-hint";
    hint.textContent = "Click a segment to inspect its lineage.";
    wrap.appendChild(hint);

    const details = document.createElement("div");
    details.className = "sample-sunburst-details";
    wrap.appendChild(details);

    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", "0 0 320 320");
    svg.setAttribute("class", "sample-sunburst-svg");
    wrap.appendChild(svg);

    const centerX = 160;
    const centerY = 160;
    const innerRadius = 30;
    const ringWidth = 34;
    const focusPath = [];

    function nodeColor(depth, familyName) {
      if (!tmFamilyColorMap.has(familyName)) tmFamilyColor(familyName);
      const base = tmFamilyColorMap.get(familyName) || "#9aa39b";
      if (depth <= 1) return base;
      return base;
    }

    function walk(node, startAngle, endAngle, depth, familyName, nextPath) {
      if (!Array.isArray(node.children) || !node.children.length || depth > 3) return;
      const span = endAngle - startAngle;
      let cursor = startAngle;
      node.children.forEach((child) => {
        const childSpan = node.value > 0 ? span * (child.value / node.value) : 0;
        const nextAngle = cursor + childSpan;
        if (childSpan <= 0.001) {
          cursor = nextAngle;
          return;
        }
        const family = depth === 0 ? child.name : familyName;
        const innerR = innerRadius + (depth * ringWidth);
        const outerR = innerR + ringWidth;
        const path = document.createElementNS(svgNS, "path");
        const childPath = nextPath.concat(child.name);
        path.setAttribute("d", annularSectorPath(centerX, centerY, innerR, outerR, cursor, nextAngle));
        path.setAttribute("fill", nodeColor(depth + 1, family));
        path.setAttribute("stroke", "#ffffff");
        path.setAttribute("stroke-width", "1");
        path.setAttribute("fill-opacity", String(Math.max(0.42, 0.95 - depth * 0.18)));
        path.classList.add("sample-sunburst-clickable");
        path.addEventListener("click", () => {
          if (pathsEqual(focusPath, childPath)) {
            focusPath.splice(0, focusPath.length);
          } else {
            focusPath.splice(0, focusPath.length, ...childPath);
          }
          render();
        });
        if (focusPath.length) {
          if (pathsEqual(focusPath, childPath)) {
            path.setAttribute("stroke", "#1b2417");
            path.setAttribute("stroke-width", "2.5");
            path.setAttribute("fill-opacity", "1");
          } else if (isPathPrefix(childPath, focusPath) || isPathPrefix(focusPath, childPath)) {
            path.setAttribute("stroke", "#365448");
            path.setAttribute("stroke-width", "1.8");
            path.setAttribute("fill-opacity", String(Math.max(0.55, 0.98 - depth * 0.14)));
          } else {
            path.setAttribute("fill-opacity", String(Math.max(0.18, 0.52 - depth * 0.08)));
          }
        }
        const title = document.createElementNS(svgNS, "title");
        title.textContent = `${child.name}: ${Math.round(num(child.value))}`;
        path.appendChild(title);
        svg.appendChild(path);
        walk(child, cursor, nextAngle, depth + 1, family, childPath);
        cursor = nextAngle;
      });
    }

    function renderBreadcrumbs(pathNodes) {
      clearNode(controls);
      const crumbs = document.createElement("div");
      crumbs.className = "sample-sunburst-breadcrumbs";
      pathNodes.forEach((node, idx) => {
        const isLast = idx === pathNodes.length - 1;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "sample-sunburst-crumb" + (isLast ? " active" : "");
        btn.textContent = node.name || "";
        if (!isLast) {
          btn.addEventListener("click", () => {
            focusPath.splice(0, focusPath.length, ...pathNodes.slice(1, idx + 1).map((entry) => entry.name));
            render();
          });
        } else {
          btn.disabled = true;
        }
        crumbs.appendChild(btn);
      });
      controls.appendChild(crumbs);
      if (focusPath.length) {
        const resetBtn = document.createElement("button");
        resetBtn.type = "button";
        resetBtn.className = "sample-sunburst-reset";
        resetBtn.textContent = "Reset";
        resetBtn.addEventListener("click", () => {
          focusPath.splice(0, focusPath.length);
          render();
        });
        controls.appendChild(resetBtn);
      }
    }

    function renderDetails(pathNodes, focusNode) {
      clearNode(details);
      if (!focusPath.length || !focusNode) return;

      function addSection(titleText, items, mode) {
        if (!Array.isArray(items) || !items.length) return;
        const section = document.createElement("div");
        section.className = "sample-sunburst-section";
        const titleEl = document.createElement("div");
        titleEl.className = "sample-sunburst-section-title";
        titleEl.textContent = titleText;
        section.appendChild(titleEl);
        const list = document.createElement("div");
        list.className = "sample-sunburst-chip-list";
        items.forEach((item) => {
          const chip = document.createElement("span");
          chip.className = "sample-sunburst-chip";
          if (mode === "selected") chip.classList.add("selected");
          if (mode === "ancestor") chip.classList.add("ancestor");
          if (mode === "descendant") chip.classList.add("descendant");
          if (mode === "selected") {
            chip.textContent = `${item.name} (${Math.round(num(item.value))})`;
          } else if (mode === "descendant") {
            chip.textContent = `${item.name} (${Math.round(num(item.value))})`;
          } else {
            chip.textContent = item.name;
          }
          list.appendChild(chip);
        });
        section.appendChild(list);
        details.appendChild(section);
      }

      const ancestors = pathNodes.slice(0, -1).map((node) => ({ name: node.name || "", value: node.value }));
      const selected = [{ name: focusNode.name || "", value: focusNode.value }];
      const descendants = collectSunburstDescendants(focusNode)
        .filter((item) => item && item.name)
        .sort((a, b) => a.depth - b.depth || b.value - a.value || a.name.localeCompare(b.name));

      addSection("Parents", ancestors, "ancestor");
      addSection("Selected", selected, "selected");
      addSection("Descendants", descendants, "descendant");
    }

    function render() {
      clearNode(svg);
      const pathNodes = getSunburstPathNodes(tree, focusPath);
      const focusNode = getSunburstNodeByPath(tree, focusPath) || tree;
      renderBreadcrumbs(pathNodes);
      renderDetails(pathNodes, focusNode);

      const centerCircle = document.createElementNS(svgNS, "circle");
      centerCircle.setAttribute("cx", String(centerX));
      centerCircle.setAttribute("cy", String(centerY));
      centerCircle.setAttribute("r", String(innerRadius));
      centerCircle.setAttribute("fill", markerColorsFromData(rounds).get(canonicalMarkerToken(tree.name)) || "#2c6e49");
      if (focusPath.length) {
        centerCircle.classList.add("sample-sunburst-clickable");
        centerCircle.addEventListener("click", () => {
          focusPath.splice(0, focusPath.length);
          render();
        });
      }
      svg.appendChild(centerCircle);

      const centerTitle = document.createElementNS(svgNS, "text");
      centerTitle.setAttribute("x", String(centerX));
      centerTitle.setAttribute("y", "154");
      centerTitle.setAttribute("text-anchor", "middle");
      centerTitle.setAttribute("class", "sample-sunburst-center");
      centerTitle.textContent = tree.name || "";
      svg.appendChild(centerTitle);

      const centerValue = document.createElementNS(svgNS, "text");
      centerValue.setAttribute("x", String(centerX));
      centerValue.setAttribute("y", "172");
      centerValue.setAttribute("text-anchor", "middle");
      centerValue.setAttribute("class", "sample-sunburst-center-value");
      centerValue.textContent = String(Math.round(num(tree.value)));
      svg.appendChild(centerValue);

      walk(tree, 0, 360, 0, "", []);
      hint.textContent = focusPath.length
        ? "Click another segment to change the selection, or the center to clear it."
        : "Click a segment to inspect its lineage.";
    }

    render();
    return wrap;
  }

  function renderStaticSunburstSvg(tree) {
    const wrap = document.createElement("div");
    wrap.className = "sample-sunburst-wrap run-static-sunburst-wrap";
    if (!tree || num(tree.value) <= 0) {
      const empty = document.createElement("p");
      empty.className = "treemap-empty";
      empty.textContent = "No taxonomy assignments available.";
      wrap.appendChild(empty);
      return wrap;
    }

    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", "0 0 980 680");
    svg.setAttribute("class", "sample-sunburst-svg run-static-sunburst-svg");
    svg.style.overflow = "visible";
    wrap.appendChild(svg);

    const centerX = 490;
    const centerY = 340;
    const innerRadius = 52;
    const ringWidth = 48;
    const outerRadius = innerRadius + (3 * ringWidth);
    const palette = [
      "#4c956c", "#2c6e49", "#1d5c3a", "#1d4e89", "#3a7fad", "#6baed6",
      "#8b4513", "#9B5090", "#e07b39", "#d4a017", "#9a7d0a", "#6b4226",
    ];
    const labelCandidates = [];

    function colorForFamily(name) {
      const key = (name || "Other").toString();
      let hash = 0;
      for (let i = 0; i < key.length; i += 1) {
        hash = ((hash * 31) + key.charCodeAt(i)) >>> 0;
      }
      return palette[hash % palette.length];
    }

    function nodeColor(depth, familyName) {
      const base = colorForFamily(familyName);
      return depth <= 1 ? base : base;
    }

    function walk(node, startAngle, endAngle, depth, familyName) {
      if (!Array.isArray(node.children) || !node.children.length || depth > 3) return;
      const span = endAngle - startAngle;
      let cursor = startAngle;
      node.children.forEach((child) => {
        const childSpan = node.value > 0 ? span * (child.value / node.value) : 0;
        const nextAngle = cursor + childSpan;
        if (childSpan <= 0.001) {
          cursor = nextAngle;
          return;
        }
        const family = depth === 0 ? child.name : familyName;
        const innerR = innerRadius + (depth * ringWidth);
        const outerR = innerR + ringWidth;
        const path = document.createElementNS(svgNS, "path");
        path.setAttribute("d", annularSectorPath(centerX, centerY, innerR, outerR, cursor, nextAngle));
        path.setAttribute("fill", nodeColor(depth + 1, family));
        path.setAttribute("stroke", "#ffffff");
        path.setAttribute("stroke-width", "1");
        path.setAttribute("fill-opacity", String(Math.max(0.42, 0.95 - depth * 0.18)));
        const title = document.createElementNS(svgNS, "title");
        title.textContent = `${child.name}: ${Math.round(num(child.value))}`;
        path.appendChild(title);
        svg.appendChild(path);
        if (!Array.isArray(child.children) || !child.children.length) {
          labelCandidates.push({
            name: child.name || "",
            value: num(child.value),
            highlight: !!child.highlight,
            startAngle: cursor,
            endAngle: nextAngle,
            midAngle: cursor + (childSpan / 2),
            innerR,
            outerR,
          });
        }
        walk(child, cursor, nextAngle, depth + 1, family);
        cursor = nextAngle;
      });
    }

    function drawTerminalLabels() {
      const minAngleSpan = 1.2;
      const minGap = 34;
      const topLimit = 42;
      const bottomLimit = 638;
      const leftX = centerX - outerRadius - 92;
      const rightX = centerX + outerRadius + 92;
      const baseViewWidth = 980;
      const baseFontSize = 24;
      const baseStrokeWidth = 6;
      const labelEls = [];
      const bounds = {
        minX: centerX - outerRadius - 12,
        maxX: centerX + outerRadius + 12,
        minY: centerY - outerRadius - 12,
        maxY: centerY + outerRadius + 12,
      };

      function place(sideItems, side) {
        const sorted = sideItems.slice().sort((a, b) => a.targetY - b.targetY);
        let currentY = topLimit;
        sorted.forEach((item) => {
          item.finalY = Math.max(item.targetY, currentY);
          currentY = item.finalY + minGap;
        });
        currentY = bottomLimit;
        for (let idx = sorted.length - 1; idx >= 0; idx -= 1) {
          const item = sorted[idx];
          item.finalY = Math.min(item.finalY, currentY);
          currentY = item.finalY - minGap;
        }
        sorted.forEach((item) => {
          const leaderStart = polarToCartesian(centerX, centerY, (item.innerR + item.outerR) / 2, item.midAngle);
          const elbow = polarToCartesian(centerX, centerY, item.outerR + 12, item.midAngle);
          const textX = side === "right" ? rightX : leftX;
          const lineEndX = side === "right" ? textX - 6 : textX + 6;
          const textWidth = Math.max(80, item.name.length * 16);

          const polyline = document.createElementNS(svgNS, "polyline");
          polyline.setAttribute("points", `${leaderStart.x},${leaderStart.y} ${elbow.x},${elbow.y} ${lineEndX},${item.finalY}`);
          polyline.setAttribute("class", "run-sunburst-leader");
          svg.appendChild(polyline);

          const text = document.createElementNS(svgNS, "text");
          text.setAttribute("x", String(textX));
          text.setAttribute("y", String(item.finalY + 3));
          text.setAttribute("text-anchor", side === "right" ? "start" : "end");
          text.setAttribute("class", "run-sunburst-label");
          if (item.highlight) {
            text.classList.add("run-sunburst-label-highlight");
            if (tree.highlightTone === "frozen") text.classList.add("run-sunburst-label-frozen");
            else if (tree.highlightTone === "consolidated") text.classList.add("run-sunburst-label-consolidated");
          }
          text.textContent = item.name;
          const title = document.createElementNS(svgNS, "title");
          title.textContent = item.highlight && tree.highlightLabel
            ? `${item.name}: ${Math.round(item.value)} (includes ${tree.highlightLabel})`
            : `${item.name}: ${Math.round(item.value)}`;
          text.appendChild(title);
          svg.appendChild(text);
          labelEls.push(text);
          item._polyline = polyline;
          item._text = text;

          bounds.minX = Math.min(bounds.minX, leaderStart.x, elbow.x, side === "right" ? textX : textX - textWidth);
          bounds.maxX = Math.max(bounds.maxX, leaderStart.x, elbow.x, side === "right" ? textX + textWidth : textX);
          bounds.minY = Math.min(bounds.minY, leaderStart.y, elbow.y, item.finalY - baseFontSize);
          bounds.maxY = Math.max(bounds.maxY, leaderStart.y, elbow.y, item.finalY + 8);
        });

        return bounds;
      }

      const rankedCandidates = labelCandidates
        .filter((item) => item && item.name)
        .sort((a, b) => b.value - a.value || (b.endAngle - b.startAngle) - (a.endAngle - a.startAngle) || a.name.localeCompare(b.name));
      const terminalLabels = rankedCandidates
        .filter((item) => (item.endAngle - item.startAngle) >= minAngleSpan || item.value > 0)
        .map((item) => {
          const point = polarToCartesian(centerX, centerY, item.outerR + 14, item.midAngle);
          return {
            ...item,
            targetY: point.y,
            side: point.x >= centerX ? "right" : "left",
          };
        });

      place(terminalLabels.filter((item) => item.side === "left"), "left");
      place(terminalLabels.filter((item) => item.side === "right"), "right");
      const padding = 18;
      const width = Math.ceil((bounds.maxX - bounds.minX) + (padding * 2));
      const height = Math.ceil((bounds.maxY - bounds.minY) + (padding * 2));
      const scale = width / baseViewWidth;
      let fontSize = Math.max(24, Math.round(baseFontSize * scale * 10) / 10);
      ["left", "right"].forEach((side) => {
        const sideLabels = terminalLabels
          .filter((item) => item.side === side)
          .sort((a, b) => a.finalY - b.finalY);
        if (sideLabels.length < 2) return;
        let minDelta = Infinity;
        for (let idx = 1; idx < sideLabels.length; idx += 1) {
          minDelta = Math.min(minDelta, sideLabels[idx].finalY - sideLabels[idx - 1].finalY);
        }
        if (Number.isFinite(minDelta) && minDelta > 0) {
          fontSize = Math.min(fontSize, Math.max(8, Math.round((minDelta - 6) * 10) / 10));
        }
      });
      const strokeWidth = Math.max(2, Math.round((fontSize / 4.5) * 10) / 10);
      svg.setAttribute(
        "viewBox",
        `${Math.floor(bounds.minX - padding)} ${Math.floor(bounds.minY - padding)} ${width} ${height}`,
      );
      svg.setAttribute("width", String(width));
      svg.setAttribute("height", String(height));
      wrap.style.width = "";
      labelEls.forEach((el) => {
        el.style.fontSize = `${fontSize}px`;
        el.style.strokeWidth = `${strokeWidth}px`;
      });
    }

    const centerCircle = document.createElementNS(svgNS, "circle");
    centerCircle.setAttribute("cx", String(centerX));
    centerCircle.setAttribute("cy", String(centerY));
    centerCircle.setAttribute("r", String(innerRadius));
    const centerColors = markerColorsFromData(rounds);
    centerCircle.setAttribute("fill", centerColors.get(canonicalMarkerToken(tree.name)) || "#2c6e49");
    svg.appendChild(centerCircle);

    const centerTitle = document.createElementNS(svgNS, "text");
    centerTitle.setAttribute("x", String(centerX));
    centerTitle.setAttribute("y", String(centerY - 6));
    centerTitle.setAttribute("text-anchor", "middle");
    centerTitle.setAttribute("class", "sample-sunburst-center");
    centerTitle.textContent = tree.name || "";
    svg.appendChild(centerTitle);

    const centerValue = document.createElementNS(svgNS, "text");
    centerValue.setAttribute("x", String(centerX));
    centerValue.setAttribute("y", String(centerY + 14));
    centerValue.setAttribute("text-anchor", "middle");
    centerValue.setAttribute("class", "sample-sunburst-center-value");
    centerValue.textContent = String(Math.round(num(tree.value)));
    svg.appendChild(centerValue);

    walk(tree, 0, 360, 0, "");
    drawTerminalLabels();
    return wrap;
  }

  function renderSampleSunbursts(panel, sampleLabel, sampleFigures) {
    const specs = flatMapCompat(markerOrderFromData(rounds), (marker) => {
      const slug = markerSlug(marker);
      return [
        { id: `sample_otu_${slug}_sunburst`, title: `OTU Sunburst (${marker})`, sourceKey: "otu", marker },
        { id: `sample_consensus_${slug}_sunburst`, title: `Consensus Sunburst (${marker})`, sourceKey: "consensus", marker },
      ];
    });
    const figById = new Map((Array.isArray(sampleFigures) ? sampleFigures : []).filter(Boolean).map((fig) => [fig.id, fig]));
    const title = document.createElement("h4");
    title.className = "sample-figures-title";
    title.textContent = "Taxonomic Sunbursts";
    panel.appendChild(title);

    const specTrees = specs.map((spec) => ({
      ...spec,
      tree: buildSampleTaxonomyTree(
        sampleLabel,
        spec.sourceKey,
        spec.marker,
        (row) => spec.sourceKey !== "otu" || isSupportedOtuAssignment(row),
      ),
    }));
    const validSpecs = specTrees.filter((spec) => spec.tree && num(spec.tree.value) > 0);
    if (!validSpecs.length) {
      const empty = document.createElement("p");
      empty.className = "treemap-empty";
      empty.textContent = "No taxonomy assignments available.";
      panel.appendChild(empty);
      return;
    }

    const hint = document.createElement("p");
    hint.className = "sample-sunburst-hint";
    hint.textContent = validSpecs.some((spec) => treeHasHighlight(spec.tree))
      ? "Bold labels in OTU and consensus sunbursts mark taxa that include frozen OTUs or consolidated consensus sequences."
      : "No frozen OTU or consolidated consensus assignments are present in these OTU and consensus sunbursts.";
    panel.appendChild(hint);

    const grid = document.createElement("div");
    grid.className = "sample-sunburst-grid";
    validSpecs.forEach((spec) => {
      const card = document.createElement("div");
      card.className = "chart-card run-sunburst-card";
      const header = document.createElement("div");
      header.className = "chart-header";
      const h = document.createElement("h4");
      h.className = "sample-figures-title";
      h.textContent = spec.title;
      header.appendChild(h);
      const downloads = document.createElement("div");
      downloads.className = "download-links";
      const fig = figById.get(spec.id);
      const pdfLink = makeAssetLink("Download PDF", fig && fig.pdf_path ? fig.pdf_path : "");
      if (pdfLink) downloads.appendChild(pdfLink);
      if (downloads.childNodes.length) header.appendChild(downloads);
      card.appendChild(header);
      card.appendChild(renderStaticSunburstSvg(spec.tree));
      grid.appendChild(card);
    });
    panel.appendChild(grid);
  }

  function renderRunSunburstCards(round, mountNode) {
    if (!mountNode || !round) return;
    const specs = flatMapCompat(markerOrderFromData([round]), (marker) => {
      const suffix = marker === "COI" || marker === "ITS2" ? marker : markerSlug(marker);
      return [
        { id: `run_otu_sunburst_${suffix}`, title: `OTUs (${marker})`, sourceKey: "otu", marker },
        { id: `run_consensus_sunburst_${suffix}`, title: `Consensus (${marker})`, sourceKey: "consensus", marker },
      ];
    }).map((spec) => ({
      ...spec,
      tree: buildRunTaxonomyTree(
        round,
        spec.sourceKey,
        spec.marker,
        (row) => spec.sourceKey !== "otu" || isSupportedOtuAssignment(row),
      ),
    }));
    // Add frozen OTU and consolidated consensus sunbursts to the same grid
    flatMapCompat(markerOrderFromData([round]), (marker) => {
      const suffix = marker === "COI" || marker === "ITS2" ? marker : markerSlug(marker);
      return [
        { id: `run_frozen_otu_sunburst_${suffix}`, title: `Frozen OTUs (${marker})`, sourceKey: "otu", marker, countField: "frozen_otu_count" },
        { id: `run_consolidated_consensus_sunburst_${suffix}`, title: `Consolidated Consensus (${marker})`, sourceKey: "consensus", marker, countField: "consolidated_consensus_count" },
      ];
    }).forEach((spec) => {
      specs.push({ ...spec, tree: buildFilteredAssignmentTree(round, spec.sourceKey, spec.countField, spec.marker, null) });
    });
    const validSpecs = specs.filter((spec) => spec.tree && num(spec.tree.value) > 0);
    if (!validSpecs.length) return;

    const section = document.createElement("section");
    section.className = "run-taxa-section";
    const title = document.createElement("h4");
    title.className = "sample-figures-title";
    title.textContent = "Taxons Identified in the Run";
    section.appendChild(title);
    const hint = document.createElement("p");
    hint.className = "sample-sunburst-hint";
    hint.textContent = validSpecs.some((spec) => !spec.countField && treeHasHighlight(spec.tree))
      ? "Bold labels in OTU and consensus sunbursts mark taxa that include frozen OTUs or consolidated consensus sequences."
      : "No frozen OTU or consolidated consensus assignments are present in the OTU and consensus sunbursts for this run.";
    section.appendChild(hint);

    const grid = document.createElement("div");
    grid.className = "sample-sunburst-grid run-sunburst-grid";
    validSpecs.forEach((spec) => {
      const subcard = document.createElement("div");
      subcard.className = "chart-card run-sunburst-card";
      const subheader = document.createElement("div");
      subheader.className = "chart-header";
      const sh = document.createElement("h4");
      sh.className = "sample-figures-title";
      sh.textContent = spec.title;
      subheader.appendChild(sh);
      const _pdfPath = getChartPdfPath(spec.id || "", 0);
      const _pdfLink = makeAssetLink("Download PDF", _pdfPath);
      if (_pdfLink) subheader.appendChild(_pdfLink);
      subcard.appendChild(subheader);
      subcard.appendChild(renderStaticSunburstSvg(spec.tree));
      grid.appendChild(subcard);
    });
    section.appendChild(grid);
    mountNode.appendChild(section);
  }

  // ── Frozen OTU / Consolidated Consensus sections ─────────────────────────

  function buildFilteredAssignmentTree(round, sourceKey, countField, marker, sampleFilter) {
    const assignments = get(round, [sourceKey, "assignments_by_level"], null);
    return buildAssignmentTaxonomyTree(
      assignments,
      marker,
      (row) => {
        if (num(row[countField]) <= 0) return false;
        if (typeof sampleFilter === "function" && !sampleFilter(row)) return false;
        return true;
      },
      assignmentSunburstHighlightSpec(sourceKey),
    );
  }

  function getSampleRoundSnapshot(round, sampleId, sampleLabel) {
    const entries = getGroupedSampleMetricEntries(round, sampleLabel);
    return aggregateSampleMetricEntries(entries, sampleId, sampleLabel);
  }

  function countSampleAssignmentsByLevel(round, sampleLabel, sourceKey, includeRow) {
    const baseLabel = (sampleLabel || "").toString();
    const byLevel = { species: 0, genus: 0, family: 0 };
    ["species", "genus", "family"].forEach((level) => {
      const rows = get(round, [sourceKey, "assignments_by_level", level], []);
      const taxa = new Set();
      (Array.isArray(rows) ? rows : []).forEach((row) => {
        if (!row) return;
        if (assignmentGroupLabel(row) !== baseLabel) return;
        if (typeof includeRow === "function" && !includeRow(row)) return;
        const taxon = (row.taxon || row[level] || "").toString().trim();
        if (taxon) taxa.add(taxon);
      });
      byLevel[level] = taxa.size;
    });
    return byLevel;
  }

  function renderSampleEvolutionChart(title, rows, seriesDefs, options = {}) {
    const card = document.createElement("div");
    card.className = "chart-card sample-evolution-card";
    const header = document.createElement("div");
    header.className = "chart-header";
    const h = document.createElement("h4");
    h.className = "sample-figures-title";
    h.textContent = title;
    header.appendChild(h);
    card.appendChild(header);

    const defs = Array.isArray(seriesDefs) ? seriesDefs.filter(Boolean) : [];
    const dataRows = Array.isArray(rows) ? rows.filter(Boolean) : [];
    const activeDefs = defs.filter((def) =>
      dataRows.some((row) => num(row && row[def.key]) > 0),
    );
    if (!dataRows.length || !activeDefs.length) {
      const empty = document.createElement("p");
      empty.className = "otu-assign-empty";
      empty.textContent = options.emptyText || "No sample history available.";
      card.appendChild(empty);
      return card;
    }

    const downloads = document.createElement("div");
    downloads.className = "download-links";
    const columns = ["round", ...activeDefs.map((def) => def.label)];
    const tsvRows = dataRows.map((row) => [
      row.round_barcode || "",
      ...activeDefs.map((def) => row[def.key] != null ? row[def.key] : 0),
    ]);
    downloads.appendChild(makeDownloadLink(title, makeTsv(columns, tsvRows)));
    header.appendChild(downloads);

    if (activeDefs.length > 1) {
      const legend = document.createElement("div");
      legend.className = "chart-legend";
      activeDefs.forEach((def) => {
        const item = document.createElement("span");
        item.className = "chart-legend-item";
        const swatch = document.createElement("span");
        swatch.className = "chart-legend-swatch";
        swatch.style.background = def.color;
        const label = document.createElement("span");
        label.textContent = def.label;
        item.appendChild(swatch);
        item.appendChild(label);
        legend.appendChild(item);
      });
      card.appendChild(legend);
    }

    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", "0 0 620 260");
    svg.setAttribute("class", "sample-evolution-svg");

    const left = 52;
    const right = 18;
    const top = 18;
    const bottom = 56;
    const plotWidth = 620 - left - right;
    const plotHeight = 260 - top - bottom;
    const transformedMax = Math.max(
      1,
      ...flatMapCompat(activeDefs, (def) =>
        dataRows.map((row) => {
          const value = num(row && row[def.key]);
          return options.logScale ? Math.log10(Math.max(1, value)) : value;
        }),
      ),
    );
    const xFor = (idx) => {
      if (dataRows.length === 1) return left + plotWidth / 2;
      return left + (plotWidth * idx) / (dataRows.length - 1);
    };
    const yFor = (rawValue) => {
      const transformed = options.logScale ? Math.log10(Math.max(1, num(rawValue))) : num(rawValue);
      const frac = transformedMax > 0 ? transformed / transformedMax : 0;
      return top + plotHeight - (frac * plotHeight);
    };

    for (let i = 0; i <= 4; i += 1) {
      const y = top + (plotHeight * i) / 4;
      const line = document.createElementNS(svgNS, "line");
      line.setAttribute("x1", String(left));
      line.setAttribute("x2", String(left + plotWidth));
      line.setAttribute("y1", String(y));
      line.setAttribute("y2", String(y));
      line.setAttribute("class", "sample-evolution-gridline");
      svg.appendChild(line);
    }

    const axisX = document.createElementNS(svgNS, "line");
    axisX.setAttribute("x1", String(left));
    axisX.setAttribute("x2", String(left + plotWidth));
    axisX.setAttribute("y1", String(top + plotHeight));
    axisX.setAttribute("y2", String(top + plotHeight));
    axisX.setAttribute("class", "sample-evolution-axis");
    svg.appendChild(axisX);

    const axisY = document.createElementNS(svgNS, "line");
    axisY.setAttribute("x1", String(left));
    axisY.setAttribute("x2", String(left));
    axisY.setAttribute("y1", String(top));
    axisY.setAttribute("y2", String(top + plotHeight));
    axisY.setAttribute("class", "sample-evolution-axis");
    svg.appendChild(axisY);

    [0, transformedMax / 2, transformedMax].forEach((tickValue) => {
      const rawValue = options.logScale ? Math.round(10 ** tickValue) : Math.round(tickValue);
      const y = top + plotHeight - ((tickValue / transformedMax) * plotHeight);
      const label = document.createElementNS(svgNS, "text");
      label.setAttribute("x", String(left - 8));
      label.setAttribute("y", String(y + 4));
      label.setAttribute("text-anchor", "end");
      label.setAttribute("class", "sample-evolution-tick");
      label.textContent = String(rawValue);
      svg.appendChild(label);
    });

    dataRows.forEach((row, idx) => {
      const x = xFor(idx);
      const tick = document.createElementNS(svgNS, "text");
      tick.setAttribute("x", String(x));
      tick.setAttribute("y", String(top + plotHeight + 18));
      tick.setAttribute("text-anchor", "end");
      tick.setAttribute("transform", `rotate(-35 ${x} ${top + plotHeight + 18})`);
      tick.setAttribute("class", "sample-evolution-tick");
      tick.textContent = row.round_barcode || `R${idx + 1}`;
      svg.appendChild(tick);
    });

    activeDefs.forEach((def) => {
      const points = dataRows.map((row, idx) => `${xFor(idx)},${yFor(row[def.key])}`).join(" ");
      const polyline = document.createElementNS(svgNS, "polyline");
      polyline.setAttribute("points", points);
      polyline.setAttribute("fill", "none");
      polyline.setAttribute("stroke", def.color);
      polyline.setAttribute("stroke-width", "2.5");
      polyline.setAttribute("stroke-linejoin", "round");
      polyline.setAttribute("stroke-linecap", "round");
      svg.appendChild(polyline);
      dataRows.forEach((row, idx) => {
        const circle = document.createElementNS(svgNS, "circle");
        circle.setAttribute("cx", String(xFor(idx)));
        circle.setAttribute("cy", String(yFor(row[def.key])));
        circle.setAttribute("r", "3.5");
        circle.setAttribute("fill", def.color);
        const titleEl = document.createElementNS(svgNS, "title");
        titleEl.textContent = `${row.round_barcode || `R${idx + 1}`} - ${def.label}: ${num(row[def.key])}`;
        circle.appendChild(titleEl);
        svg.appendChild(circle);
      });
    });

    card.appendChild(svg);
    return card;
  }

  function renderSampleEvolutionSection(panel, sampleEntry) {
    const figures = (Array.isArray(sampleEntry.figures) ? sampleEntry.figures : []).filter((fig) => fig && new Set([
      "sample_reads_time_history",
      "sample_reads_cumulative_history",
      "sample_otu_tax_time_history",
      "sample_consensus_tax_time_history",
    ]).has(fig.id));
    if (!figures.length) return;
    const section = document.createElement("section");
    section.className = "sample-subsection";
    const title = document.createElement("h4");
    title.className = "sample-figures-title";
    title.textContent = `${groupEntityLabel()} run evolution`;
    section.appendChild(title);
    const wrap = document.createElement("div");
    wrap.className = "figures-grid sample-figures-grid";
    renderFigureGallery(wrap, figures, "No sample history figures available for this sample.");
    section.appendChild(wrap);
    panel.appendChild(section);
  }

  function renderCurrentSampleResults(panel, sampleEntry) {
    const section = document.createElement("section");
    section.className = "sample-subsection";
    const title = document.createElement("h4");
    title.className = "sample-figures-title";
    title.textContent = `Current ${groupEntityLabel()} Results`;
    section.appendChild(title);

    const current = sampleEntry.current || {};
    const totals = sampleEntry.totals || {};
    const cardsWrap = document.createElement("div");
    cardsWrap.className = "cards";
    [
      ["Demultiplexed Reads", num(totals.reads_demux)],
      ["BLAST-assigned Reads", num(totals.reads_blast_assigned)],
      ["Number of OTUs", num(current.otu_total != null ? current.otu_total : current.otu_active)],
      ["Number of Consensus", num(current.consensus_total != null ? current.consensus_total : current.consensus_emitted)],
    ].forEach(([label, value]) => {
      const div = document.createElement("div");
      div.className = "card";
      const labelNode = document.createElement("div");
      labelNode.className = "label";
      labelNode.textContent = label;
      const valueNode = document.createElement("div");
      valueNode.className = "value";
      valueNode.textContent = String(value);
      div.appendChild(labelNode);
      div.appendChild(valueNode);
      cardsWrap.appendChild(div);
    });
    section.appendChild(cardsWrap);
    renderSampleSunbursts(section, sampleEntry.label, sampleEntry.figures);
    panel.appendChild(section);
  }

  function buildAssignmentSampleMatrix(lastRound, sourceKey, level, metricField, samplesWithReads, specialField) {
    const rows = get(lastRound, [sourceKey, "assignments_by_level", level], []);
    const sampleMetrics = (reportIdentityMode === "track" && hasTrackUnitMetrics(lastRound))
      ? get(lastRound, ["track_unit_metrics"], {})
      : get(lastRound, ["sample_metrics"], {});
    const targetTaxaByMarker = get(lastRound, ["markers", "target_taxa_by_marker"], {});
    const markerKingdomFiltered = (marker) => !!(targetTaxaByMarker[marker]);
    const matrixMap = new Map();
    const sampleIdByLabel = new Map();
    const taxonTotals = new Map();
    const taxonSupportedOtuTotals = new Map();
    const taxonReadTotals = new Map();
    const taxonSupportedSampleSets = new Map();
    const taxonSampleSets = new Map();
    const taxonMarkers = new Map();
    const sampleSet = new Set();
    const samplesWithReadEvidence = new Set(samplesWithReads instanceof Set ? Array.from(samplesWithReads) : []);
    const groupSortMetaByLabel = new Map();

    function compareTrackGroupSortMeta(a, b, aLabel, bLabel) {
      if (groupViewMode === "track_detail") {
        const sampleCmp = (a.track_sample_label || aLabel || "").localeCompare(b.track_sample_label || bLabel || "");
        if (sampleCmp !== 0) return sampleCmp;
        const repCmp = num(a.track_replicate_number) - num(b.track_replicate_number);
        if (repCmp !== 0) return repCmp;
        return (a.track_sample_replicate_label || aLabel || "").localeCompare(b.track_sample_replicate_label || bLabel || "");
      }
      if (groupViewMode === "replicate") {
        const sampleCmp = (a.track_sample_label || aLabel || "").localeCompare(b.track_sample_label || bLabel || "");
        if (sampleCmp !== 0) return sampleCmp;
        const repCmp = num(a.track_replicate_number) - num(b.track_replicate_number);
        if (repCmp !== 0) return repCmp;
        return (a.track_replicate_label || aLabel || "").localeCompare(b.track_replicate_label || bLabel || "");
      }
      return (a.track_sample_label || aLabel || "").localeCompare(b.track_sample_label || bLabel || "");
    }

    function recordTrackGroupSortMeta(label, raw, fallbackId) {
      if (!label || !raw || typeof raw !== "object" || reportIdentityMode !== "track") return;
      const meta = {
        track_sample_label: (raw.track_sample_label || "").toString().trim(),
        track_replicate_number: num(raw.track_replicate_number),
        track_replicate_label: (raw.track_replicate_label || raw.track_replicate_id || "").toString().trim(),
        track_sample_replicate_label: trackSampleReplicateLabel(raw),
        track_primer_label: (raw.track_primer_label || "").toString().trim(),
        track_unit_id: (raw.track_unit_id || fallbackId || label || "").toString().trim(),
      };
      const prev = groupSortMetaByLabel.get(label);
      if (!prev || compareTrackGroupSortMeta(meta, prev, label, label) < 0) {
        groupSortMetaByLabel.set(label, meta);
      }
    }

    if (sampleMetrics && typeof sampleMetrics === "object" && !Array.isArray(sampleMetrics)) {
      Object.entries(sampleMetrics).forEach(([sampleId, raw]) => {
        if (!raw || typeof raw !== "object") return;
        const label = metricGroupLabel(raw);
        if (!label) return;
        sampleSet.add(label);
        if (!sampleIdByLabel.has(label)) sampleIdByLabel.set(label, currentGroupId(label));
        recordTrackGroupSortMeta(label, raw, sampleId);
      });
    }

    if (Array.isArray(rows)) {
      rows.forEach((row) => {
        if (!row) return;
        const sampleLabel = assignmentGroupLabel(row);
        const taxon = (row.taxon || row[level] || "").toString().trim();
        const marker = canonicalMarkerToken(row.marker);
        const value = num(row[metricField]);
        if (!sampleLabel || !taxon || value <= 0) return;
        sampleSet.add(sampleLabel);
        if (num(row.reads_total) > 0 || value > 0) samplesWithReadEvidence.add(sampleLabel);
        recordTrackGroupSortMeta(sampleLabel, row, sampleLabel);
        if (!matrixMap.has(sampleLabel)) matrixMap.set(sampleLabel, new Map());
        const sampleMap = matrixMap.get(sampleLabel);
        const cell = sampleMap.get(taxon) || { supported: 0, unsupported: 0, frozen: 0, frozenReads: 0, consolidated: 0, consolidatedReads: 0, special: 0 };
        const supported = sourceKey !== "otu" || isSupportedOtuAssignment(row);
        if (!taxonMarkers.has(taxon)) taxonMarkers.set(taxon, new Set());
        if (marker && (supported || markerKingdomFiltered(marker))) taxonMarkers.get(taxon).add(marker);
        taxonTotals.set(taxon, (taxonTotals.get(taxon) || 0) + value);
        taxonReadTotals.set(taxon, (taxonReadTotals.get(taxon) || 0) + num(row.reads_total));
        if (!taxonSampleSets.has(taxon)) taxonSampleSets.set(taxon, new Set());
        taxonSampleSets.get(taxon).add(sampleLabel);
        if (supported) {
          cell.supported += value;
          if (!taxonSupportedSampleSets.has(taxon)) taxonSupportedSampleSets.set(taxon, new Set());
          taxonSupportedSampleSets.get(taxon).add(sampleLabel);
          if (sourceKey === "otu") {
            taxonSupportedOtuTotals.set(taxon, (taxonSupportedOtuTotals.get(taxon) || 0) + num(row.otu_count));
          }
        } else {
          cell.unsupported += value;
        }
        cell.frozen += num(row.frozen_otu_count);
        cell.frozenReads += num(row.frozen_otu_reads_total);
        cell.consolidated += num(row.consolidated_consensus_count);
        cell.consolidatedReads += num(row.consolidated_consensus_reads_total);
        if (specialField) cell.special += num(row[specialField]);
        if (Array.isArray(row.replicate_reads) && row.replicate_reads.length > 1) {
          cell.repReads = cell.repReads || {};
          row.replicate_reads.forEach((r) => {
            if (r && r.label) {
              cell.repReads[r.label] = (cell.repReads[r.label] || 0) + (r.count || 0);
            }
          });
        }
        sampleMap.set(taxon, cell);
      });
    }

    const configuredMarkers = markerOrderFromData([lastRound || {}]);
    const markerIndex = new Map(configuredMarkers.map((marker, idx) => [marker, idx]));
    const markerRank = (taxon) => {
      const markers = Array.from(taxonMarkers.get(taxon) || []);
      if (!markers.length) return Number.MAX_SAFE_INTEGER;
      return Math.min(...markers.map((marker) => markerIndex.has(marker) ? markerIndex.get(marker) : configuredMarkers.length + 1));
    };

    const taxonList = Array.from(taxonTotals.entries())
      .sort((a, b) => {
        if (sourceKey === "otu") {
          const markerDiff = markerRank(a[0]) - markerRank(b[0]);
          if (markerDiff !== 0) return markerDiff;
          const supportedSampleDiff = (taxonSupportedSampleSets.get(b[0]) || new Set()).size - (taxonSupportedSampleSets.get(a[0]) || new Set()).size;
          if (supportedSampleDiff !== 0) return supportedSampleDiff;
          const sampleDiff = (taxonSampleSets.get(b[0]) || new Set()).size - (taxonSampleSets.get(a[0]) || new Set()).size;
          if (sampleDiff !== 0) return sampleDiff;
          const supportedDiff = (taxonSupportedOtuTotals.get(b[0]) || 0) - (taxonSupportedOtuTotals.get(a[0]) || 0);
          if (supportedDiff !== 0) return supportedDiff;
          const totalDiff = (taxonTotals.get(b[0]) || 0) - (taxonTotals.get(a[0]) || 0);
          if (totalDiff !== 0) return totalDiff;
          const readsDiff = (taxonReadTotals.get(b[0]) || 0) - (taxonReadTotals.get(a[0]) || 0);
          if (readsDiff !== 0) return readsDiff;
        } else if (sourceKey === "consensus") {
          const markerDiff = markerRank(a[0]) - markerRank(b[0]);
          if (markerDiff !== 0) return markerDiff;
          const sampleDiff = (taxonSampleSets.get(b[0]) || new Set()).size - (taxonSampleSets.get(a[0]) || new Set()).size;
          if (sampleDiff !== 0) return sampleDiff;
          const totalDiff = (taxonTotals.get(b[0]) || 0) - (taxonTotals.get(a[0]) || 0);
          if (totalDiff !== 0) return totalDiff;
          const readsDiff = (taxonReadTotals.get(b[0]) || 0) - (taxonReadTotals.get(a[0]) || 0);
          if (readsDiff !== 0) return readsDiff;
        }
        return b[1] - a[1] || a[0].localeCompare(b[0]);
      })
      .map(([taxon]) => taxon);
    const noReadsSamples = new Set();
    sampleSet.forEach((label) => {
      if (!samplesWithReadEvidence.has(label)) noReadsSamples.add(label);
    });
    const sampleList = Array.from(sampleSet).sort((a, b) => {
      const aNoReads = noReadsSamples.has(a);
      const bNoReads = noReadsSamples.has(b);
      if (aNoReads !== bNoReads) return aNoReads ? 1 : -1;
      const aNoAdapter = /^no_adapter$/i.test(a);
      const bNoAdapter = /^no_adapter$/i.test(b);
      if (aNoAdapter !== bNoAdapter) return aNoAdapter ? 1 : -1;
      if (reportIdentityMode === "track") {
        const aMeta = groupSortMetaByLabel.get(a);
        const bMeta = groupSortMetaByLabel.get(b);
        if (aMeta || bMeta) return compareTrackGroupSortMeta(aMeta || {}, bMeta || {}, a, b);
      }
      return a.localeCompare(b);
    });

    let maxVal = 0;
    sampleList.forEach((sampleLabel) => {
      taxonList.forEach((taxon) => {
        const cell = (matrixMap.get(sampleLabel) || new Map()).get(taxon) || { supported: 0, unsupported: 0 };
        const v = cell.supported > 0 ? cell.supported : cell.unsupported;
        if (v > maxVal) maxVal = v;
      });
    });

    const taxonMarkerMap = new Map();
    taxonList.forEach((taxon) => {
      const markers = Array.from(taxonMarkers.get(taxon) || []);
      markers.sort((a, b) => {
        const ai = markerIndex.has(a) ? markerIndex.get(a) : configuredMarkers.length + 1;
        const bi = markerIndex.has(b) ? markerIndex.get(b) : configuredMarkers.length + 1;
        return ai - bi || a.localeCompare(b);
      });
      taxonMarkerMap.set(taxon, markers.length ? markers[0] : "OTHER");
    });

    const hasSpecialCounts = Array.from(matrixMap.values()).some((sampleMap) =>
      Array.from(sampleMap.values()).some((cell) => num(cell.special) > 0),
    );

    return { matrixMap, sampleList, taxonList, maxVal, taxonMarkerMap, sampleIdByLabel, noReadsSamples, hasSpecialCounts };
  }

  // ── Assignments × sample heatmap ──────────────────────────────────────────
  function renderSpeciesSampleTable(mountNode) {
    const target = mountNode || document.getElementById("global-overview");
    if (!target || viewScope !== "run") return;

    const lastRound = rounds.length > 0 ? rounds[rounds.length - 1] : null;
    const samplesWithReads = new Set();
    rounds.forEach((r) => {
      const sm = (reportIdentityMode === "track" && hasTrackUnitMetrics(r))
        ? r.track_unit_metrics
        : ((r && typeof r.sample_metrics === "object" && !Array.isArray(r.sample_metrics)) ? r.sample_metrics : {});
      Object.values(sm).forEach((raw) => {
        if (raw && num(raw.reads_demux) > 0) samplesWithReads.add(metricGroupLabel(raw));
      });
    });
    const viewDefs = [
      {
        id: "otu_otu_count",
        label: "OTU assignments: OTUs",
        sourceKey: "otu",
        metricField: "otu_count",
        specialField: "frozen_otu_count",
        specialUnit: "frozen OTU",
        emptyLabel: "No OTU assignment counts available for this level.",
      },
      {
        id: "otu_reads_total",
        label: "OTU assignments: reads",
        sourceKey: "otu",
        metricField: "reads_total",
        specialField: "frozen_otu_reads_total",
        specialUnit: "reads from frozen OTUs",
        emptyLabel: "No OTU read totals available for this level.",
      },
      {
        id: "consensus_consensus_count",
        label: "Consensus assignments: consensus",
        sourceKey: "consensus",
        metricField: "consensus_count",
        specialField: "consolidated_consensus_count",
        specialUnit: "consolidated consensus",
        emptyLabel: "No consensus counts available for this level.",
      },
      {
        id: "consensus_reads_total",
        label: "Consensus assignments: reads",
        sourceKey: "consensus",
        metricField: "reads_total",
        specialField: "consolidated_consensus_reads_total",
        specialUnit: "reads from consolidated consensus",
        emptyLabel: "No consensus read totals available for this level.",
      },
    ];
    const levels = ["species", "genus", "family"];

    const section = document.createElement("section");
    section.id = "species-sample-section";
    const h3 = document.createElement("h3");
    h3.textContent = `Assignments by ${groupEntityLabel()}`;
    section.appendChild(h3);

    const hasAnyData = viewDefs.some((view) =>
      levels.some((level) => {
        const rows = get(lastRound, [view.sourceKey, "assignments_by_level", level], []);
        return Array.isArray(rows) && rows.length > 0;
      }),
    );

    if (!lastRound || !hasAnyData) {
      const p = document.createElement("p");
      p.className = "otu-assign-empty";
      p.textContent = "No assignment data available.";
      section.appendChild(p);
      target.appendChild(section);
      return;
    }

    let activeView = viewDefs.find((view) =>
      levels.some((level) => {
        const rows = get(lastRound, [view.sourceKey, "assignments_by_level", level], []);
        return Array.isArray(rows) && rows.length > 0;
      }),
    ) || viewDefs[0];
    let activeLevel = levels.find((level) => {
      const rows = get(lastRound, [activeView.sourceKey, "assignments_by_level", level], []);
      return Array.isArray(rows) && rows.length > 0;
    }) || "species";

    const intro = document.createElement("p");
    intro.className = "species-sample-help";
    intro.textContent = `Switch between OTU and consensus assignments, then change taxonomic level to compare distributions across ${groupEntityPlural().toLowerCase()}.`;
    section.appendChild(intro);

    const viewTabs = document.createElement("div");
    viewTabs.className = "assignment-tabs";
    const levelTabs = document.createElement("div");
    levelTabs.className = "assignment-tabs";
    const statusRow = document.createElement("div");
    statusRow.className = "chart-header";
    const status = document.createElement("p");
    status.className = "species-sample-status";
    const downloads = document.createElement("div");
    downloads.className = "download-links";
    const tableMount = document.createElement("div");

    const viewButtons = viewDefs.map((view) => {
      const btn = document.createElement("button");
      btn.className = "assignment-tab";
      btn.textContent = view.label;
      btn.addEventListener("click", () => {
        activeView = view;
        renderMatrix();
      });
      viewTabs.appendChild(btn);
      return { view, btn };
    });

    const levelButtons = levels.map((level) => {
      const btn = document.createElement("button");
      btn.className = "assignment-tab";
      btn.textContent = level.charAt(0).toUpperCase() + level.slice(1);
      btn.addEventListener("click", () => {
        activeLevel = level;
        renderMatrix();
      });
      levelTabs.appendChild(btn);
      return { level, btn };
    });

    section.appendChild(viewTabs);
    section.appendChild(levelTabs);
    statusRow.appendChild(status);
    statusRow.appendChild(downloads);
    section.appendChild(statusRow);
    section.appendChild(tableMount);

    function renderMatrix() {
      viewButtons.forEach(({ view, btn }) => btn.classList.toggle("active", view.id === activeView.id));
      levelButtons.forEach(({ level, btn }) => btn.classList.toggle("active", level === activeLevel));

      const matrix = buildAssignmentSampleMatrix(lastRound, activeView.sourceKey, activeLevel, activeView.metricField, samplesWithReads, activeView.specialField);
      const highlightSpec = assignmentSunburstHighlightSpec(activeView.sourceKey);
      status.textContent = `${activeView.label} at ${activeLevel} level.`;
      if (matrix.hasSpecialCounts) {
        status.textContent += ` Cells show total values; bold values in parentheses show ${activeView.specialUnit}.`;
      } else {
        status.textContent += ` Cells show total values. No ${highlightSpec.label} are present in this view.`;
      }
      if (activeView.sourceKey === "otu") {
        status.textContent += `. Purple cells contain frozen OTUs. Light orange cells mark OTU assignments with fewer than ${OTU_ASSIGNMENT_MIN_READS} supporting reads (not counted as supported).`;
      } else if (activeView.sourceKey === "consensus") {
        status.textContent += `. Amber cells contain consolidated consensus sequences.`;
      }
      clearNode(downloads);
      clearNode(tableMount);

      if (!matrix.taxonList.length) {
        const empty = document.createElement("p");
        empty.className = "otu-assign-empty";
        empty.textContent = activeView.emptyLabel;
        tableMount.appendChild(empty);
        return;
      }

      const columns = [groupEntityLabel(), ...matrix.taxonList];
      const tsvRows = matrix.sampleList.map((sampleLabel) => [
        sampleLabel,
        ...matrix.taxonList.map((taxon) => {
          const cell = (matrix.matrixMap.get(sampleLabel) || new Map()).get(taxon) || { supported: 0, unsupported: 0 };
          return cell.supported > 0 ? cell.supported : cell.unsupported;
        }),
      ]);
      downloads.appendChild(
        makeDownloadLink(
          `assignments_by_sample_${activeView.id}_${activeLevel}`,
          makeTsv(columns, tsvRows),
        ),
      );

      const markerGroups = [];
      let currentGroup = null;
      matrix.taxonList.forEach((taxon) => {
        const markerGroup = matrix.taxonMarkerMap ? matrix.taxonMarkerMap.get(taxon) : "other";
        if (!currentGroup || currentGroup.markerGroup !== markerGroup) {
          currentGroup = { markerGroup, count: 0 };
          markerGroups.push(currentGroup);
        }
        currentGroup.count += 1;
      });
      const showMarkerHeader = markerGroups.some((group) => canonicalMarkerToken(group.markerGroup) !== "OTHER");

      const wrap = document.createElement("div");
      wrap.className = "species-sample-wrap";
      const table = document.createElement("table");
      table.className = "species-sample-table";
      const thead = document.createElement("thead");
      if (showMarkerHeader) {
        const trg = document.createElement("tr");
        trg.className = "assignment-group-row";
        const corner = document.createElement("th");
        corner.className = "row-header";
          corner.textContent = groupEntityLabel();
        corner.rowSpan = 2;
        trg.appendChild(corner);
        markerGroups.forEach((group) => {
          const th = document.createElement("th");
          th.className = "assignment-group-header";
          const groupMarker = canonicalMarkerToken(group.markerGroup);
          if (groupMarker === "COI") {
            th.classList.add("assignment-header-coi");
          } else if (groupMarker === "ITS2") {
            th.classList.add("assignment-header-its");
          }
          th.textContent = groupMarker === "OTHER" ? "Other assignments" : `${groupMarker}-based assignments`;
          th.colSpan = group.count;
          trg.appendChild(th);
        });
        thead.appendChild(trg);
      }

      const trh = document.createElement("tr");
      if (!showMarkerHeader) {
        const corner = document.createElement("th");
        corner.className = "row-header";
        corner.textContent = groupEntityLabel();
        trh.appendChild(corner);
      }
      matrix.taxonList.forEach((taxon) => {
        const th = document.createElement("th");
        th.className = "col-header";
        if (showMarkerHeader) th.classList.add("assignment-subheader");
        const markerGroup = canonicalMarkerToken(matrix.taxonMarkerMap ? matrix.taxonMarkerMap.get(taxon) : "OTHER");
        if (markerGroup === "COI") th.classList.add("assignment-header-coi");
        else if (markerGroup === "ITS2") th.classList.add("assignment-header-its");
        th.textContent = taxon;
        th.title = taxon;
        trh.appendChild(th);
      });
      thead.appendChild(trh);
      table.appendChild(thead);

      const tbody = document.createElement("tbody");
      matrix.sampleList.forEach((sampleLabel) => {
        const isNoReads = matrix.noReadsSamples instanceof Set && matrix.noReadsSamples.has(sampleLabel);
        const tr = document.createElement("tr");
        if (isNoReads) tr.style.background = "#f5f5f5";
        const nameTd = document.createElement("td");
        nameTd.className = "sample-name";
        if (isNoReads) {
          nameTd.style.background = "#f5f5f5";
          nameTd.textContent = `${sampleLabel} (no reads)`;
        } else {
          const sampleId = matrix.sampleIdByLabel instanceof Map ? matrix.sampleIdByLabel.get(sampleLabel) : "";
          if (sampleId) {
            const sampleLink = document.createElement("a");
            sampleLink.href = `#sample-${sampleId}`;
            sampleLink.textContent = sampleLabel;
            nameTd.appendChild(sampleLink);
          } else {
            nameTd.textContent = sampleLabel;
          }
        }
        tr.appendChild(nameTd);

        function setCellValue(td, value, specialValue) {
          td.textContent = String(value);
          if (specialValue > 0) {
            td.appendChild(document.createTextNode(" "));
            const special = document.createElement("span");
            special.className = "assignment-special-count";
            if (activeView.sourceKey === "otu") special.classList.add("assignment-special-count-frozen");
            else if (activeView.sourceKey === "consensus") special.classList.add("assignment-special-count-consolidated");
            special.textContent = `(${specialValue})`;
            td.appendChild(special);
          }
        }

        matrix.taxonList.forEach((taxon) => {
          const cell = (matrix.matrixMap.get(sampleLabel) || new Map()).get(taxon) || { supported: 0, unsupported: 0 };
          const v = cell.supported > 0 ? cell.supported : cell.unsupported;
          const specialValue = num(cell.special);
          const td = document.createElement("td");
          td.title = `${sampleLabel} / ${taxon}: ${v || 0}`;
          if (isNoReads) {
            td.style.background = "#f5f5f5";
            if (v > 0) setCellValue(td, v, specialValue);
          } else if (v > 0) {
            setCellValue(td, v, specialValue);
            const intensity = matrix.maxVal > 0 ? v / matrix.maxVal : 0;
            if (activeView.sourceKey === "otu" && cell.frozen > 0) {
              // Purple gradient: frozen OTUs present
              const r = Math.round(237 + (106 - 237) * intensity);
              const g = Math.round(224 + (40 - 224) * intensity);
              const b = Math.round(247 + (160 - 247) * intensity);
              td.style.background = `rgb(${r},${g},${b})`;
              td.style.color = intensity > 0.6 ? "#ffffff" : "#3d1060";
              td.title = specialValue > 0
                ? `${sampleLabel} / ${taxon}: ${v} (${specialValue} ${activeView.specialUnit})`
                : `${sampleLabel} / ${taxon}: ${v} (${cell.frozen} frozen OTU${cell.frozen !== 1 ? "s" : ""})`;
            } else if (activeView.sourceKey === "consensus" && cell.consolidated > 0) {
              // Amber gradient: consolidated consensus present
              const r = Math.round(254 + (181 - 254) * intensity);
              const g = Math.round(247 + (129 - 247) * intensity);
              const b = Math.round(224 + (15 - 224) * intensity);
              td.style.background = `rgb(${r},${g},${b})`;
              td.style.color = intensity > 0.6 ? "#ffffff" : "#5a3e00";
              td.title = specialValue > 0
                ? `${sampleLabel} / ${taxon}: ${v} (${specialValue} ${activeView.specialUnit})`
                : `${sampleLabel} / ${taxon}: ${v} (${cell.consolidated} consolidated)`;
            } else if (activeView.sourceKey === "otu" && cell.supported <= 0 && cell.unsupported > 0) {
              // Light orange: unsupported (low reads)
              td.style.background = "#fde8d8";
              td.style.color = "#7a3b18";
              td.title = `${sampleLabel} / ${taxon}: ${v} (<${OTU_ASSIGNMENT_MIN_READS} supporting reads, not counted as supported)`;
            } else {
              // Blue-green gradient: normal active cells
              const r = Math.round(233 + (44 - 233) * intensity);
              const g = Math.round(243 + (110 - 243) * intensity);
              const b = Math.round(227 + (73 - 227) * intensity);
              td.style.background = `rgb(${r},${g},${b})`;
              td.style.color = intensity > 0.6 ? "#ffffff" : "#1b2417";
            }
          }
          const repKeys = Object.keys(cell.repReads || {});
          if (repKeys.length > 1) {
            const repSpan = document.createElement("span");
            repSpan.className = "rep-breakdown";
            const sortedRepKeys = repKeys.sort((a, b) => {
              const na = parseInt((a.match(/_(\d+)$/) || [])[1] || "0", 10);
              const nb = parseInt((b.match(/_(\d+)$/) || [])[1] || "0", 10);
              return na - nb || a.localeCompare(b);
            });
            repSpan.textContent = `(${sortedRepKeys.map((k) => cell.repReads[k]).join(" / ")})`;
            td.appendChild(repSpan);
          }
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      wrap.appendChild(table);
      tableMount.appendChild(wrap);
    }

    renderMatrix();
    target.appendChild(section);
  }

  function renderSampleSections() {
    if (!sampleIndex || !sampleDetails) return;

    if (viewScope !== "run") {
      const msg = document.createElement("p");
      msg.className = "sample-index-empty";
      msg.textContent = "Sample details are available in per-run reports.";
      sampleIndex.appendChild(msg);
      return;
    }

    const anyDemuxOn = detectDemuxEnabled();
    if (!anyDemuxOn) {
      const msg = document.createElement("p");
      msg.className = "sample-index-empty";
      msg.textContent = "Sample details are hidden because demultiplexing is disabled for this run.";
      sampleIndex.appendChild(msg);
      return;
    }

    const samples = new Map();
    const useTrackUnitMetrics = reportIdentityMode === "track" && rounds.some((r) => hasTrackUnitMetrics(r));
    rounds.forEach((r) => {
      const roundId = r.round_barcode || "-";
      const sampleMetrics = useTrackUnitMetrics
        ? ((r && typeof r.track_unit_metrics === "object" && !Array.isArray(r.track_unit_metrics)) ? r.track_unit_metrics : {})
        : ((r && typeof r.sample_metrics === "object" && !Array.isArray(r.sample_metrics)) ? r.sample_metrics : {});
      Object.entries(sampleMetrics).forEach(([sampleId, raw]) => {
        if (!raw || typeof raw !== "object") return;
        const label = useTrackUnitMetrics ? metricGroupLabel(raw) : currentGroupLabel(raw.label || raw.sample_id || sampleId);
        if (!label) return;
        const groupId = useTrackUnitMetrics ? currentGroupId(label) : currentGroupId(label);
        if (!samples.has(groupId)) {
          samples.set(groupId, {
            sample_id: groupId,
            label,
            totals: {
              reads_demux: 0,
              reads_demux_coi: 0,
              reads_demux_its2: 0,
              reads_demux_by_marker: {},
              reads_blast_assigned: 0,
              otu_active: 0,
              consensus_emitted: 0,
            },
            roundsById: new Map(),
            figures: [],
            replicates: [],
            sort_sample_label: useTrackUnitMetrics ? ((raw.track_sample_label || "").toString()) : label,
            sort_replicate_number: useTrackUnitMetrics ? num(raw.track_replicate_number) : 0,
            sort_track_replicate_label: useTrackUnitMetrics ? ((raw.track_replicate_label || raw.track_replicate_id || "").toString()) : "",
            sort_track_sample_replicate_label: useTrackUnitMetrics ? trackSampleReplicateLabel(raw) : "",
            sort_primer_label: useTrackUnitMetrics ? ((raw.track_primer_label || "").toString()) : "",
            sort_track_unit_id: useTrackUnitMetrics ? ((raw.track_unit_id || sampleId || "").toString()) : "",
          });
        }
        const entry = samples.get(groupId);
        if (!entry.roundsById.has(roundId)) {
          entry.roundsById.set(roundId, {
            round_barcode: roundId,
            reads_demux: 0,
            reads_blast_assigned: 0,
            otu_active: 0,
            consensus_emitted: 0,
          });
        }
        const row = entry.roundsById.get(roundId);
        row.reads_demux += num(raw.reads_demux);
        row.reads_blast_assigned += num(raw.reads_blast_assigned);
        row.otu_active += num(raw.otu_active);
        row.consensus_emitted += num(raw.consensus_emitted);
        entry.totals.reads_demux += num(raw.reads_demux);
        entry.totals.reads_demux_coi += num(raw.reads_demux_coi);
        entry.totals.reads_demux_its2 += num(raw.reads_demux_its2);
        mergeMarkerCounts(entry.totals.reads_demux_by_marker, raw.reads_demux_by_marker);
        entry.totals.reads_blast_assigned += num(raw.reads_blast_assigned);
        entry.totals.otu_active += num(raw.otu_active);
        entry.totals.consensus_emitted += num(raw.consensus_emitted);
        if (reportIdentityMode !== "track") {
          const sampleFigs = Array.isArray(raw.figures) ? raw.figures.filter(Boolean) : [];
          if (sampleFigs.length && !entry.figures.length) {
            entry.figures = sampleFigs;
          }
        }
        if (!useTrackUnitMetrics && groupViewMode === "replicate" && raw && typeof raw.replicates === "object" && !Array.isArray(raw.replicates)) {
          entry.replicates = Object.keys(raw.replicates).map((replicateId) => {
            const rep = raw.replicates[replicateId] || {};
            return {
              id: replicateId,
              label: (rep.label || replicateId || "Barcode").toString(),
              reads_demux: num(rep.reads_demux),
            };
          }).sort((a, b) => {
            const diff = b.reads_demux - a.reads_demux;
            if (diff !== 0) return diff;
            return a.label.localeCompare(b.label);
          });
        }
      });
    });

    if (!samples.size) {
      const msg = document.createElement("p");
      msg.className = "sample-index-empty";
      msg.textContent = "No sample-specific data available for this run.";
      sampleIndex.appendChild(msg);
      return;
    }

    const ordered = Array.from(samples.values()).map((entry) => ({
      ...entry,
      rounds: Array.from(entry.roundsById.values()),
    })).sort((a, b) => {
      const aNoReads = (a.totals.reads_demux || 0) === 0;
      const bNoReads = (b.totals.reads_demux || 0) === 0;
      if (aNoReads !== bNoReads) return aNoReads ? 1 : -1;
      if (useTrackUnitMetrics && groupViewMode === "track_detail") {
        const sampleCmp = (a.sort_sample_label || "").localeCompare(b.sort_sample_label || "");
        if (sampleCmp !== 0) return sampleCmp;
        const repCmp = num(a.sort_replicate_number) - num(b.sort_replicate_number);
        if (repCmp !== 0) return repCmp;
        return (a.sort_track_sample_replicate_label || a.label || "").localeCompare(b.sort_track_sample_replicate_label || b.label || "");
      }
      if (useTrackUnitMetrics && groupViewMode === "replicate") {
        const sampleCmp = (a.sort_sample_label || "").localeCompare(b.sort_sample_label || "");
        if (sampleCmp !== 0) return sampleCmp;
        const repCmp = num(a.sort_replicate_number) - num(b.sort_replicate_number);
        if (repCmp !== 0) return repCmp;
        return (a.sort_track_replicate_label || a.label || "").localeCompare(b.sort_track_replicate_label || b.label || "");
      }
      const aNoAdapter = /^no_adapter$/i.test(a.label);
      const bNoAdapter = /^no_adapter$/i.test(b.label);
      if (aNoAdapter !== bNoAdapter) return aNoAdapter ? 1 : -1;
      return a.label.localeCompare(b.label);
    });

    drawDemuxByMarkerPerSample(ordered, sampleDetails);

    ordered.forEach((s) => {
      if (s.totals.reads_demux === 0) return;
      const panel = document.createElement("article");
      panel.className = "sample-panel";
      panel.id = `sample-${s.sample_id}`;

      const headingRow = document.createElement("div");
      headingRow.className = "sample-panel-heading";
      const h = document.createElement("h3");
      h.textContent = s.label;
      headingRow.appendChild(h);
      const backLink = document.createElement("a");
      backLink.className = "sample-back-link";
      backLink.href = "#global-overview";
      backLink.textContent = "Back to top";
      headingRow.appendChild(backLink);
      panel.appendChild(headingRow);

      s.current = getSampleRoundSnapshot(rounds.length ? rounds[rounds.length - 1] : null, s.sample_id, s.label) || {};
      renderSampleEvolutionSection(panel, s);
      renderCurrentSampleResults(panel, s);

      sampleDetails.appendChild(panel);
    });
  }

  if (viewScope === "run") {
    renderSampleSections();

    const historyPrefix = "global_history";
    const warningSeen = new Set();
    parseWarnings.forEach((w) => {
      const msg = `${historyPrefix}: ${w}`;
      if (warningSeen.has(msg)) return;
      warningSeen.add(msg);
      appendWarning(msg);
    });

    if (!warningsInitialized) {
      appendWarning("none");
    }
  }

  function startAutoRefresh() {
    const enabled = meta.auto_refresh_enabled === true;
    if (!enabled) return;
    const currentRevision = typeof meta.report_revision === "string" ? meta.report_revision : "";
    const stateUrl = (typeof meta.state_url === "string" && meta.state_url) ? meta.state_url : "report_state.json";
    const intervalSecRaw = Number(meta.refresh_interval_sec);
    const intervalSec = Number.isFinite(intervalSecRaw) && intervalSecRaw > 0 ? intervalSecRaw : 15;
    let warned = false;

    const poll = () => {
      fetch(stateUrl, { cache: "no-store" })
        .then((res) => {
          if (!res.ok) {
            throw new Error(`HTTP ${res.status}`);
          }
          return res.json();
        })
        .then((state) => {
          const nextRevision = state && typeof state.report_revision === "string" ? state.report_revision : "";
          if (!nextRevision) return;
          if (currentRevision && nextRevision !== currentRevision) {
            window.location.reload();
          }
        })
        .catch(() => {
          if (!warned) {
            appendWarning("auto_refresh_unavailable: failed to fetch report_state.json (manual refresh may be required)");
            warned = true;
          }
        });
    };

    window.setInterval(poll, intervalSec * 1000);
  }

  startAutoRefresh();

  function figureFromThumb(img) {
    const title = (img.dataset.figTitle || "").trim();
    const desc = (img.dataset.figDesc || "").trim();
    return {
      src: img.getAttribute("src") || "",
      title,
      desc,
    };
  }

  function openFigureModal(fig) {
    if (!modal || !modalImg || !modalCaption) return;
    lastFocus = document.activeElement;
    modalImg.src = fig.src || "";
    const title = fig.title || "Figure";
    modalImg.alt = title;
    if (modalTitle) {
      modalTitle.textContent = title;
    }
    let caption = "";
    if (fig.title && fig.desc) {
      caption = `${fig.title} - ${fig.desc}`;
    } else if (fig.title) {
      caption = fig.title;
    } else if (fig.desc) {
      caption = fig.desc;
    }
    modalCaption.textContent = caption;
    modal.classList.add("is-open");
    modal.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    if (modalClose) {
      modalClose.focus();
    }
  }

  function closeFigureModal() {
    if (!modal) return;
    modal.classList.remove("is-open");
    modal.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
    if (modalImg) {
      modalImg.src = "";
    }
    if (modalTitle) {
      modalTitle.textContent = "Figure";
    }
    if (modalCaption) {
      modalCaption.textContent = "";
    }
    if (lastFocus && typeof lastFocus.focus === "function") {
      lastFocus.focus();
    }
  }

  if (modal) {
    document.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      // Only image thumbnails are interactive; placeholders are non-image elements.
      const img = target.closest("img.figure-thumb");
      if (!img) return;
      if (img.classList.contains("figure-missing")) return;
      openFigureModal(figureFromThumb(img));
    });
    document.addEventListener("keydown", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      // Only image thumbnails are interactive; placeholders are non-image elements.
      const img = target.closest("img.figure-thumb");
      if (!img) return;
      if (img.classList.contains("figure-missing")) return;
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openFigureModal(figureFromThumb(img));
      }
    });
  }
  if (modalClose) {
    modalClose.addEventListener("click", closeFigureModal);
  }
  if (modal) {
    modal.addEventListener("click", (event) => {
      if (event.target === modal) {
        closeFigureModal();
      }
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && modal && modal.classList.contains("is-open")) {
      closeFigureModal();
    }
  });
})();
