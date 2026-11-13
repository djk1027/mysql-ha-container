"use strict";

const MAX_POINTS = 720;
const CHART_WINDOW_SEC = 600;   // 차트는 최근 10분만 표시
const LEVELS = { 10: "DEBUG", 20: "INFO", 30: "WARNING", 40: "ERROR", 50: "CRITICAL" };
const NODE_COLORS = ["var(--n1)", "var(--n2)", "var(--n3)"];

let history = [];
let latest = null;
let nodeOrder = [];
let focusedNode = null;   // 차트에서 클릭으로 강조한 노드 (null = 전체 표시)

const $ = (sel) => document.querySelector(sel);

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function fmt(v, digits = 1) {
  return v === null || v === undefined ? "-" : Number(v).toFixed(digits);
}

// 16:19:01 형식
function timeStr(ts) {
  const d = new Date(ts * 1000);
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((v) => String(v).padStart(2, "0"))
    .join(":");
}

function uptimeStr(sec) {
  if (sec == null) return "-";
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  return d ? `${d}일 ${h}시간` : h ? `${h}시간 ${m}분` : `${m}분`;
}

function colorOf(node) {
  const i = nodeOrder.indexOf(node);
  return NODE_COLORS[(i < 0 ? 0 : i) % NODE_COLORS.length];
}

// ---------------------------------------------------------------- header
function renderHeader(snap) {
  // Failover/Switchover 진행 중에는 Primary가 바뀌는 중이므로 회색 "전환 중"으로 표시
  const switching = Object.values(snap.nodes ?? {}).some((n) => n.configuring?.op === "failover");
  const pill = $("#primary-pill");
  pill.textContent = switching ? "Primary: 전환 중" : `Primary: ${snap.primary ?? "식별 불가"}`;
  pill.classList.toggle("cfg", switching);
  pill.title = switching ? "Failover/Switchover 진행 중" : "";
  renderFreshness();
}

function renderFreshness() {
  if (!latest) return;
  const age = Date.now() / 1000 - latest.ts;
  const stale = age > latest.interval * 3;
  $("#updated").textContent = stale
    ? `⚠ 데이터 지연: ${Math.round(age)}초 전 갱신`
    : `${timeStr(latest.ts)} 갱신`;
  $("#updated").style.color = stale ? "var(--warn)" : "";
}

function setConn(state, text) {
  const pill = $("#conn-pill");
  pill.querySelector(".dot").className = `dot ${state}`;
  pill.querySelector("span").textContent = text;
}

// ---------------------------------------------------------------- alerts
// 상단 바에는 심각도별 건수만 표시하고, 버튼을 누르면 팝업으로 상세 목록을 보여준다
const ALERT_ORDER = ["CRITICAL", "WARNING"];
let alerts = [];

function renderAlerts(snap) {
  // 예전 서버(심각도 없이 issues만 보냄)도 표시되도록 WARNING으로 간주
  alerts = (snap.alerts ?? (snap.issues ?? []).map((message) => ({ level: "WARNING", message })))
    .map(withNodes);
  // 심각도순, 같은 심각도 안에서는 최근 발생순
  alerts = alerts.slice().sort((a, b) =>
    ALERT_ORDER.indexOf(a.level) - ALERT_ORDER.indexOf(b.level) || (b.since ?? 0) - (a.since ?? 0));

  const counts = ALERT_ORDER
    .map((lv) => [lv, alerts.filter((a) => a.level === lv).length])
    .filter(([, n]) => n);
  $("#alert-summary").innerHTML = counts.length
    ? counts.map(([lv, n]) => `<span class="alert-count ${lv}">${lv} ${n}</span>`).join("")
    : `<span class="alert-count none">정상</span>`;
  const btn = $("#alert-btn");
  btn.title = alerts.length ? `알림 ${alerts.length}건 보기` : "알림 없음";

  // 팝업이 열려 있으면 실시간으로 갱신
  if ($("#alert-dialog").open) renderAlertList();
}

// 서버가 nodes를 주지 않으면(재시작 전 예전 서버) 메시지 앞의 "노드명: " 에서 인스턴스를 꺼낸다
function withNodes(a) {
  if (a.nodes?.length) return a;
  const node = nodeOrder.find((n) => a.message.startsWith(`${n}: `));
  return node
    ? { ...a, nodes: [node], message: a.message.slice(node.length + 2) }
    : { ...a, nodes: [] };
}

// 발생 후 경과 시간: "방금", "3분째", "2시간 5분째"
function elapsedStr(since) {
  const sec = Math.max(0, (latest?.ts ?? Date.now() / 1000) - since);
  if (sec < 60) return "방금";
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  return (d ? `${d}일 ${h}시간` : h ? `${h}시간 ${m}분` : `${m}분`) + "째";
}

// 오늘이면 시각만, 아니면 날짜도 표시
function sinceStr(since) {
  const d = new Date(since * 1000);
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay ? timeStr(since) : `${d.getMonth() + 1}/${d.getDate()} ${timeStr(since)}`;
}

function renderAlertList() {
  $("#alert-dialog-count").textContent = alerts.length ? `${alerts.length}건` : "";
  $("#alert-list").innerHTML = alerts.length
    ? alerts.map((a) => `<li>
        <span class="alert-since">${a.since == null ? `<span class="muted">-</span>`
          : `<span class="mono" title="${esc(new Date(a.since * 1000).toLocaleString())}">${sinceStr(a.since)}</span>
             <span class="muted">${elapsedStr(a.since)}</span>`}</span>
        <span class="lv ${esc(a.level)}">${esc(a.level)}</span>
        <span class="alert-nodes">${a.nodes.length
          ? a.nodes.map((n) => `<span class="alert-node"><i style="background:${colorOf(n)}"></i>${esc(n)}</span>`).join("")
          : `<span class="muted">-</span>`}</span>
        <span>${esc(a.message)}</span></li>`).join("")
    : `<li class="muted">현재 이상 없음</li>`;
}

function initAlerts() {
  const dialog = $("#alert-dialog");
  $("#alert-btn").addEventListener("click", () => {
    renderAlertList();
    dialog.showModal();
  });
  dialog.addEventListener("click", (e) => {
    // 닫기 버튼 또는 바깥(backdrop) 클릭 시 닫기
    if (e.target.closest("[data-action=close]") || e.target === dialog) dialog.close();
  });
}

// ---------------------------------------------------------------- nodes
// 서버가 계산한 노드 상태(common.NodeState) → 상태 점 색, 역할 배지
const STATE_DOT = { OK: "ok", DEGRADED: "warn", REPL_BROKEN: "bad", DOWN: "bad", STOPPED: "bad", CONFIGURING: "cfg" };
const OP_LABEL = { failover: "Failover", rejoin: "Rejoin" };

function nodeState(n) {
  // 예전 서버(state 없음)는 접속 여부/이슈로 판단
  const state = n.state ?? (!n.reachable ? "DOWN" : n.issues.length ? "DEGRADED" : "OK");
  const roleCls = n.role === "PRIMARY" ? "primary" : "";
  // Failover/Rejoin 진행 중: 일시적인 다운/복제 끊김 대신 "구성 중"으로 표시
  if (state === "CONFIGURING") {
    const op = OP_LABEL[n.configuring?.op] ?? "구성";
    const since = n.configuring?.since;
    const title = `${op} 진행 중` + (since ? ` (${elapsedStr(since).replace("째", "")} 경과)` : "");
    return { cls: "cfg", role: "CONFIGURING", roleCls: "configuring", state: title, configuring: true };
  }
  if (!n.reachable) return { cls: STATE_DOT[state] ?? "bad", role: state, roleCls: "down", state };
  return { cls: STATE_DOT[state] ?? "warn", role: n.role, roleCls, state };
}

function yesNo(v) {
  return v === "Yes" ? `<span class="yes">Yes</span>` : `<span class="no">${esc(v ?? "-")}</span>`;
}

function fmtBytes(bytes) {
  if (bytes == null) return "-";
  const gib = bytes / 1024 ** 3;
  return gib >= 2 ? `${+gib.toFixed(1)} GiB` : `${Math.round(bytes / 1024 ** 2)} MiB`;
}

// 원형 게이지: 한도 전체를 100%로 보고 얼마나 썼는지 표시
function gauge(name, pct, sub, title) {
  const R = 42, C = 2 * Math.PI * R;
  pct = pct == null ? null : Math.max(0, Math.min(100, pct));
  const level = pct == null ? "" : pct >= 90 ? "bad" : pct >= 70 ? "warn" : "ok";
  // 값이 아주 작아도 0이 아니면 링이 살짝 보이도록 최소 길이 부여
  const len = !pct ? 0 : Math.max(C * pct / 100, 2);
  // 한 자릿수는 소수 1자리, 두 자릿수 이상은 정수
  const label = pct == null ? "-" : pct < 9.95 ? pct.toFixed(1) : Math.round(pct);

  return `
    <div class="gauge ${level}" title="${esc(title)}">
      <svg viewBox="0 0 100 100">
        <circle class="gauge-track" cx="50" cy="50" r="${R}"/>
        <circle class="gauge-value" cx="50" cy="50" r="${R}"
          stroke-dasharray="${len.toFixed(2)} ${C.toFixed(2)}"
          transform="rotate(-90 50 50)"/>
      </svg>
      <div class="gauge-label">
        <span class="muted">${name}</span>
        <b>${label}<small>%</small></b>
      </div>
      <div class="gauge-sub muted">${sub}</div>
    </div>`;
}

function cpuGauge(n) {
  const sub = n.cpu_cores == null ? "" : `${fmt(n.cpu_cores_used, 2)} / ${+n.cpu_cores.toFixed(2)}core`;
  return gauge("CPU", n.cpu, sub,
    `할당 CPU ${n.cpu_cores ?? "-"}core 중 ${fmt(n.cpu_cores_used, 2)}core 사용`);
}

function memGauge(n) {
  const sub = n.mem_used == null ? "" : `${fmtBytes(n.mem_used)} / ${fmtBytes(n.mem_limit)}`;
  const title = n.mem_limited === false
    ? "mem_limit 미설정: 호스트 전체 메모리 대비 사용률"
    : "mem_limit 대비 사용률";
  return gauge("MEM", n.mem_pct, sub + (n.mem_limited === false ? "<br>(제한 없음)" : ""), title);
}

function renderNodes(snap) {
  const html = nodeOrder.map((name) => {
    const n = snap.nodes[name];
    const st = nodeState(n);
    const container = n.container === true ? "실행 중" : n.container === false ? "중지됨" : "확인 불가";
    const r = n.replica;

    const replicaRows = r ? `
      <dt>Source</dt><dd class="mono">${esc(r.source)}</dd>
      <dt>IO / SQL Thread</dt><dd>${yesNo(r.io)} / ${yesNo(r.sql)}</dd>
      <dt>Seconds Behind</dt><dd>${r.seconds_behind ?? "-"}</dd>
    ` : n.reachable ? `<dt>복제</dt><dd>${n.role === "PRIMARY" ? "Source" : "구성 안 됨"}</dd>` : "";

    return `
      <article class="node ${n.reachable || st.configuring ? "" : "down"}" style="--node-color:${colorOf(name)}">
        <div class="node-head">
          <div class="node-name"><i class="dot ${st.cls}" title="${esc(st.state)}"></i>${esc(name)}
            <span class="muted mono">:${n.port}</span></div>
          <span class="role ${st.roleCls}" title="${esc(st.configuring ? st.state : "")}">${esc(st.role ?? "-")}</span>
        </div>

        <div class="node-body">
          ${cpuGauge(n)}
          ${memGauge(n)}
          <div class="metrics">
            <div class="metric"><span class="muted">QPS</span><b>${fmt(n.qps)}</b></div>
            <div class="metric"><span class="muted">Conn</span><b>${n.connections ?? "-"}</b></div>
          </div>
        </div>

        <dl class="kv">
          <dt>Status</dt><dd>${container}</dd>
          <dt>read_only / super</dt><dd>${n.read_only ?? "-"} / ${n.super_read_only ?? "-"}</dd>
          <dt>Uptime</dt><dd>${uptimeStr(n.uptime)}</dd>
          ${replicaRows}
        </dl>

      </article>`;
  }).join("");

  $("#nodes").innerHTML = html;
}

// ---------------------------------------------------------------- charts
function niceMax(v) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  return [1, 2, 2.5, 5, 10].map((m) => m * p).find((m) => m >= v);
}

function lineChart(el, key, { transform = (v) => v, fixedMax = null } = {}) {
  const W = 600, H = 180, L = 40, R = 8, T = 8, B = 20;
  const pts = history;
  if (pts.length < 2) {
    el.innerHTML = `<p class="muted">데이터 수집 중…</p>`;
    return;
  }

  const t0 = pts[0].ts, t1 = pts[pts.length - 1].ts;
  let max = 0;
  for (const p of pts) for (const n of nodeOrder) {
    const v = transform(p.nodes[n]?.[key]);
    if (v != null && v > max) max = v;
  }
  // 눈금 간격을 1·2·2.5·5 단위로 맞춘 뒤 최댓값 = 간격 × 4
  // fixedMax가 있으면 y축 고정 (% 그래프: 0~100), 없으면 데이터에 맞춰 자동
  const step = fixedMax ? fixedMax / 4 : niceMax(max * 1.1 / 4);
  max = step * 4;

  const x = (ts) => L + (ts - t0) / Math.max(1, t1 - t0) * (W - L - R);
  // 축 범위를 벗어난 값(fixedMax 초과, 잘못된 단위 등)이 그래프 밖으로 뚫고 나가지 않도록 고정
  const y = (v) => T + (1 - Math.max(0, Math.min(v, max)) / max) * (H - T - B);

  let svg = "";
  // 눈금 간격에 맞춰 소수 자릿수 결정 (0.2, 0.2, 0.1 처럼 중복 표시 방지)
  const digits = Math.max(0, Math.min(3, -Math.floor(Math.log10(step)) + 1));
  for (let i = 0; i <= 4; i++) {
    const v = max * i / 4, yy = y(v);
    svg += `<line class="grid" x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}"/>`;
    svg += `<text class="axis" x="${L - 6}" y="${yy + 3}" text-anchor="end">${+v.toFixed(digits)}</text>`;
  }
  svg += `<text class="axis" x="${L}" y="${H - 4}">${timeStr(t0)}</text>`;
  svg += `<text class="axis" x="${W - R}" y="${H - 4}" text-anchor="end">${timeStr(t1)}</text>`;

  // null(수집 실패/다운) 구간은 선을 끊어서 표시
  // 강조된 노드는 다른 선에 가려지지 않도록 마지막(맨 위)에 그린다
  const drawOrder = focusedNode
    ? [...nodeOrder.filter((n) => n !== focusedNode), focusedNode]
    : nodeOrder;
  for (const n of drawOrder) {
    let d = "", pen = false;
    for (const p of pts) {
      const v = transform(p.nodes[n]?.[key]);
      if (v == null) { pen = false; continue; }
      d += `${pen ? "L" : "M"}${x(p.ts).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    }
    if (!d) continue;
    const dim = focusedNode && n !== focusedNode ? " dim" : "";
    svg += `<path class="line${dim}" d="${d}" stroke="${colorOf(n)}"/>`;
    // 얇은 선도 클릭하기 쉽도록 투명한 굵은 선을 겹쳐 둔다
    svg += `<path class="hit" d="${d}" data-node="${esc(n)}"><title>${esc(n)}</title></path>`;
  }

  const legend = nodeOrder.map((n) => {
    const state = !focusedNode ? "" : n === focusedNode ? " active" : " dim";
    return `<button type="button" class="legend-item${state}" data-node="${esc(n)}"
      aria-pressed="${n === focusedNode}"><i style="background:${colorOf(n)}"></i>${esc(n)}</button>`;
  }).join("");
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${svg}</svg><div class="legend">${legend}</div>`;
}

function renderCharts() {
  lineChart($("#chart-cpu"), "cpu", { fixedMax: 100 });
  lineChart($("#chart-mem"), "mem", { fixedMax: 100 });
  lineChart($("#chart-qps"), "qps");
  lineChart($("#chart-conn"), "conn");
}

// ---------------------------------------------------------------- events
function renderEvents(events) {
  if (!events.length) return;
  $("#events").innerHTML = events.slice().reverse().map((e) => {
    const lv = LEVELS[e.level] ?? e.level;
    return `<li><span class="muted mono">${timeStr(e.ts)}</span><span class="lv ${lv}">${lv}</span><span>${esc(e.message)}</span></li>`;
  }).join("");
}

// ---------------------------------------------------------------- data flow
// 가장 최근 포인트 기준 CHART_WINDOW_SEC 이전 데이터는 버린다 (개수 상한도 함께 적용)
function trimHistory() {
  if (!history.length) return;
  const cutoff = history[history.length - 1].ts - CHART_WINDOW_SEC;
  const start = history.findIndex((p) => p.ts >= cutoff);
  history.splice(0, Math.max(start, history.length - MAX_POINTS));
}

function applySnapshot(snap) {
  latest = snap;
  nodeOrder = Object.keys(snap.nodes);

  const last = history[history.length - 1];
  if (!last || snap.ts > last.ts) {
    history.push({
      ts: snap.ts,
      nodes: Object.fromEntries(Object.entries(snap.nodes).map(([k, n]) => [k, {
        cpu: n.cpu, mem: n.mem_pct, qps: n.qps, conn: n.connections,
      }])),
    });
    trimHistory();
  }

  renderHeader(snap);
  renderAlerts(snap);
  // 서버가 예전 코드로 돌고 있으면 경고 (이 필드가 없는 더 예전 서버도 포함)
  $("#restart-banner").classList.toggle("hidden", snap.code_changed === false);
  renderNodes(snap);
  renderCharts();
  renderProcesses(snap);
}

let stream = null;

// 서버가 보관 중인 데이터(시계열, 이벤트, 최신 상태)를 다시 받아 화면 전체를 다시 그린다
async function loadAll() {
  const [h, e, c] = await Promise.all([
    fetch("/api/history").then((r) => r.json()),
    fetch("/api/events").then((r) => r.json()),
    fetch("/api/cluster").then((r) => r.json()),
  ]);
  history = h;
  trimHistory();
  renderEvents(e);
  if (c) applySnapshot(c);
  else renderCharts();
}

// EventSource는 연결이 끊기면 자동으로 재접속한다
function connectStream() {
  if (stream) stream.close();
  stream = new EventSource("/api/stream");
  stream.onopen = () => setConn("ok", "실시간");
  stream.onerror = () => setConn("bad", "재연결 중…");
  stream.onmessage = (msg) => {
    const { snapshot, events } = JSON.parse(msg.data);
    applySnapshot(snapshot);
    renderEvents(events);
  };
}

async function refresh() {
  const btn = $("#refresh-btn");
  btn.disabled = true;
  btn.classList.add("spinning");
  try {
    await loadAll();
    connectStream();
  } catch (err) {
    console.warn("새로고침 실패", err);
    setConn("bad", "새로고침 실패");
  } finally {
    // 너무 빨리 끝나도 눌렸다는 게 보이도록 최소 0.4초 유지
    setTimeout(() => { btn.disabled = false; btn.classList.remove("spinning"); }, 400);
  }
}

async function init() {
  try {
    await loadAll();
  } catch (err) {
    console.warn("초기 데이터 로딩 실패", err);
  }
  connectStream();

  setInterval(renderFreshness, 1000);
  $("#refresh-btn").addEventListener("click", refresh);

  // 범례/선 클릭 → 해당 노드만 강조 (다시 클릭하거나 Esc → 전체 표시). 모든 차트에 공통 적용
  $(".charts").addEventListener("click", (e) => {
    const item = e.target.closest("[data-node]");
    if (!item) return;
    focusedNode = focusedNode === item.dataset.node ? null : item.dataset.node;
    renderCharts();
  });

  initAlerts();
  initProcesses();   // processlist.js
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && focusedNode) {
      focusedNode = null;
      renderCharts();
    }
  });
}

// ---------------------------------------------------------------- theme
// 기본은 다크. 라이트로 바꾸면 <html data-theme="light"> + localStorage 에 저장
function initTheme() {
  const btn = $("#theme-btn");
  const sync = () => {
    const label = document.documentElement.dataset.theme === "light" ? "다크 모드로 전환" : "라이트 모드로 전환";
    btn.title = label;
    btn.setAttribute("aria-label", label);
  };
  btn.addEventListener("click", () => {
    const light = document.documentElement.dataset.theme !== "light";
    if (light) document.documentElement.dataset.theme = "light";
    else delete document.documentElement.dataset.theme;
    try { localStorage.setItem("theme", light ? "light" : "dark"); } catch (e) { /* 저장 불가 환경 무시 */ }
    sync();
  });
  sync();
}

initTheme();
init();
