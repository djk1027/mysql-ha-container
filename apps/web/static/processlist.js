"use strict";
// ================================================================
// 실행 중인 SQL (processlist) 패널
//  - 노드 선택 콤보박스, Sleep/시스템 스레드 필터
//  - 컬럼 너비 드래그 조절 (브라우저에 기억, 더블클릭 시 기본값)
//  - SQL 클릭 → 상세 팝업 (전체 SQL + 세션 정보 + 복사)
// app.js의 $, esc, colorOf, nodeOrder, latest 를 사용한다.
// ================================================================

const PROC_COLUMNS = [
  { key: "node", label: "노드", width: 120 },
  { key: "id", label: "ID", width: 56, num: true },
  { key: "user", label: "User", width: 72 },
  { key: "host", label: "Host", width: 110 },
  { key: "db", label: "DB", width: 64 },
  { key: "command", label: "Command", width: 76 },
  { key: "time", label: "Time", width: 56, num: true },
  { key: "state", label: "State", width: 130 },
  { key: "info", label: "SQL", width: 640 },
];
const PROC_MIN_WIDTH = 40;
const PREF_WIDTHS = "proc-col-widths";
const PREF_NODE = "proc-node";

let procWidths = PROC_COLUMNS.map((c) => c.width);
let procRows = [];   // 현재 표에 그려진 행 (팝업에서 index로 찾음)

// ---------- 브라우저 저장 (실패해도 동작에는 영향 없음) ----------
function prefGet(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function prefSet(key, value) {
  try { localStorage.setItem(key, value); } catch { /* 저장 불가 환경은 무시 */ }
}

function timeClass(p) {
  if (p.system || p.command === "Sleep") return "";
  return p.time >= 30 ? "t-bad" : p.time >= 3 ? "t-warn" : "";
}

// ---------- 컬럼 / 너비 ----------
function applyWidths() {
  const cols = $("#proc-cols").children;
  procWidths.forEach((w, i) => { cols[i].style.width = `${w}px`; });
  $("#proc-table").style.width = `${procWidths.reduce((a, b) => a + b, 0)}px`;
}

function buildHeader() {
  try {
    const saved = JSON.parse(prefGet(PREF_WIDTHS));
    if (Array.isArray(saved) && saved.length === PROC_COLUMNS.length) {
      procWidths = saved.map((w, i) => Number(w) >= PROC_MIN_WIDTH ? Number(w) : PROC_COLUMNS[i].width);
    }
  } catch { /* 저장값이 깨졌으면 기본값 사용 */ }

  $("#proc-cols").innerHTML = PROC_COLUMNS.map(() => "<col>").join("");
  $("#proc-head").innerHTML = PROC_COLUMNS.map((c, i) => `
    <th class="${c.num ? "num" : ""}">
      <span class="th-label">${c.label}</span>
      <span class="col-resizer" data-col="${i}" title="드래그: 너비 조절 · 더블클릭: 기본값"></span>
    </th>`).join("");
  applyWidths();

  const head = $("#proc-head");
  head.addEventListener("pointerdown", (e) => {
    const handle = e.target.closest(".col-resizer");
    if (!handle) return;
    e.preventDefault();
    const i = Number(handle.dataset.col);
    const startX = e.clientX, startW = procWidths[i];
    handle.setPointerCapture(e.pointerId);
    document.body.classList.add("col-resizing");

    const move = (ev) => {
      procWidths[i] = Math.max(PROC_MIN_WIDTH, Math.round(startW + ev.clientX - startX));
      applyWidths();
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
      document.body.classList.remove("col-resizing");
      prefSet(PREF_WIDTHS, JSON.stringify(procWidths));
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  });
  head.addEventListener("dblclick", (e) => {
    const handle = e.target.closest(".col-resizer");
    if (!handle) return;
    const i = Number(handle.dataset.col);
    procWidths[i] = PROC_COLUMNS[i].width;
    applyWidths();
    prefSet(PREF_WIDTHS, JSON.stringify(procWidths));
  });
}

// ---------- 노드 콤보박스 ----------
function syncNodeOptions() {
  const sel = $("#proc-node");
  const current = [...sel.options].slice(1).map((o) => o.value).join(",");
  if (current === nodeOrder.join(",")) return;

  const wanted = sel.value || prefGet(PREF_NODE) || "";
  sel.innerHTML = `<option value="">All</option>` +
    nodeOrder.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  sel.value = nodeOrder.includes(wanted) ? wanted : "";
}

// ---------- 표 ----------
function renderProcesses(snap) {
  if (!snap) return;
  syncNodeOptions();

  const nodeFilter = $("#proc-node").value;
  const showSleep = $("#proc-sleep").checked;
  const showSystem = $("#proc-system").checked;

  const rows = [];
  const failed = [];
  for (const name of nodeOrder) {
    if (nodeFilter && name !== nodeFilter) continue;
    const procs = snap.nodes[name].processes;
    if (procs == null) { failed.push(name); continue; }
    for (const p of procs) {
      if (!showSystem && p.system) continue;
      if (!showSleep && p.command === "Sleep") continue;
      rows.push({ node: name, ts: snap.ts, ...p });
    }
  }
  rows.sort((a, b) => b.time - a.time);
  procRows = rows;

  $("#proc-count").textContent = `${rows.length}건`;

  const cell = (v, cls = "") => {
    const text = v == null || v === "" ? "-" : String(v);
    return `<td class="${cls}" title="${esc(text)}">${esc(text)}</td>`;
  };

  let html = rows.map((p, idx) => `
    <tr class="${p.system ? "system" : ""}">
      <td title="${esc(p.node)}"><span class="node-cell"><i style="background:${colorOf(p.node)}"></i>${esc(p.node)}</span></td>
      ${cell(p.id, "num")}
      ${cell(p.user)}
      ${cell(p.host, "mono")}
      ${cell(p.db)}
      ${cell(p.command)}
      <td class="num ${timeClass(p)}">${p.time ?? "-"}s</td>
      ${cell(p.state, "state")}
      <td class="sql-cell">${p.info
        ? `<button type="button" class="sql-link" data-idx="${idx}">${esc(p.info)}</button>`
        : `<span class="muted">-</span>`}</td>
    </tr>`).join("");

  // 행이 없으면 빈 행 대신 표 가운데에 안내 문구 표시
  const empty = $("#proc-empty");
  empty.hidden = rows.length > 0;
  if (!rows.length) {
    empty.textContent = `실행 중인 SQL 없음${showSleep && showSystem ? "" : " (필터로 숨긴 세션 제외)"}`;
  }
  if (failed.length) {
    html += `<tr><td colspan="${PROC_COLUMNS.length}" class="no">조회 실패: ${failed.map(esc).join(", ")}</td></tr>`;
  }
  $("#proc-body").innerHTML = html;
}

// ---------- SQL 상세 팝업 ----------
function openSqlDialog(p) {
  const meta = [
    ["노드", p.node], ["ID", p.id], ["User", p.user], ["Host", p.host || "-"],
    ["DB", p.db ?? "-"], ["Command", p.command], ["Time", `${p.time}s`],
    ["State", p.state || "-"], ["조회 시각", timeStr(p.ts)],
  ];
  $("#sql-meta").innerHTML = meta.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
  $("#sql-text").textContent = p.info ?? "";
  $("#sql-copied").textContent = "";

  const note = $("#sql-note");
  note.hidden = !p.info_truncated;
  note.textContent = p.info_truncated ? "SQL이 너무 길어 앞부분 4,000자까지만 표시합니다." : "";

  // 팝업은 클릭한 순간의 내용을 보여준다 (실시간 갱신으로 바뀌지 않음)
  $("#sql-dialog").showModal();
}

async function copySql() {
  const text = $("#sql-text").textContent;
  try {
    await navigator.clipboard.writeText(text);
    $("#sql-copied").textContent = "복사됨";
  } catch {
    // 클립보드 API를 못 쓰는 환경: 텍스트를 선택해 두고 직접 복사하게 한다
    const range = document.createRange();
    range.selectNodeContents($("#sql-text"));
    getSelection().removeAllRanges();
    getSelection().addRange(range);
    $("#sql-copied").textContent = "선택됨 · Ctrl+C로 복사";
  }
}

// ---------- 초기화 (app.js init에서 호출) ----------
function initProcesses() {
  buildHeader();

  for (const id of ["proc-sleep", "proc-system"]) {
    const box = $(`#${id}`);
    box.checked = prefGet(id) === "1";
    box.addEventListener("change", () => { prefSet(id, box.checked ? "1" : "0"); renderProcesses(latest); });
  }
  $("#proc-node").addEventListener("change", (e) => {
    prefSet(PREF_NODE, e.target.value);
    renderProcesses(latest);
  });

  $("#proc-body").addEventListener("click", (e) => {
    const link = e.target.closest(".sql-link");
    if (link) openSqlDialog(procRows[Number(link.dataset.idx)]);
  });

  const dialog = $("#sql-dialog");
  dialog.addEventListener("click", (e) => {
    if (e.target === dialog) return dialog.close();          // 바깥(배경) 클릭
    const action = e.target.closest("[data-action]")?.dataset.action;
    if (action === "close") dialog.close();
    if (action === "copy") copySql();
  });
}
