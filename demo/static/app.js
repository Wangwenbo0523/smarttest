const $ = (sel) => document.querySelector(sel);

const state = {
  mode: "buggy",
  useLlm: true,
  tab: "overview",
  result: null,
  compare: null,
  health: null,
  caseQuery: "",
  caseBasis: "",
  resultFilter: "all",
  codeFile: "contract",
};

const PANELS = [
  { id: "overview", label: "概览" },
  { id: "contract", label: "设计契约" },
  { id: "cases", label: "生成用例" },
  { id: "scenarios", label: "业务场景" },
  { id: "results", label: "执行结果" },
  { id: "defects", label: "缺陷归因" },
  { id: "code", label: "生成代码" },
  { id: "compare", label: "修复版对比" },
];

const CODE_FILES = [
  { id: "contract", name: "test_contract_generated.py" },
  { id: "scenario", name: "test_scenarios_generated.py" },
  { id: "scenarios_json", name: "scenarios.json" },
  { id: "business", name: "test_business_generated.py" },
  { id: "conftest", name: "conftest.py" },
];

function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function json(value) {
  try {
    return esc(JSON.stringify(value));
  } catch (err) {
    return esc(String(value));
  }
}

function methodChip(method) {
  const m = String(method || "").toLowerCase();
  return `<span class="chip ${m}">${esc(String(method || "").toUpperCase())}</span>`;
}

async function getJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${url} -> HTTP ${resp.status}`);
  return resp.json();
}

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${url} -> HTTP ${resp.status} ${text.slice(0, 300)}`);
  }
  return resp.json();
}

// ---------------------------------------------------------------- 顶部状态

async function loadHealth() {
  try {
    state.health = await getJSON("/api/health");
  } catch (err) {
    state.health = null;
  }
  renderEnv();
}

function renderEnv() {
  const el = $("#envbar");
  const h = state.health;
  if (!h) {
    el.innerHTML = `<span class="dot warn"></span>无法连接演示服务`;
    return;
  }
  const targetDot = h.target_ready ? "on" : "warn";
  const targetText = h.target_ready
    ? `靶场就绪 · ${esc(h.target_mode || "外部占用")} 模式`
    : "靶场未就绪（首次执行时会自动拉起）";
  const llmText = h.llm_configured
    ? `<span class="dot on"></span>模型已配置 · <b>${esc(h.llm_model || "unknown")}</b>`
    : `<span class="dot off"></span>未配置 SMARTTEST_LLM_API_KEY，语义层走规则推导兜底`;
  el.innerHTML =
    `<span class="dot ${targetDot}"></span>${targetText}` +
    ` &nbsp;|&nbsp; 靶场地址 <b>${esc(h.target_url)}</b>` +
    ` &nbsp;|&nbsp; ${llmText}`;
}

// ---------------------------------------------------------------- 顶部指标

function renderMetrics() {
  const el = $("#metrics");
  const r = state.result;
  if (!r) {
    el.innerHTML = "";
    return;
  }
  const m = r.metrics;
  const fpClass = m.suspected_false_positives === 0 ? "good" : "";
  const cards = [
    { label: "检出真实缺陷", value: m.defect_count, sub: "按根因归并后的缺陷条目", cls: "hero" },
    { label: "疑似假阳性", value: m.suspected_false_positives, sub: "归因判定为「用例自身问题」", cls: fpClass },
    { label: "用例通过率", value: `${m.pass_rate}%`, sub: `${m.cases_passed} 通过 / ${m.cases_failed} 失败`, cls: "" },
    { label: "接口覆盖率", value: `${m.api_coverage}%`, sub: `${m.operations_covered}/${m.operations_total} 个接口`, cls: "" },
    { label: "字段级覆盖", value: m.field_count, sub: "被用例覆盖到约束的字段", cls: "" },
    { label: "用例总数", value: m.cases_total, sub: `契约用例 ${r.case_count} 条 + 业务/场景`, cls: "" },
    { label: "执行耗时", value: `${m.duration}s`, sub: `整条流水线 ${r.duration_s}s`, cls: "" },
  ];
  el.innerHTML = cards
    .map(
      (c) => `<div class="metric ${c.cls}">
        <div class="label">${esc(c.label)}</div>
        <div class="value">${esc(c.value)}</div>
        <div class="sub">${esc(c.sub)}</div>
      </div>`
    )
    .join("");
}

function renderVerdict() {
  const el = $("#verdict");
  const r = state.result;
  if (!r || !r.ok) {
    el.className = "verdict risk hidden";
    el.innerHTML = "";
    return;
  }
  const m = r.metrics;
  const modeText = r.mode === "buggy" ? "缺陷版靶场" : "修复版靶场";
  let title = "";
  let body = "";
  if (r.mode === "buggy") {
    title = `${modeText}：检出 ${m.defect_count} 个真实缺陷，假阳性 ${m.suspected_false_positives} 条`;
    body = `用例 ${m.cases_total} 条、通过率 ${m.pass_rate}%、接口覆盖率 ${m.api_coverage}%，` +
      `语义层提供方为 ${esc(r.provider)}。规则引擎的边界用例命中了契约与实现的偏差。`;
    // 缺陷版报出缺陷是预期结果，用中性色；绿色只留给「修复版零缺陷」这个自证结论
    el.className = m.defect_count > 0 ? "verdict info" : "verdict";
  } else {
    title = `${modeText}：${m.defect_count} 个缺陷、假阳性 ${m.suspected_false_positives} 条`;
    body = `同一套用例、同一个靶场，只把注入的缺陷修掉，报告就从「有缺陷」回落到「零缺陷、零误报」——` +
      `这证明报出来的缺陷来自被测系统，而不是生成器的幻觉。`;
    el.className = m.defect_count > 0 ? "verdict risk" : "verdict";
  }
  el.innerHTML = `<h3>${esc(title)}</h3><p>${body}</p>`;
}

// ---------------------------------------------------------------- 标签页

function renderTabs() {
  const r = state.result;
  const counts = {
    contract: r ? r.contract.operations.length : 0,
    cases: r ? r.cases.length : 0,
    scenarios: r ? r.scenarios.accepted.length : 0,
    results: r ? r.totals.total : 0,
    defects: r ? r.defects.length : 0,
  };
  $("#tabs").innerHTML = PANELS.map((p) => {
    const n = counts[p.id];
    const badge = n === undefined ? "" : `<span class="count">${n}</span>`;
    return `<button data-tab="${p.id}" class="${state.tab === p.id ? "active" : ""}">${esc(p.label)}${badge}</button>`;
  }).join("");
  $("#tabs").querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.tab = btn.dataset.tab;
      renderTabs();
      renderPanel();
    });
  });
}

// ---------------------------------------------------------------- 各面板

function panelOverview(r) {
  const steps = r.steps
    .map(
      (s) => `<li class="${s.status === "fail" ? "fail" : ""}">
        <span class="idx">${s.n}</span>
        <div class="body">
          <div class="title">${esc(s.title)}</div>
          <div class="detail">${esc(s.detail)}</div>
        </div>
      </li>`
    )
    .join("");

  const bases = Object.entries(r.basis_summary || {});
  const max = bases.reduce((acc, [, v]) => Math.max(acc, v), 1);
  const bars = bases
    .map(
      ([name, count]) => `<div class="bar">
        <span class="mono">${esc(name)}</span>
        <span class="track"><span class="fill" style="width:${Math.round((count / max) * 100)}%"></span></span>
        <span class="n">${count}</span>
      </div>`
    )
    .join("");

  const fields = (r.coverage_detail || [])
    .map(
      (c) => `<tr>
        <td class="mono">${esc(c.field)}</td>
        <td class="mono">${esc(c.operation_id)}</td>
        <td>${c.bases.map((b) => `<span class="chip accent">${esc(b)}</span>`).join(" ")}</td>
      </tr>`
    )
    .join("");

  return `
    <div class="card">
      <h3>流水线执行步骤</h3>
      <ul class="steps">${steps}</ul>
    </div>
    <div class="split">
      <div class="card">
        <h3>用例设计方法分布</h3>
        <p class="muted">规则引擎不是「随便生成」，每条用例都挂着可追溯的设计依据。</p>
        <div class="bars">${bars}</div>
      </div>
      <div class="card">
        <h3>字段级覆盖</h3>
        <p class="muted">同一个字段被哪些设计方法覆盖过 —— 漏测就是从这张表里看出来的。</p>
        <div class="table-wrap" style="max-height:360px">
          <table>
            <thead><tr><th>字段</th><th>所属接口</th><th>覆盖的设计方法</th></tr></thead>
            <tbody>${fields}</tbody>
          </table>
        </div>
      </div>
    </div>`;
}

function panelContract(r) {
  if (!r.contract.operations.length) return `<div class="empty">未解析到接口。</div>`;
  const cards = r.contract.operations
    .map((op) => {
      const params = op.parameters.length
        ? `<h4>入参</h4><table><thead><tr><th>参数</th><th>位置</th><th>必填</th><th>约束</th></tr></thead><tbody>
            ${op.parameters
              .map(
                (p) => `<tr><td class="mono">${esc(p.name)}</td><td>${esc(p.location)}</td>
                <td>${p.required ? '<span class="chip bad">必填</span>' : '<span class="chip">可选</span>'}</td>
                <td class="mono">${esc(p.hint)}</td></tr>`
              )
              .join("")}
          </tbody></table>`
        : "";
      const body = op.body_fields.length
        ? `<h4>请求体字段</h4><table><thead><tr><th>字段</th><th>必填</th><th>约束</th></tr></thead><tbody>
            ${op.body_fields
              .map(
                (f) => `<tr><td class="mono">${esc(f.name)}</td>
                <td>${f.required ? '<span class="chip bad">必填</span>' : '<span class="chip">可选</span>'}</td>
                <td class="mono">${esc(f.hint)}</td></tr>`
              )
              .join("")}
          </tbody></table>`
        : "";
      const resp = op.response_fields.length
        ? `<h4>响应字段（${op.success_status}）</h4><table><thead><tr><th>字段</th><th>约束</th></tr></thead><tbody>
            ${op.response_fields
              .map((f) => `<tr><td class="mono">${esc(f.name)}</td><td class="mono">${esc(f.hint)}</td></tr>`)
              .join("")}
          </tbody></table>`
        : "";
      return `<div class="card">
        <h3>${methodChip(op.method)} <span class="mono">${esc(op.path)}</span></h3>
        <p class="muted">operationId <span class="mono">${esc(op.operation_id)}</span> · ${esc(op.summary || "")}</p>
        <p class="muted">成功码 ${esc(op.success_status)} · 声明的错误码 ${
          op.documented_errors.length ? op.documented_errors.map((c) => `<span class="chip">${c}</span>`).join(" ") : "无"
        }</p>
        ${params}${body}${resp}
      </div>`;
    })
    .join("");
  return `
    <div class="card">
      <h3>契约来源</h3>
      <p class="muted">${esc(r.contract.title)} v${esc(r.contract.version)} · 共 ${r.contract.operations.length} 个接口。
      这份契约就是被测服务对外声明的「应该长什么样」，测试用例全部由它推导，不靠人工拍脑袋。</p>
    </div>
    ${cards}`;
}

function panelCases(r) {
  const bases = Array.from(new Set(r.cases.map((c) => c.design_basis.split("=")[0]))).sort();
  const rows = r.cases
    .filter((c) => {
      if (state.caseBasis && !c.design_basis.startsWith(state.caseBasis)) return false;
      if (!state.caseQuery) return true;
      const q = state.caseQuery.toLowerCase();
      return (
        c.case_id.toLowerCase().includes(q) ||
        c.operation_id.toLowerCase().includes(q) ||
        c.description.toLowerCase().includes(q) ||
        c.design_basis.toLowerCase().includes(q)
      );
    })
    .map(
      (c) => `<tr>
        <td class="mono">${esc(c.case_id)}</td>
        <td>${methodChip(c.method)}<br><span class="mono">${esc(c.path)}</span></td>
        <td>${esc(c.description)}</td>
        <td class="mono">${esc(c.expected_status)}</td>
        <td><span class="chip accent">${esc(c.design_basis)}</span></td>
        <td><details><summary>请求体</summary><pre style="max-height:220px">${json(c.payload)}</pre></details></td>
      </tr>`
    )
    .join("");
  return `
    <div class="card">
      <h3>规则引擎生成的契约用例</h3>
      <p class="muted">共 ${r.cases.length} 条。每条都对应 JSON Schema 里的一条约束（必填 / 类型 / 边界 / 长度 / 枚举），
      确定性可复现 —— 同样的契约跑两次，用例完全一致。</p>
      <div style="display:flex;gap:10px;flex-wrap:wrap;margin:12px 0">
        <input id="case-query" placeholder="按用例名 / 接口 / 说明搜索" value="${esc(state.caseQuery)}"
          style="flex:1;min-width:220px;padding:8px 12px;border:1px solid var(--line);border-radius:8px;font-family:inherit" />
        <select id="case-basis" style="padding:8px 12px;border:1px solid var(--line);border-radius:8px;font-family:inherit">
          <option value="">全部设计依据</option>
          ${bases.map((b) => `<option value="${esc(b)}" ${state.caseBasis === b ? "selected" : ""}>${esc(b)}</option>`).join("")}
        </select>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>用例 ID</th><th>接口</th><th>用例说明</th><th>期望码</th><th>设计依据</th><th>数据</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;
}

function panelScenarios(r) {
  const s = r.scenarios;
  const accepted = s.accepted.length
    ? s.accepted
        .map(
          (sc) => `<div class="scenario">
            <div class="head">
              <b>${esc(sc.title)}</b>
              <span class="chip accent">${esc(sc.rationale)}</span>
              <span class="chip">${esc(sc.source)}</span>
              <span class="mono muted">${esc(sc.scenario_id)}</span>
            </div>
            <ol>
              ${sc.steps
                .map(
                  (st) => `<li><span class="mono">${esc(st.operation_id)}</span>
                    → 期望 <span class="chip ${st.expect_status < 400 ? "ok" : "bad"}">${esc(st.expect_status)}</span>
                    ${st.body ? `<div class="kv">body: ${json(st.body)}</div>` : ""}
                    ${Object.keys(st.capture || {}).length ? `<div class="kv">capture: ${json(st.capture)}</div>` : ""}
                    ${Object.keys(st.expect_body || {}).length ? `<div class="kv">expect: ${json(st.expect_body)}</div>` : ""}
                  </li>`
                )
                .join("")}
            </ol>
          </div>`
        )
        .join("")
    : `<div class="empty">本次没有采纳任何场景（语义层可能整体降级）。</div>`;

  const rejected = s.rejected.length
    ? `<div class="table-wrap"><table>
        <thead><tr><th>场景 ID</th><th>拒绝原因（校验层给出，不是静默丢弃）</th></tr></thead>
        <tbody>${s.rejected
          .map((x) => `<tr><td class="mono">${esc(x.scenario_id)}</td><td>${esc(x.reason)}</td></tr>`)
          .join("")}</tbody>
      </table></div>`
    : `<p class="muted">本次没有被拒绝的场景。</p>`;

  const degraded = s.degraded_reason ? `<p class="muted">降级原因：${esc(s.degraded_reason)}</p>` : "";
  const retry = s.retry_attempted
    ? `<span class="chip">回灌拒绝原因重试，补回 ${esc(s.retry_recovered)} 个</span>`
    : "";

  return `
    <div class="card">
      <h3>语义增强层：模型产出数据，不产出代码</h3>
      <p class="muted">提供方 <span class="chip accent">${esc(s.provider)}</span> ${retry}
      采纳 ${s.accepted.length} 个、拒绝 ${s.rejected.length} 个。</p>
      <p class="muted">模型只描述「调哪个接口、传什么、期望什么」，由解释器确定性执行。
      模型编造的接口、契约里没声明的状态码、不存在的资源 ID 都会被校验层拦掉，
      所以模型不可能把语法错误或不存在的接口带进测试套件。</p>
      ${degraded}
    </div>
    ${accepted}
    <div class="card">
      <h3>被拒绝的场景</h3>
      <p class="muted">拒绝原因全部保留 —— 这一栏正好是「提示词怎么迭代」的证据链。</p>
      ${rejected}
    </div>`;
}

function panelResults(r) {
  const showFailedOnly = state.resultFilter === "failed";
  const rows = r.results
    .filter((x) => !showFailedOnly || x.status !== "passed")
    .map(
      (x) => `<tr>
        <td class="mono">${esc(x.name)}</td>
        <td><span class="chip ${x.status === "passed" ? "ok" : "bad"}">${esc(x.status)}</span></td>
        <td class="mono">${esc(x.duration)}s</td>
        <td>${x.message ? `<details><summary>失败详情</summary><pre style="max-height:240px">${esc(x.message)}</pre></details>` : "—"}</td>
      </tr>`
    )
    .join("");
  return `
    <div class="card">
      <h3>执行结果</h3>
      <p class="muted">
        用例 ${r.totals.total} · 通过 ${r.totals.passed} · 失败 ${r.totals.failed} · 错误 ${r.totals.errors}
        · 耗时 ${r.metrics.duration}s。结果取自 junit-xml，不解析 pytest 的文本输出。</p>
      <div style="display:flex;gap:8px;margin:12px 0">
        <button class="btn ${showFailedOnly ? "" : "primary"}" data-filter="all">全部</button>
        <button class="btn ${showFailedOnly ? "primary" : ""}" data-filter="failed">只看失败</button>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>用例</th><th>状态</th><th>耗时</th><th>信息</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;
}

function panelDefects(r) {
  const cards = r.defects.length
    ? r.defects
        .map(
          (d, i) => `<div class="card">
            <h3>${i + 1}. ${esc(d.title)}
              <span class="chip ${d.severity === "high" ? "bad" : "accent"}">${esc(d.severity)}</span>
              <span class="chip accent mono">${esc(d.basis)}</span>
            </h3>
            <p class="muted">修复建议：${esc(d.suggestion)}</p>
            <p class="muted">命中用例 ${d.cases.length} 条：</p>
            <div>${d.cases.map((c) => `<span class="chip mono">${esc(c)}</span>`).join(" ")}</div>
          </div>`
        )
        .join("")
    : `<div class="card"><h3>未发现真实缺陷</h3>
        <p class="muted">所有失败都被归因到环境 / 数据 / 用例自身，没有指向被测系统的缺陷。</p></div>`;

  const others = (r.findings || []).filter((f) => f.category !== "真实缺陷");
  const otherTable = others.length
    ? `<div class="table-wrap"><table>
        <thead><tr><th>用例</th><th>归因分类</th><th>结论</th><th>证据</th></tr></thead>
        <tbody>${others
          .map(
            (f) => `<tr>
              <td class="mono">${esc(f.case_id)}</td>
              <td><span class="chip">${esc(f.category)}</span></td>
              <td>${esc(f.title)}</td>
              <td><details><summary>查看</summary><pre style="max-height:200px">${esc(f.evidence)}</pre></details></td>
            </tr>`
          )
          .join("")}</tbody>
      </table></div>`
    : `<p class="muted">本次没有非缺陷类失败。</p>`;

  return `
    <div class="card">
      <h3>失败归因：测试失败 ≠ 发现缺陷</h3>
      <p class="muted">环境挂了、数据没了、用例自己写错了，在 pytest 眼里都是 failed。
      归因层把它们拆成五类，只有指向被测系统的才计入缺陷 —— 这是误报率能不能压到 0 的关键。</p>
      <p class="muted">本次：真实缺陷 ${r.metrics.defect_count} 个 ·
      非缺陷类失败 ${others.length} 条 · 疑似假阳性 ${r.metrics.suspected_false_positives} 条。</p>
    </div>
    ${cards}
    <div class="card">
      <h3>其余失败（非真实缺陷）</h3>
      ${otherTable}
    </div>
    <div class="card">
      <h3>完整质量报告（Markdown）</h3>
      <details><summary>展开 quality_report.md</summary><pre style="max-height:520px">${esc(r.report_markdown)}</pre></details>
    </div>`;
}

function panelCode(r) {
  const files = CODE_FILES.filter((f) => r.code && r.code[f.id]);
  if (!files.length) return `<div class="empty">本次没有落盘生成代码。</div>`;
  if (!files.some((f) => f.id === state.codeFile)) state.codeFile = files[0].id;
  const current = r.code[state.codeFile] || "";
  const lineCount = current.split("\n").length;
  return `
    <div class="card">
      <h3>生成的 pytest 代码</h3>
      <p class="muted">代码由 Jinja2 模板渲染，模型不参与写代码，只提供场景数据。
      这些文件同时被 CI 门禁直接执行。</p>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin:12px 0">
        ${files
          .map(
            (f) => `<button class="btn ${state.codeFile === f.id ? "primary" : ""}" data-code="${f.id}">
              ${esc(f.name)}</button>`
          )
          .join("")}
      </div>
      <p class="muted mono">${esc(files.find((f) => f.id === state.codeFile).name)} · ${lineCount} 行</p>
      <pre>${esc(current)}</pre>
    </div>`;
}

function panelCompare(r) {
  const c = state.compare;
  if (!c) {
    return `<div class="card">
      <h3>零误报自证</h3>
      <p class="muted">点右上角「一键对比」：同一套用例，先在注入缺陷的靶场上跑一遍，
      再在修复版靶场上跑一遍。真正决定工具能不能用的，是修复版这一列必须干净。</p>
      <p class="muted">当前页面显示的是上一次对比结果之前的状态；点击按钮开始对比。</p>
    </div>`;
  }
  const rows = [
    ["检出真实缺陷", (x) => x.metrics.defect_count],
    ["疑似假阳性", (x) => x.metrics.suspected_false_positives],
    ["用例总数", (x) => x.metrics.cases_total],
    ["通过 / 失败", (x) => `${x.totals.passed} / ${x.totals.failed + x.totals.errors}`],
    ["用例通过率", (x) => `${x.metrics.pass_rate}%`],
    ["接口覆盖率", (x) => `${x.metrics.api_coverage}%`],
    ["字段级覆盖", (x) => x.metrics.field_count],
    ["采纳的业务场景", (x) => x.scenarios.accepted.length],
    ["执行耗时", (x) => `${x.metrics.duration}s`],
  ];
  const table = rows
    .map(
      ([label, fn]) => `<tr>
        <td>${esc(label)}</td>
        <td class="mono">${esc(fn(c.buggy))}</td>
        <td class="mono">${esc(fn(c.fixed))}</td>
      </tr>`
    )
    .join("");
  const clean = c.fixed.metrics.defect_count === 0 && c.fixed.metrics.suspected_false_positives === 0;
  const verdict = clean
    ? `<div class="verdict">
        <h3>零误报自证通过</h3>
        <p>缺陷版检出 ${c.buggy.metrics.defect_count} 个真实缺陷；修复版 0 个缺陷、0 条假阳性。
        说明报出来的缺陷来自被测系统的实现偏差，不是生成器的幻觉。</p>
      </div>`
    : `<div class="verdict risk">
        <h3>对比结果需要人工确认</h3>
        <p>修复版仍有 ${c.fixed.metrics.defect_count} 个缺陷、${c.fixed.metrics.suspected_false_positives} 条假阳性，
        需要检查归因规则。</p>
      </div>`;
  const buggyDefects = c.buggy.defects
    .map((d) => `<li><b>${esc(d.title)}</b> <span class="chip bad mono">${esc(d.basis)}</span>
      <div class="muted">命中 ${d.cases.length} 条用例 · ${esc(d.suggestion)}</div></li>`)
    .join("");
  return `
    ${verdict}
    <div class="card">
      <h3>缺陷版 vs 修复版</h3>
      <table>
        <thead><tr><th>指标</th><th>缺陷版靶场</th><th>修复版靶场</th></tr></thead>
        <tbody>${table}</tbody>
      </table>
    </div>
    <div class="card">
      <h3>缺陷版命中的缺陷清单</h3>
      <ul>${buggyDefects}</ul>
    </div>`;
}

function renderPanel() {
  const el = $("#panel");
  const r = state.result;
  if (state.tab !== "compare" && !r) {
    el.innerHTML = `<div class="empty">点击右上角「开始执行」，跑一次完整流水线。</div>`;
    return;
  }
  let html = "";
  switch (state.tab) {
    case "overview": html = panelOverview(r); break;
    case "contract": html = panelContract(r); break;
    case "cases": html = panelCases(r); break;
    case "scenarios": html = panelScenarios(r); break;
    case "results": html = panelResults(r); break;
    case "defects": html = panelDefects(r); break;
    case "code": html = panelCode(r); break;
    case "compare": html = panelCompare(r); break;
    default: html = "";
  }
  el.innerHTML = html;
  bindPanelEvents();
}

function bindPanelEvents() {
  const q = $("#case-query");
  if (q) {
    q.addEventListener("input", () => {
      state.caseQuery = q.value;
      renderPanel();
      const again = $("#case-query");
      if (again) { again.focus(); again.setSelectionRange(again.value.length, again.value.length); }
    });
  }
  const basis = $("#case-basis");
  if (basis) {
    basis.addEventListener("change", () => {
      state.caseBasis = basis.value;
      renderPanel();
    });
  }
  document.querySelectorAll("[data-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.resultFilter = btn.dataset.filter;
      renderPanel();
    });
  });
  document.querySelectorAll("[data-code]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.codeFile = btn.dataset.code;
      renderPanel();
    });
  });
}

// ---------------------------------------------------------------- 运行控制

let timerHandle = null;

function showOverlay(title, hint) {
  $("#overlay-title").textContent = title;
  $("#overlay").querySelector(".overlay-hint").textContent = hint;
  $("#overlay-log").innerHTML = "";
  $("#overlay").classList.remove("hidden");
  const t0 = Date.now();
  $("#overlay-timer").textContent = "0.0s";
  clearInterval(timerHandle);
  timerHandle = setInterval(() => {
    $("#overlay-timer").textContent = `${((Date.now() - t0) / 1000).toFixed(1)}s`;
  }, 100);
}

function hideOverlay() {
  clearInterval(timerHandle);
  $("#overlay").classList.add("hidden");
}

function setBusy(busy) {
  $("#btn-run").disabled = busy;
  $("#btn-compare").disabled = busy;
}

async function doRun() {
  setBusy(true);
  showOverlay("正在执行流水线", "真实模型调用通常需要 10-40 秒，请勿关闭页面");
  try {
    const result = await postJSON("/api/run", { mode: state.mode, use_llm: state.useLlm });
    if (!result.ok) {
      $("#overlay-log").innerHTML = `<b style="color:#b3261e">执行中断：${esc(result.reason)}</b>`;
      await new Promise((r) => setTimeout(r, 1200));
    }
    state.result = result;
    state.compare = null;
    hideOverlay();
    renderAll();
    await loadHealth();
  } catch (err) {
    $("#overlay-log").innerHTML = `<b style="color:#b3261e">请求失败：${esc(err.message)}</b>`;
    await new Promise((r) => setTimeout(r, 1500));
    hideOverlay();
  } finally {
    setBusy(false);
  }
}

async function doCompare() {
  setBusy(true);
  showOverlay("正在执行一键对比", "缺陷版与修复版各跑一次完整流水线，约 20-80 秒");
  try {
    const data = await postJSON("/api/compare", { mode: "buggy", use_llm: state.useLlm });
    state.result = data.buggy;
    state.compare = data;
    state.tab = "compare";
    hideOverlay();
    renderAll();
    await loadHealth();
  } catch (err) {
    $("#overlay-log").innerHTML = `<b style="color:#b3261e">请求失败：${esc(err.message)}</b>`;
    await new Promise((r) => setTimeout(r, 1500));
    hideOverlay();
  } finally {
    setBusy(false);
  }
}

function renderAll() {
  renderMetrics();
  renderVerdict();
  renderTabs();
  renderPanel();
}

function bindControls() {
  $("#mode-seg").querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.mode = btn.dataset.mode;
      $("#mode-seg").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
    });
  });
  $("#llm-seg").querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.useLlm = btn.dataset.llm === "1";
      $("#llm-seg").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
    });
  });
  $("#btn-run").addEventListener("click", doRun);
  $("#btn-compare").addEventListener("click", doCompare);
}

// 支持用 URL 参数直接进入某个状态，便于分享演示链接与自动截图：
//   /?auto=1&mode=buggy&llm=0&tab=defects
//   /?auto=compare&mode=buggy&llm=0
function applyQueryParams() {
  const params = new URLSearchParams(location.search);
  const mode = params.get("mode");
  if (mode === "buggy" || mode === "fixed") {
    state.mode = mode;
    $("#mode-seg").querySelectorAll("button").forEach((b) => {
      b.classList.toggle("active", b.dataset.mode === mode);
    });
  }
  const llm = params.get("llm");
  if (llm === "0" || llm === "1") {
    state.useLlm = llm === "1";
    $("#llm-seg").querySelectorAll("button").forEach((b) => {
      b.classList.toggle("active", b.dataset.llm === llm);
    });
  }
  const tab = params.get("tab");
  if (tab && PANELS.some((p) => p.id === tab)) state.tab = tab;

  const auto = params.get("auto");
  if (auto === "compare") return doCompare();
  if (auto === "1") return doRun();
  return null;
}

bindControls();
loadHealth();
applyQueryParams();
