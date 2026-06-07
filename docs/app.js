"use strict";
const pct = x => (x * 100).toFixed(1) + "%";
const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c; if (h != null) e.innerHTML = h; return e; };

let DATA = null;

// --- iframe embed support -------------------------------------------------- #
// ?embed=1 slims the chrome; the page reports its height so a parent iframe can
// auto-resize (no inner scrollbar). Height messages carry no sensitive data.
const EMBED = new URLSearchParams(location.search).get("embed") === "1";
if (EMBED) document.documentElement.classList.add("embed");
function postHeight() {
  if (window.parent === window) return;
  const h = Math.ceil(document.documentElement.getBoundingClientRect().height);
  window.parent.postMessage({ absTrackerHeight: h }, "*");
}
if (window.ResizeObserver) new ResizeObserver(postHeight).observe(document.body);
window.addEventListener("load", () => { postHeight(); setTimeout(postHeight, 500); });
window.addEventListener("resize", postHeight);

fetch("data.json?t=" + Date.now())
  .then(r => r.json())
  .then(d => { DATA = d; render(d); })
  .catch(e => { document.getElementById("updated").textContent = "Failed to load data.json"; console.error(e); });

function render(d) {
  document.getElementById("updated").textContent =
    `Updated ${d.updated} · ${d.league.games_logged} games logged · ${DATA.season} season`;
  document.querySelectorAll(".minteam").forEach(s => s.textContent = d.min_team_chal);

  cards(d);
  correlation(d);
  teamTable(d);
  challengerProfiles(d);
  umpTable(d);
  trendChart(d);
  playerTabs(d);
  postHeight();
  setTimeout(postHeight, 300);
}

function cards(d) {
  const L = d.league;
  const elig = d.teams.filter(t => t.chal >= d.min_team_chal);
  const best = elig[0], worst = elig[elig.length - 1];
  const ue = d.umpires.filter(u => u.challenges >= d.min_ump_chal);
  const mostU = ue[0];
  const items = [
    [pct(L.rate), "of challenges overturned"],
    [L.per_team, "challenges per team"],
    [best ? `${best.team.split(" ").pop()} ${pct(best.rate)}` : "-", "best team success rate"],
    [mostU ? `${mostU.umpire.split(" ").pop()} ${pct(mostU.rate)}` : "-", "most-overturned umpire"],
    [(d.correlation.win_pct.r ?? "-"), "success vs win% (r)"],
  ];
  const box = document.getElementById("cards");
  box.innerHTML = "";
  items.forEach(([b, l]) => {
    const c = el("div", "card");
    c.append(el("div", "big", b), el("div", "lbl", l));
    box.append(c);
  });
}

function correlation(d) {
  const c = d.correlation;
  const read = describe(c.win_pct.r);
  document.getElementById("corr-read").innerHTML =
    `Across all ${c.win_pct.n} teams, a team's challenge success rate has ${read} with winning percentage. ` +
    `In plain terms: being good at the robot challenge is <strong>not</strong> a meaningful driver of standings so far. ` +
    `These are 30-team, partial-season correlations, so read them as directional, not proof.`;
  const stats = document.getElementById("corr-stats");
  stats.innerHTML = "";
  const rows = [["Win %", c.win_pct.r], ["Team ERA", c.era.r], ["Run differential", c.run_diff.r]];
  rows.forEach(([k, v]) => {
    const s = el("div", "stat");
    s.append(el("span", null, "vs " + k), el("span", "v", v == null ? "n/a" : (v >= 0 ? "+" : "") + v));
    stats.append(s);
  });
  scatter(d);
}

function describe(r) {
  if (r == null) return "no measurable relationship";
  const a = Math.abs(r);
  const s = a < 0.1 ? "essentially no relationship" : a < 0.3 ? "a weak relationship"
    : a < 0.5 ? "a moderate relationship" : "a strong relationship";
  return `${s} (r = ${r >= 0 ? "+" : ""}${r})`;
}

let charts = {};
function scatter(d) {
  const pts = d.teams.filter(t => t.win_pct != null).map(t => ({ x: t.rate * 100, y: t.win_pct, team: t.abbr }));
  charts.scatter?.destroy();
  charts.scatter = new Chart(document.getElementById("scatter"), {
    type: "scatter",
    data: { datasets: [{ data: pts, backgroundColor: "#58a6ff", pointRadius: 5, pointHoverRadius: 7 }] },
    options: {
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => `${c.raw.team}: ${c.raw.x.toFixed(1)}% success, ${c.raw.y.toFixed(3)} win%` } }
      },
      scales: {
        x: { title: { display: true, text: "Challenge success rate (%)", color: "#8b97a6" }, ticks: { color: "#8b97a6" }, grid: { color: "#26303d" } },
        y: { title: { display: true, text: "Winning percentage", color: "#8b97a6" }, ticks: { color: "#8b97a6" }, grid: { color: "#26303d" } }
      }
    }
  });
}

function sortableTable(tableId, cols, rows, opts = {}) {
  const table = document.getElementById(tableId);
  let sortKey = opts.sortKey, asc = false;
  function draw() {
    const sorted = [...rows].sort((a, b) => {
      const va = a[sortKey], vb = b[sortKey];
      const cmp = (typeof va === "number") ? va - vb : String(va).localeCompare(String(vb));
      return asc ? cmp : -cmp;
    });
    table.innerHTML = "";
    const thead = el("thead"), htr = el("tr");
    htr.append(el("th", "rank", "#"));
    cols.forEach(c => {
      const th = el("th", c.key === sortKey ? "sorted " + (asc ? "asc" : "") : "", c.label);
      th.onclick = () => { if (sortKey === c.key) asc = !asc; else { sortKey = c.key; asc = !!c.asc; } draw(); };
      htr.append(th);
    });
    thead.append(htr); table.append(thead);
    const tb = el("tbody");
    sorted.forEach((r, i) => {
      const tr = el("tr", opts.rowClass ? opts.rowClass(r) : "");
      tr.append(el("td", "rank", i + 1));
      cols.forEach(c => {
        const td = el("td", c.cls || "");
        td.innerHTML = c.fmt ? c.fmt(r[c.key], r) : r[c.key];
        tr.append(td);
      });
      tb.append(tr);
    });
    table.append(tb);
  }
  draw();
}

function teamTable(d) {
  const elig = d.teams.filter(t => t.chal >= d.min_team_chal);
  const best = elig[0]?.abbr, worst = elig[elig.length - 1]?.abbr;
  sortableTable("team-table", [
    { key: "team", label: "Team", asc: true },
    { key: "chal", label: "Challenges" },
    { key: "overturned", label: "Overturned" },
    { key: "rate", label: "Success Rate", cls: "rate", fmt: v => pct(v) },
    { key: "win_pct", label: "Win%", fmt: v => v == null ? "-" : v.toFixed(3) },
    { key: "era", label: "ERA", fmt: v => v == null ? "-" : v.toFixed(2) },
  ], d.teams, { sortKey: "rate", rowClass: t => t.abbr === best ? "best" : t.abbr === worst ? "worst" : "" });
}

function challengerProfiles(d) {
  const rl = d.role_league;
  const tot = rl.batter.n + rl.catcher.n + rl.pitcher.n;
  const share = r => tot ? Math.round(rl[r].n / tot * 100) : 0;
  document.getElementById("role-read").innerHTML =
    `Catchers do the most challenging and win most often (${pct(rl.catcher.rate)}), ` +
    `batters are middle of the pack (${pct(rl.batter.rate)}), and pitchers rarely bother ` +
    `and rarely win (${pct(rl.pitcher.rate)}). The robot rewards the players who see the zone best.`;
  const rc = document.getElementById("role-cards");
  rc.innerHTML = "";
  [["Catchers", "catcher"], ["Batters", "batter"], ["Pitchers", "pitcher"]].forEach(([lbl, r]) => {
    const c = el("div", "card");
    c.append(el("div", "big", pct(rl[r].rate)),
             el("div", "lbl", `${lbl}: ${share(r)}% of all challenges (${rl[r].n})`));
    rc.append(c);
  });

  const roleCell = (p, r) => {
    const x = p.roles[r];
    return x.n ? `${x.n} <span class="pill">${pct(x.rate)}</span>` : '<span class="mut">-</span>';
  };
  const rows = d.challengers.map(p => ({
    abbr: p.abbr, total: p.total,
    bat: p.roles.batter.n, cat: p.roles.catcher.n, pit: p.roles.pitcher.n,
    _p: p,
    top: p.top ? `${p.top.name} <span class="pill">${p.top.role[0].toUpperCase()} · ${p.top.challenges}</span>` : "-",
  }));
  sortableTable("challenger-table", [
    { key: "abbr", label: "Team", asc: true },
    { key: "total", label: "Total" },
    { key: "cat", label: "Catchers", fmt: (_, r) => roleCell(r._p, "catcher") },
    { key: "bat", label: "Batters", fmt: (_, r) => roleCell(r._p, "batter") },
    { key: "pit", label: "Pitchers", fmt: (_, r) => roleCell(r._p, "pitcher") },
    { key: "top", label: "Most-active challenger", asc: true },
  ], rows, { sortKey: "total" });
}

function umpTable(d) {
  const all = document.getElementById("ump-alln");
  function build() {
    const rows = all.checked ? d.umpires : d.umpires.filter(u => u.challenges >= d.min_ump_chal);
    const elig = d.umpires.filter(u => u.challenges >= d.min_ump_chal);
    const worst = elig[0]?.umpire, best = elig[elig.length - 1]?.umpire;
    sortableTable("ump-table", [
      { key: "umpire", label: "Umpire", asc: true },
      { key: "games", label: "Games" },
      { key: "challenges", label: "Challenges" },
      { key: "overturned", label: "Overturned" },
      { key: "rate", label: "Overturn Rate", cls: "rate", fmt: (v, r) => pct(v) + (r.challenges < d.min_ump_chal ? ' <span class="flag">low n</span>' : "") },
    ], rows, { sortKey: "rate", rowClass: u => u.umpire === worst ? "worst" : u.umpire === best ? "best" : "" });
  }
  all.onchange = build;
  build();
}

function trendChart(d) {
  const t = d.trend;
  charts.trend?.destroy();
  charts.trend = new Chart(document.getElementById("trend"), {
    type: "line",
    data: {
      labels: t.map(p => p.date),
      datasets: [{ data: t.map(p => (p.rate * 100).toFixed(1)), borderColor: "#3fb950", backgroundColor: "rgba(63,185,80,.15)", fill: true, tension: .25, pointRadius: t.length > 30 ? 0 : 3 }]
    },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => c.parsed.y + "% overturned" } } },
      scales: {
        y: { title: { display: true, text: "% overturned", color: "#8b97a6" }, ticks: { color: "#8b97a6" }, grid: { color: "#26303d" }, suggestedMin: 40, suggestedMax: 65 },
        x: { ticks: { color: "#8b97a6", maxTicksLimit: 10 }, grid: { display: false } }
      }
    }
  });
}

function playerTabs(d) {
  const tabs = document.getElementById("player-tabs");
  function show(kind) {
    sortableTable("player-table", [
      { key: "name", label: "Player", asc: true },
      { key: "team", label: "Team", asc: true },
      { key: "challenges", label: "Challenges" },
      { key: "overturned", label: "Overturned" },
      { key: "rate", label: "Success Rate", cls: "rate", fmt: v => pct(v) },
    ], d.players[kind], { sortKey: "rate" });
  }
  tabs.querySelectorAll("button").forEach(b => b.onclick = () => {
    tabs.querySelectorAll("button").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    show(b.dataset.k);
  });
  show("batter");
}
