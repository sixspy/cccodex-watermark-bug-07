(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "batches";
  let templates = [];
  let batches = [];
  let reports = [];
  let verifications = [];
  let tasks = [];
  let selected = null;
  let stampValue = "";
  let traceValue = "WM-ORD-131";
  let traceResult = null;
  // 缺口对账页的读数状态：打开页时冻结快照，读数二选一。
  let gapContext = null;
  let readingChoice = "冻结";
  let activeReportId = null;
  let activeCards = [];
  let cardsError = "";
  let gapBusy = false;

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    return j;
  }

  function setMsg(t) {
    msgEl.textContent = t || "";
  }

  function esc(v) {
    return String(v === null || v === undefined ? "" : v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function refresh() {
    const meta = await api("/api/meta");
    document.getElementById("lab-name").textContent = meta.lab_name || "血缘水印台";
    templates = (await api("/api/templates")).items || [];
    batches = (await api("/api/batches")).items || [];
    reports = (await api("/api/gap-reports")).items || [];
    verifications = (await api("/api/verifications")).items || [];
    tasks = (await api("/api/orchestration-tasks")).items || [];
  }

  async function openGapPage() {
    // “打开页时冻结”：每次进入差额目录都冻结一份作废集合快照；
    // 本页出数时若选“冻结”，用的就是这一份，不随后续作废漂移。
    const ctx = await api("/api/gap-reports/context");
    gapContext = ctx;
    readingChoice = "冻结";
    activeReportId = reports.length ? reports[0].id : null;
    activeCards = activeReportId
      ? await fetchCards(activeReportId)
      : ctx.void_cards || [];
  }

  async function fetchCards(reportId) {
    try {
      const r = await api("/api/gap-reports/" + reportId + "/void-cards");
      cardsError = "";
      return r.void_cards || [];
    } catch (e) {
      cardsError = e.message;
      return [];
    }
  }

  function syncDock() {
    document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
      btn.classList.toggle("on", btn.dataset.tab === tab);
    });
  }

  function readingChip(reading, snapshotId) {
    if (reading !== "冻结" && reading !== "重扫") {
      return `<span class="chip chip-read">旧数·无读数</span>`;
    }
    const label = reading === "冻结" ? "冻结读数" : "重扫读数";
    const extra = reading === "冻结" && snapshotId ? ` · 快照#${snapshotId}` : "";
    return `<span class="chip chip-read chip-${reading === "冻结" ? "frozen" : "rescan"}">${label}${extra}</span>`;
  }

  function renderTemplates() {
    return `
      <h2>水印模板</h2>
      <table>
        <thead><tr><th>表</th><th>列</th><th>版本</th><th>状态</th></tr></thead>
        <tbody>
          ${templates
            .map(
              (t) => `<tr>
            <td>${esc(t.table_name)}</td>
            <td>${esc(t.column_name)}</td>
            <td>${esc(t.version_no)}</td>
            <td><span class="chip">${esc(t.status)}</span></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderBatches() {
    return `
      <h2>运行批次</h2>
      <p class="muted">主链：选批盖印 → 用水印追索下游 → 缺口对账；作废批会进差额读数。</p>
      <table>
        <thead><tr><th>编号</th><th>表</th><th>运行码</th><th>业务日</th><th>状态</th><th>水印</th><th>作废</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr>
            <td>${esc(b.id)}</td>
            <td>${esc(b.table_name)}</td>
            <td>${esc(b.run_code)}</td>
            <td>${esc(b.biz_date)}</td>
            <td><span class="chip">${esc(b.status)}</span></td>
            <td>${esc(b.watermark_value || "-")}</td>
            <td>${esc(b.void_reason || "-")}</td>
            <td>${
              b.status === "待盖印"
                ? `<button type="button" class="ghost" data-pick="${b.id}">盖印</button>`
                : ""
            }${
                b.status !== "已作废"
                  ? ` <button type="button" class="ghost danger" data-void="${b.id}">作废</button>`
                  : ""
              }</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      ${
        selected
          ? `<div class="cardish">
        <h3>盖印</h3>
        <p>${esc(selected.run_code)} · ${esc(selected.table_name)}</p>
        <label>水印值<input id="stamp-input" value="${esc(stampValue)}" /></label>
        <button type="button" id="btn-stamp">写入盖印</button>
      </div>`
          : ""
      }`;
  }

  function renderTrace() {
    const src = (traceResult && traceResult.sources) || [];
    const down = (traceResult && traceResult.downstream) || [];
    return `
      <h2>下游追索</h2>
      <label>水印值<input id="trace-input" value="${esc(traceValue)}" placeholder="例如 WM-ORD-131" /></label>
      <div class="row"><button type="button" id="btn-trace">追索</button></div>
      ${
        traceResult
          ? `<div>
        <h3>源批次</h3>
        <ul>${src.map((s) => `<li>${esc(s.run_code)} · ${esc(s.table_name)}</li>`).join("")}</ul>
        <h3>下游</h3>
        <ul>${down
          .map(
            (d) =>
              `<li>${esc(d.run_code)} · ${esc(d.status)} · ${esc(d.watermark_value || "未盖印")}</li>`
          )
          .join("")}</ul>
      </div>`
          : ""
      }`;
  }

  function renderGapBar() {
    const ctx = gapContext;
    if (!ctx) return `<p class="muted">正在冻结作废集合快照…</p>`;
    const snap = ctx.snapshot;
    const latest = ctx.latest_report;
    const shown =
      (activeReportId && reports.find((r) => r.id === activeReportId)) ||
      latest ||
      null;
    // 漂移按“当前列表里最新一份”实时算，不用打开页时的旧值。
    const newest = reports[0] || latest;
    const drift = newest
      ? [...new Set([...(newest.void_codes || []), ...(snap.void_codes || [])])]
          .filter(
            (c) =>
              (newest.void_codes || []).includes(c) !==
              (snap.void_codes || []).includes(c)
          )
          .sort()
      : [];
    const driftNote = drift.length
      ? `<p class="warn">最新对账单与本页快照的作废集合相差：${esc(
          drift.join("、")
        )}。生成前请确认读数。</p>`
      : "";
    return `
      <div class="gapbin">
        <div class="gapbin-head">
          <strong>差额条</strong>
          ${readingChip(
            readingChoice,
            readingChoice === "冻结" ? snap.snapshot_id : null
          )}
          <span class="muted">本页作废快照 #${esc(snap.snapshot_id)} · ${esc(
            snap.captured_at
          )} · ${esc(snap.void_count)} 批</span>
        </div>
        ${
          shown
            ? `<div class="gapbin-nums">
          <span class="muted">最新第 ${esc(shown.seq)} 序</span>
          ${readingChip(shown.reading)}
          <span>应盖印 <b>${esc(shown.expected_count)}</b></span>
          <span>已盖印 <b>${esc(shown.stamped_count)}</b></span>
          <span>缺口 <b>${esc(shown.gap_count)}</b></span>
          <span>作废 <b>${esc(shown.void_count)}</b> 批</span>
        </div>`
            : `<p class="muted">还没有出过差额数。</p>`
        }
        ${driftNote}
        <div class="row">
          <label class="inline"><input type="radio" name="reading" value="冻结" ${
            readingChoice === "冻结" ? "checked" : ""
          } /> 冻结（用打开页快照）</label>
          <label class="inline"><input type="radio" name="reading" value="重扫" ${
            readingChoice === "重扫" ? "checked" : ""
          } /> 重扫（按提交瞬间作废态）</label>
          <button type="button" id="btn-gap" ${gapBusy ? "disabled" : ""}>${
      gapBusy ? "出数中…" : "生成差额数"
    }</button>
        </div>
      </div>`;
  }

  function renderReportList() {
    return `
      <h3>对账列表</h3>
      <table>
        <thead><tr><th>序</th><th>截至</th><th>读数</th><th>应盖印</th><th>已盖印</th><th>缺口</th><th>作废批</th><th>摘要</th><th></th></tr></thead>
        <tbody>
          ${
            reports.length
              ? reports
                  .map(
                    (r) => `<tr class="${
                      activeReportId === r.id ? "picked" : ""
                    }">
            <td>${esc(r.seq)}</td>
            <td>${esc(r.as_of_date)}</td>
            <td>${readingChip(r.reading)}</td>
            <td>${esc(r.expected_count)}</td>
            <td>${esc(r.stamped_count)}</td>
            <td>${esc(r.gap_count)}</td>
            <td>${esc(r.void_count)}</td>
            <td>${esc(r.summary)}</td>
            <td>
              <button type="button" class="ghost" data-cards="${r.id}">作废卡片</button>
              <button type="button" class="ghost" data-check="${r.id}">核对</button>
              <button type="button" class="ghost" data-verify="${r.id}">复验</button>
            </td>
          </tr>`
                  )
                  .join("")
              : `<tr><td colspan="9" class="muted">暂无对账单</td></tr>`
          }
        </tbody>
      </table>`;
  }

  function renderVoidCards() {
    const cards = activeCards || [];
    const report = reports.find((r) => r.id === activeReportId) || null;
    return `
      <h3>作废卡片 ${report ? readingChip(report.reading) : ""}</h3>
      <p class="muted">${
        report
          ? `按对账单 #${esc(report.id)} 落库的作废明细出卡片；卡片与差额数同源，偏了会拒绝。`
          : "按本页冻结快照的作废明细出卡片。"
      }</p>
      ${
        cardsError
          ? `<p class="warn">${esc(cardsError)}</p>`
          : cards.length
          ? `<div class="cards">${cards
              .map(
                (c) => `<div class="vcard">
              <div class="vcard-code">${esc(c.run_code)}</div>
              <div>${esc(c.table_name)} · ${esc(c.biz_date)}</div>
              <div class="muted">作废于 ${esc(c.voided_at || "-")}</div>
              <div class="muted">原因：${esc(c.void_reason || "-")}</div>
            </div>`
              )
              .join("")}</div>`
          : `<p class="muted">这份读数下没有作废批。</p>`
      }`;
  }

  function renderVerifyProgress() {
    return `
      <h3>复验进度</h3>
      <table>
        <thead><tr><th>复验号</th><th>对账单</th><th>读数</th><th>第一轮缺口</th><th>第二轮缺口</th><th>结论</th><th>明细</th></tr></thead>
        <tbody>
          ${
            verifications.length
              ? verifications
                  .map(
                    (v) => `<tr>
            <td>${esc(v.id)}</td>
            <td>#${esc(v.report_id)}</td>
            <td>${readingChip(v.reading)}</td>
            <td>${esc(v.round1_gap)}</td>
            <td>${esc(v.round2_gap)}</td>
            <td><span class="chip ${v.aligned ? "chip-ok" : "chip-bad"}">${
                      v.aligned ? "两轮对齐" : "未对齐"
                    }</span></td>
            <td>${esc(v.detail)}</td>
          </tr>`
                  )
                  .join("")
              : `<tr><td colspan="7" class="muted">还没有复验记录</td></tr>`
          }
        </tbody>
      </table>`;
  }

  function renderTasks() {
    return `
      <h3>编排任务</h3>
      <table>
        <thead><tr><th>任务</th><th>类型</th><th>对账单</th><th>读数</th><th>序</th><th>状态</th><th>明细</th></tr></thead>
        <tbody>
          ${
            tasks.length
              ? tasks
                  .map(
                    (t) => `<tr>
            <td>${esc(t.task_code)}</td>
            <td>${esc(t.kind)}</td>
            <td>${t.report_id ? "#" + esc(t.report_id) : "-"}</td>
            <td>${t.reading ? readingChip(t.reading) : "-"}</td>
            <td>${esc(t.seq || "-")}</td>
            <td><span class="chip">${esc(t.status)}</span></td>
            <td>${esc(t.detail)}</td>
          </tr>`
                  )
                  .join("")
              : `<tr><td colspan="7" class="muted">还没有编排任务</td></tr>`
          }
        </tbody>
      </table>`;
  }

  function renderGaps() {
    return `
      <h2>缺口对账 · 差额目录</h2>
      ${renderGapBar()}
      ${renderReportList()}
      ${renderVoidCards()}
      ${renderVerifyProgress()}
      ${renderTasks()}`;
  }

  function render() {
    if (tab === "templates") view.innerHTML = renderTemplates();
    else if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "trace") view.innerHTML = renderTrace();
    else view.innerHTML = renderGaps();

    view.querySelectorAll("[data-pick]").forEach((btn) => {
      btn.onclick = () => {
        const id = Number(btn.dataset.pick);
        selected = batches.find((b) => b.id === id) || null;
        stampValue = selected ? "WM-" + selected.run_code.toUpperCase() : "";
        render();
      };
    });
    view.querySelectorAll("[data-void]").forEach((btn) => {
      btn.onclick = async () => {
        const id = Number(btn.dataset.void);
        const reason = window.prompt("作废原因（必填）", "");
        if (reason === null) return;
        setMsg("");
        try {
          await api("/api/batches/" + id + "/void", {
            method: "POST",
            body: JSON.stringify({ reason }),
          });
          await refresh();
          setMsg("批次已作废；差额读数会随后变化");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    });
    const stampInput = document.getElementById("stamp-input");
    if (stampInput) {
      stampInput.oninput = () => (stampValue = stampInput.value);
      document.getElementById("btn-stamp").onclick = async () => {
        setMsg("");
        try {
          await api("/api/batches/" + selected.id + "/stamp", {
            method: "POST",
            body: JSON.stringify({ watermark_value: stampValue }),
          });
          selected = null;
          await refresh();
          setMsg("盖印完成");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
    const traceInput = document.getElementById("trace-input");
    if (traceInput) {
      traceInput.oninput = () => (traceValue = traceInput.value);
      document.getElementById("btn-trace").onclick = async () => {
        setMsg("");
        try {
          traceResult = await api("/api/trace", {
            method: "POST",
            body: JSON.stringify({ watermark_value: traceValue }),
          });
          setMsg("追索完成");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
    view.querySelectorAll('input[name="reading"]').forEach((el) => {
      el.onchange = () => {
        readingChoice = el.value;
        render();
      };
    });
    const gapBtn = document.getElementById("btn-gap");
    if (gapBtn) {
      gapBtn.onclick = async () => {
        if (gapBusy) return;
        gapBusy = true;
        setMsg("");
        render();
        try {
          const payload = {
            as_of_date: "2026-11-16",
            reading: readingChoice,
          };
          if (readingChoice === "冻结") {
            payload.snapshot_id =
              (gapContext && gapContext.snapshot && gapContext.snapshot.snapshot_id) ||
              null;
          }
          const r = await api("/api/gap-reports", {
            method: "POST",
            body: JSON.stringify(payload),
          });
          activeReportId = r.report.id;
          // 出数后不重新冻结：本页读数仍是打开页那一刻的快照。
          await refresh();
          activeCards = await fetchCards(activeReportId);
          setMsg(r.report.summary);
        } catch (e) {
          setMsg(e.message);
        } finally {
          gapBusy = false;
          render();
        }
      };
    }
    view.querySelectorAll("[data-cards]").forEach((btn) => {
      btn.onclick = async () => {
        activeReportId = Number(btn.dataset.cards);
        activeCards = await fetchCards(activeReportId);
        setMsg(cardsError);
        render();
      };
    });
    view.querySelectorAll("[data-check]").forEach((btn) => {
      btn.onclick = async () => {
        setMsg("");
        try {
          const r = await api(
            "/api/gap-reports/" + Number(btn.dataset.check) + "/reconcile",
            { method: "POST", body: JSON.stringify({}) }
          );
          const c = r.recount || {};
          setMsg(
            `核对通过：应盖印 ${c.expected}，已盖印 ${c.stamped}，缺口 ${c.gap}`
          );
        } catch (e) {
          setMsg(e.message);
        }
      };
    });
    view.querySelectorAll("[data-verify]").forEach((btn) => {
      btn.onclick = async () => {
        setMsg("");
        try {
          const r = await api(
            "/api/gap-reports/" + Number(btn.dataset.verify) + "/verify",
            { method: "POST", body: JSON.stringify({}) }
          );
          await refresh();
          setMsg(r.verification.detail);
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    });
  }

  document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
    btn.onclick = async () => {
      tab = btn.dataset.tab;
      syncDock();
      if (tab === "gaps") {
        try {
          await refresh();
          await openGapPage();
        } catch (e) {
          setMsg(e.message);
        }
      }
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(() => {
      syncDock();
      render();
    })
    .catch((e) => setMsg(e.message));
})();
