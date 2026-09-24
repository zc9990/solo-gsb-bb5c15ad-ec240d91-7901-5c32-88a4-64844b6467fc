/* Address Matcher - vanilla JS front end. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const STATUS_LABEL = {
    auto_matched: "自动命中",
    needs_review: "待确认",
    no_match: "无匹配",
    reviewed: "已复核",
};

const DEMO_QUERIES = [
    "123 Main Street, New York, NY 10001",
    "1000 Lake Dr, Austin, TX",
    "45 Hillcrest Rd, Dallas, Texas 75201",
    "99 SW 26th Ave Unit 7B, Portland OR",
    "北京東路８８號 3楼, 上海",
    "742 Evergreen Tce, Springfield",
].join("\n");

// ------------------------------------------------------------------ helpers

function escapeHtml(value) {
    if (value === null || value === undefined) return "";
    return String(value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

async function api(path, options = {}) {
    const res = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `请求失败 (${res.status})`);
    return data;
}

function pct(value) {
    return value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
}

// ------------------------------------------------------------------- render

function renderTokens(cand) {
    // Group token chips by component so street and locality hits are distinct.
    const groups = [
        { key: "street_tokens", label: "道路", tokens: cand.street_tokens },
        { key: "locality_tokens", label: "地区", tokens: cand.locality_tokens },
    ];
    const rendered = new Set();
    const parts = [];
    groups.forEach((group) => {
        if (!group.tokens) return;
        const exact = new Set(group.tokens.matched.map((m) => m.candidate));
        group.tokens.matched.forEach((m) => {
            const cls = m.similarity >= 0.999 ? "tok" : "tok fuzzy";
            const title = `${group.label}${m.similarity >= 0.999 ? "完全命中" : "近似命中"}: “${m.query}” → “${m.candidate}” (${pct(m.similarity)})`;
            rendered.add(m.candidate);
            parts.push(`<span class="${cls}" title="${escapeHtml(title)}">${escapeHtml(m.candidate)}</span>`);
        });
        group.tokens.query_only.forEach((t) => {
            parts.push(`<span class="tok miss-q" title="${group.label}：仅查询中出现">${escapeHtml(t)}</span>`);
        });
        group.tokens.candidate_only.forEach((t) => {
            if (rendered.has(t)) return;
            parts.push(`<span class="tok miss-c" title="${group.label}：仅标准地址中出现">${escapeHtml(t)}</span>`);
            rendered.add(t);
        });
    });
    return `<div class="tokens">${parts.join("") || '<span class="muted">无命中词元</span>'}</div>`;
}

function renderConflicts(conflicts) {
    if (!conflicts || conflicts.length === 0) return "";
    // Deduplicate identical messages.
    const seen = new Set();
    return conflicts.filter((c) => {
        const key = `${c.field}:${c.query}:${c.candidate}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
    }).map((c) => `
        <div class="conflict">
            ⚠ ${escapeHtml(c.message)}：查询 <b>${escapeHtml(c.query)}</b>
            ↔ 候选 <b>${escapeHtml(c.candidate)}</b>
            （相似度 ${pct(c.similarity)}）
        </div>`).join("");
}

const CONSENSUS_STATE_HINT = {
    hit: "共识命中",
    conflict: "历史结论冲突",
    target_gone: "共识地址已失效",
    not_candidate: "共识地址不在候选",
    not_applied: "共识未改写原判",
    insufficient: "",
};

function renderConsensus(consensus) {
    if (!consensus) return "";
    const n = consensus.evidence_count || 0;
    const targetText = consensus.target
        ? `标准地址 #${consensus.target.id} ${escapeHtml(consensus.target.full_text || "")}`
        : (consensus.target_id ? `标准地址 #${consensus.target_id}` : "");
    if (consensus.applied) {
        return `
        <div class="consensus hit">
            <span class="consensus-badge">✅ 复核共识命中</span>
            <span class="consensus-meta">证据 <b>${n}</b> 条已复核记录一致确认 → ${targetText}</span>
            <div class="consensus-reason">${escapeHtml(consensus.reason || "")}</div>
        </div>`;
    }
    const label = CONSENSUS_STATE_HINT[consensus.state];
    if (!label) return "";
    return `
    <div class="consensus blocked">
        <span class="consensus-badge">⛔ ${escapeHtml(label)}</span>
        <span class="consensus-meta">证据 <b>${n}</b> 条${consensus.rejected_count ? `，拒绝 <b>${consensus.rejected_count}</b> 条` : ""}，至少需 <b>${consensus.required || 2}</b> 条同向确认</span>
        <div class="consensus-reason">${escapeHtml(consensus.reason || "")}</div>
    </div>`;
}

function renderCandidate(cand, rank, opts = {}) {
    const a = cand.address;
    const bestCls = rank === 0 ? " best" : "";
    const actionAttrs = opts.reviewable
        ? `data-log="${opts.logId}" data-address="${a.id}"`
        : "";
    const ribbon = opts.consensusTargetId === a.id
        ? `<span class="target-ribbon">共识指向</span>`
        : "";
    return `
    <div class="candidate${bestCls}">
        <div class="addr">#${rank + 1} ${escapeHtml(a.full_text)} ${ribbon}</div>
        <div class="meta">
            标准地址 ID ${a.id}${a.external_id ? ` · 外部编号 ${escapeHtml(a.external_id)}` : ""}
            ${a.house ? ` · 门牌号 ${escapeHtml(a.house)}` : ""}
        </div>
        <div class="scores">
            <span class="score-chip">综合 <b>${pct(cand.score)}</b></span>
            <span class="score-chip">道路词元 <b>${pct(cand.street_f1)}</b></span>
            <span class="score-chip">地区词元 ${pct(cand.locality_f1)}</span>
            <span class="score-chip">门牌号 <b>${pct(cand.house_score)}</b></span>
        </div>
        ${renderTokens(cand)}
        ${renderConflicts(cand.conflicts)}
        ${opts.reviewable ? `
        <div class="review-actions">
            <button class="tiny confirm candidate-confirm" ${actionAttrs}>确认此候选</button>
            ${rank !== 0 ? `<button class="tiny override candidate-override" ${actionAttrs}>改派为该候选</button>` : ""}
        </div>` : ""}
    </div>`;
}

function renderResult(item, opts = {}) {
    const consensusTargetId = item.consensus && item.consensus.applied
        ? item.consensus.target_id
        : null;
    const candidates = (item.candidates || [])
        .map((c, i) => renderCandidate(c, i, { ...opts, consensusTargetId }))
        .join("");
    return `
    <div class="result">
        <div class="query-line">🔍 ${escapeHtml(item.query)}</div>
        <div class="norm">
            归一化：${escapeHtml(item.normalized.text || "（空）")}
            · 门牌号 <b>${escapeHtml(item.normalized.house || "无")}</b>
            ${item.match_id ? ` · 记录 #${item.match_id}` : ""}
        </div>
        <span class="status ${item.status}">${STATUS_LABEL[item.status] || item.status}</span>
        <span class="muted">${escapeHtml(item.reason)}</span>
        ${renderConsensus(item.consensus)}
        ${item.runner_up_id ? `<div class="muted" style="margin-top:4px">次高分候选 #${item.runner_up_id}：${pct(item.runner_up_score)}（领先 ${pct((item.best_score ?? 0) - (item.runner_up_score ?? 0))}）</div>` : ""}
        ${candidates || '<div class="meta muted">标准地址表中没有任何候选</div>'}
        ${opts.reviewable && item.candidates.length > 0 ? `
        <div class="review-actions">
            <button class="tiny reject candidate-reject" data-log="${item.match_id}">标记为无匹配</button>
        </div>` : ""}
    </div>`;
}

// -------------------------------------------------------------------- tabs

$$(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
        $$(".tab").forEach((b) => b.classList.remove("active"));
        $$(".panel").forEach((p) => p.classList.remove("active"));
        btn.classList.add("active");
        $(`#tab-${btn.dataset.tab}`).classList.add("active");
        if (btn.dataset.tab === "table") loadAddresses();
        if (btn.dataset.tab === "review") loadReview();
        if (btn.dataset.tab === "experiment") {
            renderProfileEditor();
            loadExperimentHistory();
        }
    });
});

// ------------------------------------------------------------------ matching

$("#btn-demo").addEventListener("click", () => { $("#query").value = DEMO_QUERIES; });

$("#btn-match").addEventListener("click", async () => {
    const raw = $("#query").value.trim();
    if (!raw) return;
    const queries = raw.split("\n").map((q) => q.trim()).filter(Boolean);
    const btn = $("#btn-match");
    btn.disabled = true;
    $("#results").innerHTML = '<p class="muted">匹配中…</p>';
    try {
        if (queries.length === 1) {
            const item = await api("/api/match", {
                method: "POST",
                body: JSON.stringify({ query: queries[0], save: true }),
            });
            $("#results").innerHTML = renderResult(item, {
                reviewable: item.status === "needs_review",
            });
            $("#match-summary").textContent = "";
        } else {
            const data = await api("/api/match/batch", {
                method: "POST",
                body: JSON.stringify({ queries, save: true }),
            });
            const s = data.summary;
            $("#match-summary").innerHTML =
                `<span class="muted">共 ${s.total} 条：自动命中 ${s.auto_matched}，` +
                `待确认 ${s.needs_review}，无匹配 ${s.no_match}</span>`;
            $("#results").innerHTML = data.results
                .map((item) => renderResult(item, { reviewable: item.status === "needs_review" }))
                .join("");
        }
        refreshHealth();
    } catch (err) {
        $("#results").innerHTML = `<p class="err-msg">${escapeHtml(err.message)}</p>`;
    } finally {
        btn.disabled = false;
    }
});

// Delegate review clicks from freshly rendered match results.
$("#results").addEventListener("click", onReviewClick);

async function onReviewClick(event) {
    const btn = event.target.closest("button[data-log]");
    if (!btn) return;
    const logId = Number(btn.dataset.log);
    let body;
    if (btn.classList.contains("candidate-reject")) {
        body = { action: "rejected" };
    } else {
        body = {
            action: btn.classList.contains("candidate-override") ? "overridden" : "confirmed",
            selected_id: Number(btn.dataset.address),
        };
    }
    btn.disabled = true;
    try {
        await api(`/api/matches/${logId}/review`, { method: "POST", body: JSON.stringify(body) });
        btn.textContent = "✓ 已记录";
        btn.classList.add("confirm");
        await refreshHealth();
    } catch (err) {
        btn.disabled = false;
        alert(err.message);
    }
}

// ----------------------------------------------------------- address table

async function loadAddresses() {
    const wrap = $("#address-table-wrap");
    wrap.innerHTML = '<p class="muted">加载中…</p>';
    try {
        const data = await api("/api/addresses?limit=200");
        if (data.items.length === 0) {
            wrap.innerHTML = '<p class="muted">标准地址表为空，请上传 CSV 或加载示例数据。</p>';
            return;
        }
        const rows = data.items.map((a) => `
            <tr>
                <td>${a.id}</td>
                <td>${escapeHtml(a.full_text)}</td>
                <td>${escapeHtml(a.house || "")}</td>
                <td>${escapeHtml(a.postal_code || "")}</td>
                <td>${escapeHtml(a.external_id || "")}</td>
            </tr>`).join("");
        wrap.innerHTML = `
            <table>
                <thead><tr><th>ID</th><th>标准地址</th><th>门牌号</th><th>邮编</th><th>外部编号</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
            <p class="muted">共 ${data.total} 条${data.total > 200 ? "（仅显示前 200 条）" : ""}</p>`;
    } catch (err) {
        wrap.innerHTML = `<p class="err-msg">${escapeHtml(err.message)}</p>`;
    }
}

$("#btn-refresh-table").addEventListener("click", loadAddresses);

$("#btn-sample").addEventListener("click", async () => {
    try {
        const s = await api("/api/addresses/load-sample", { method: "POST" });
        $("#table-msg").innerHTML =
            `<span class="ok-msg">新增 ${s.inserted} 条，跳过重复 ${s.duplicates} 条</span>`;
        loadAddresses();
        refreshHealth();
    } catch (err) {
        $("#table-msg").innerHTML = `<span class="err-msg">${escapeHtml(err.message)}</span>`;
    }
});

// ---------------------------------------------------- CSV preview / confirm

// The file picked for the current pending preview (kept so confirm can
// re-submit the exact same bytes to the server).
let pendingFile = null;
let pendingToken = null;

function clearPreview() {
    pendingFile = null;
    pendingToken = null;
    $("#csv-preview-panel").classList.add("hidden");
    $("#csv-preview-panel").innerHTML = "";
}

$("#csv-preview-mode").addEventListener("change", () => {
    clearPreview();
    $("#csv-file").value = "";
});

function csvForm(file) {
    const form = new FormData();
    form.append("file", file);
    return form;
}

function renderPreview(data) {
    const lines = data.invalid_lines.length
        ? data.invalid_lines.map((n) => `第 ${n} 行`).join("、")
        : "无";
    $("#csv-preview-panel").innerHTML = `
        <div class="preview-head">
            <span class="preview-title">📄 ${escapeHtml(data.filename)} 预览结果（共 ${data.total} 行）</span>
        </div>
        <div class="preview-counts">
            <span class="pv-chip valid">有效 ${data.valid}</span>
            <span class="pv-chip dup">重复 ${data.duplicates}</span>
            <span class="pv-chip invalid">无效 ${data.invalid}</span>
        </div>
        <div class="preview-lines">无效行（CSV 物理行号，表头为第 1 行）：${escapeHtml(lines)}</div>
        <p class="hint">
            确认时需重新上传同一文件；若文件内容或标准地址表在预览后发生变化，
            服务器将拒绝写入，需要重新预览。
        </p>
        <div class="row">
            <button id="btn-csv-confirm" class="primary"${data.valid === 0 ? " disabled" : ""}>
                确认写入 ${data.valid} 条有效记录
            </button>
            <button id="btn-csv-cancel" class="ghost">取消</button>
            <button id="btn-csv-repreview" class="ghost hidden">重新预览</button>
            <span id="csv-confirm-msg"></span>
        </div>`;
    $("#csv-preview-panel").classList.remove("hidden");
}

async function createPreview(file) {
    const panel = $("#csv-preview-panel");
    panel.classList.remove("hidden");
    panel.innerHTML = '<p class="muted">正在校验预览…</p>';
    const res = await fetch("/api/addresses/preview", {
        method: "POST",
        body: csvForm(file),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "预览失败");
    pendingFile = file;
    pendingToken = data.preview_token;
    renderPreview(data);
}

$("#csv-file").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    $("#table-msg").innerHTML = "";
    clearPreview();
    try {
        if ($("#csv-preview-mode").checked) {
            await createPreview(file);
            // Keep the filename in the input so users can see what is pending;
            // confirm re-reads it and re-submits the bytes.
        } else {
            await directUpload(file);
        }
    } catch (err) {
        $("#table-msg").innerHTML = `<span class="err-msg">${escapeHtml(err.message)}</span>`;
        clearPreview();
        event.target.value = "";
    }
});

async function directUpload(file) {
    const res = await fetch("/api/addresses/upload", {
        method: "POST",
        body: csvForm(file),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "上传失败");
    $("#table-msg").innerHTML =
        `<span class="ok-msg">${escapeHtml(file.name)}：新增 ${data.inserted} 条，` +
        `重复 ${data.duplicates} 条，无效 ${data.invalid} 条</span>`;
    $("#csv-file").value = "";
    loadAddresses();
    refreshHealth();
}

$("#csv-preview-panel").addEventListener("click", async (event) => {
    if (event.target.closest("#btn-csv-cancel")) {
        clearPreview();
        $("#csv-file").value = "";
        return;
    }
    if (event.target.closest("#btn-csv-repreview")) {
        if (pendingFile) await createPreview(pendingFile);
        return;
    }
    const confirmBtn = event.target.closest("#btn-csv-confirm");
    if (!confirmBtn) return;

    // Confirm must re-submit the file alongside the preview token.
    const file = $("#csv-file").files[0];
    const msg = $("#csv-confirm-msg");
    if (!file) {
        msg.innerHTML = '<span class="err-msg">请重新选择同一个文件后再确认</span>';
        return;
    }
    confirmBtn.disabled = true;
    const form = csvForm(file);
    form.append("preview_token", pendingToken || "");
    try {
        const res = await fetch("/api/addresses/confirm", {
            method: "POST",
            body: form,
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            // Digest/snapshot mismatch or expired voucher: require re-preview.
            msg.innerHTML =
                `<span class="err-msg">${escapeHtml(data.detail || "确认失败")}，` +
                `请点击「重新预览」后再确认。</span>`;
            $("#btn-csv-confirm").classList.add("hidden");
            $("#btn-csv-repreview").classList.remove("hidden");
            return;
        }
        const lines = (data.invalid_lines || []).length
            ? `，无效行：${data.invalid_lines.map((n) => `第 ${n} 行`).join("、")}`
            : "";
        $("#table-msg").innerHTML =
            `<span class="ok-msg">${escapeHtml(data.filename)}：确认写入 ${data.inserted} 条，` +
            `重复 ${data.duplicates} 条，无效 ${data.invalid} 条${lines}</span>`;
        clearPreview();
        $("#csv-file").value = "";
        loadAddresses();
        refreshHealth();
    } catch (err) {
        msg.innerHTML = `<span class="err-msg">${escapeHtml(err.message)}</span>`;
        confirmBtn.disabled = false;
    }
});

// ------------------------------------------------------------- review queue

async function loadReview() {
    const status = $("#review-filter").value;
    const list = $("#review-list");
    list.innerHTML = '<p class="muted">加载中…</p>';
    if (status === "needs_review") {
        loadReviewGroups();
    } else {
        $("#review-groups").innerHTML = "";
    }
    try {
        const data = await api(`/api/matches?status=${encodeURIComponent(status)}&limit=100`);
        if (data.items.length === 0) {
            list.innerHTML = '<p class="muted">暂无记录。</p>';
            return;
        }
        list.innerHTML = data.items.map((item) => {
            const best = item.candidates[0];
            const consensusTargetId = item.consensus && item.consensus.applied
                ? item.consensus.target_id
                : null;
            const candidateHtml = item.candidates.length
                ? item.candidates.map((c, i) => renderCandidate(c, i, {
                    reviewable: item.status === "needs_review",
                    logId: item.id,
                    consensusTargetId,
                })).join("")
                : '<div class="meta muted">无候选</div>';
            return `
            <div class="review-item">
                <div class="query-line">🔍 ${escapeHtml(item.query)}</div>
                <div class="muted">记录 #${item.id} · ${escapeHtml(item.created_at || "")}</div>
                <span class="status ${item.status}">${STATUS_LABEL[item.status] || item.status}</span>
                ${item.review_action ? `<span class="muted">复核动作：${escapeHtml(item.review_action)}</span>` : ""}
                ${renderConsensus(item.consensus)}
                ${candidateHtml}
                ${item.status === "needs_review" ? `
                <div class="review-actions">
                    <button class="tiny reject candidate-reject" data-log="${item.id}">标记为无匹配</button>
                </div>` : ""}
            </div>`;
        }).join("");
    } catch (err) {
        list.innerHTML = `<p class="err-msg">${escapeHtml(err.message)}</p>`;
    }
}

$("#btn-refresh-review").addEventListener("click", loadReview);
$("#review-filter").addEventListener("change", loadReview);
$("#review-list").addEventListener("click", async (event) => {
    await onReviewClick(event);
    if (event.target.closest("button[data-log]")) loadReview();
});

// ------------------------------------------------- same-case merged review

// Groups fetched by the last loadReviewGroups() call, keyed lookup for actions.
let reviewGroups = [];

function renderGroup(group) {
    const queries = group.queries.map((m) => `
        <div class="gq">🔍 ${escapeHtml(m.query)}
            <span class="muted">记录 #${m.id}</span>
        </div>`).join("");
    const candidates = group.candidates
        .map((c, i) => renderCandidate(c, i, {}))
        .join("") || '<div class="meta muted">无候选</div>';
    const best = group.candidates[0];
    const options = group.candidates.map((c) =>
        `<option value="${c.address.id}">#${c.address.id} · ${pct(c.score)} · ` +
        `${escapeHtml(c.address.full_text)}</option>`).join("");
    return `
    <div class="review-group" data-group="${escapeHtml(group.group_key)}">
        <div class="group-head">
            <span class="group-badge">同案组 × ${group.count}</span>
            <span class="muted">
                归一化：道路 [${escapeHtml(group.street_tokens.join(" ") || "—")}]
                · 地区 [${escapeHtml(group.locality_tokens.join(" ") || "—")}]
                · 门牌号 ${escapeHtml(group.house || "无")}
            </span>
        </div>
        <div class="group-queries">${queries}</div>
        ${candidates}
        <div class="review-actions">
            ${best ? `<button class="tiny confirm group-action" data-action="confirmed">
                整组确认首选（#${best.address.id}）</button>` : ""}
            ${options ? `
                <select class="group-select">${options}</select>
                <button class="tiny override group-action" data-action="overridden">整组改派所选候选</button>` : ""}
            <button class="tiny reject group-action" data-action="rejected">整组判无匹配</button>
        </div>
    </div>`;
}

async function loadReviewGroups() {
    const box = $("#review-groups");
    box.innerHTML = '<p class="muted">加载同案组…</p>';
    try {
        const data = await api("/api/review-groups");
        reviewGroups = data.groups || [];
        const merged = reviewGroups.filter((g) => g.count >= 2);
        if (merged.length === 0) {
            box.innerHTML = '<p class="muted">暂无可合并的同案组：归一化后道路/地区词元、' +
                '门牌号一致且候选（ID、顺序、得分）完全相同的待确认记录会归到同组。</p>';
            return;
        }
        box.innerHTML =
            `<h3 class="group-title">同案合并复核（${merged.length} 组）</h3>` +
            merged.map(renderGroup).join("");
    } catch (err) {
        box.innerHTML = `<p class="err-msg">${escapeHtml(err.message)}</p>`;
    }
}

$("#review-groups").addEventListener("click", async (event) => {
    const btn = event.target.closest("button.group-action");
    if (!btn) return;
    const card = btn.closest(".review-group");
    const key = card.dataset.group;
    const group = reviewGroups.find((g) => g.group_key === key);
    if (!group) return;
    const action = btn.dataset.action;
    const body = { group_key: key, match_ids: group.match_ids, action };
    if (action === "overridden") {
        body.selected_id = Number(card.querySelector(".group-select").value);
    }
    btn.disabled = true;
    try {
        const res = await api("/api/review-groups/review", {
            method: "POST",
            body: JSON.stringify(body),
        });
        await refreshHealth();
        loadReview();
    } catch (err) {
        // 409：组签名或成员集合已变化，一条也未写入，刷新展示最新状态。
        alert(err.message);
        loadReview();
    }
});

// ------------------------------------------------------------------ health

async function refreshHealth() {
    try {
        const h = await api("/api/health");
        $("#health").textContent = `标准地址 ${h.address_count} 条 · 待确认 ${h.needs_review}`;
        $("#health").className = "badge ok";
        $("#addr-count").textContent = h.address_count;
        $("#review-count").textContent = h.needs_review;
        if (h.default_thresholds) window.__defaultThresholds = h.default_thresholds;
    } catch (err) {
        $("#health").textContent = "服务不可用";
        $("#health").className = "badge bad";
    }
}

refreshHealth();

// ========================================================== A/B experiments

const AB_FIELDS = [
    { key: "auto_threshold", label: "自动命中阈值", step: "0.01" },
    { key: "reject_threshold", label: "拒绝阈值", step: "0.01" },
    { key: "margin_threshold", label: "分差阈值", step: "0.01" },
    { key: "token_weight", label: "词元权重", step: "0.05" },
];

const AB_DEMO = {
    profiles: [
        { name: "基线A", auto_threshold: 0.8, reject_threshold: 0.35, margin_threshold: 0.06, token_weight: 0.7 },
        { name: "激进B", auto_threshold: 0.7, reject_threshold: 0.2, margin_threshold: 0.02, token_weight: 0.8 },
    ],
    queries: [
        "123 Main Street, New York, NY 10001",
        "1010 Lake Dr, Austin TX",
        "Hillcrest Road, Dallas",
        "北京東路８８號 3楼, 上海",
        "1 Milky Way, Mars Base",
    ].join("\n"),
};

// The editor keeps at most 4 profile cards; initialised lazily on first open.
let abProfiles = null;

function defaultThresholds() {
    return window.__defaultThresholds ||
        { auto_threshold: 0.8, reject_threshold: 0.35, margin_threshold: 0.06, token_weight: 0.7 };
}

function renderProfileEditor() {
    if (abProfiles === null) {
        const t = defaultThresholds();
        abProfiles = [
            { name: "基线A", ...t },
            { name: "宽松B", auto_threshold: 0.7, reject_threshold: 0.25,
              margin_threshold: 0.03, token_weight: t.token_weight },
        ];
    }
    const box = $("#ab-profiles");
    box.innerHTML = abProfiles.map((p, i) => `
        <div class="ab-profile" data-index="${i}">
            <div class="ab-profile-head">
                <span class="ab-letter">${"ABCD"[i]}</span>
                <input class="ab-name" type="text" maxlength="40"
                       value="${escapeHtml(p.name)}" placeholder="档案名称">
                ${abProfiles.length > 2 ?
                    `<button class="tiny reject ab-remove" type="button">移除</button>` : ""}
            </div>
            <div class="ab-fields">
                ${AB_FIELDS.map((f) => `
                <label class="ab-field">
                    <span>${f.label}</span>
                    <input type="number" min="0" max="1" step="${f.step}"
                           data-field="${f.key}" value="${p[f.key]}">
                </label>`).join("")}
            </div>
        </div>`).join("");
    $("#btn-ab-add").disabled = abProfiles.length >= 4;
}

function readProfileEditor() {
    return $$("#ab-profiles .ab-profile").map((card) => {
        const profile = { name: card.querySelector(".ab-name").value };
        AB_FIELDS.forEach((f) => {
            profile[f.key] = Number(card.querySelector(`input[data-field="${f.key}"]`).value);
        });
        return profile;
    });
}

$("#ab-profiles").addEventListener("click", (event) => {
    const btn = event.target.closest(".ab-remove");
    if (!btn) return;
    const idx = Number(btn.closest(".ab-profile").dataset.index);
    abProfiles = readProfileEditor();
    abProfiles.splice(idx, 1);
    renderProfileEditor();
});

$("#btn-ab-add").addEventListener("click", () => {
    abProfiles = readProfileEditor();
    if (abProfiles.length >= 4) return;
    abProfiles.push({
        name: `规则${"ABCD"[abProfiles.length]}`,
        ...defaultThresholds(),
    });
    renderProfileEditor();
});

$("#btn-ab-demo").addEventListener("click", () => {
    abProfiles = AB_DEMO.profiles.map((p) => ({ ...p }));
    $("#ab-queries").value = AB_DEMO.queries;
    renderProfileEditor();
});

function renderExperimentErrors(errors) {
    if (!errors || errors.length === 0) {
        $("#ab-errors").innerHTML = "";
        return;
    }
    const items = errors.map((e) => {
        const where = e.type === "profile"
            ? `第 ${e.index + 1} 档档案${e.field ? ` · ${fieldLabel(e.field)}` : ""}`
            : e.type === "query"
                ? `第 ${e.index + 1} 条查询`
                : (e.type === "profiles" ? "档案列表" : "查询列表");
        return `<li><b>${escapeHtml(where)}</b>：${escapeHtml(e.message)}</li>`;
    }).join("");
    $("#ab-errors").innerHTML =
        `<div class="ab-errors"><div class="ab-errors-head">⛔ 校验失败，实验未执行（共 ${errors.length} 项）：</div><ul>${items}</ul></div>`;
}

function fieldLabel(key) {
    const f = AB_FIELDS.find((x) => x.key === key);
    return f ? f.label : key;
}

// ------------------------------------------------------------ result render

function triggerBlock(th) {
    const rows = [
        ["auto_threshold", "自动命中阈值"],
        ["reject_threshold", "拒绝阈值"],
        ["margin_threshold", "前两名分差"],
    ].map(([key, label]) => {
        const block = th[key];
        const actual = block.actual === null || block.actual === undefined ? "—" : pct(block.actual);
        return `<div class="ab-thr${block.triggered ? " fired" : ""}">
            <span>${label}</span>
            <span class="muted">${pct(block.value)} / 实际 ${actual}</span>
            <span>${block.triggered ? "⚠ 触发" : "未触发"}</span>
        </div>`;
    }).join("");
    const decisive = th.decisive
        ? `<div class="ab-decisive">决定性阈值：<b>${escapeHtml(fieldLabel(th.decisive))}</b></div>`
        : `<div class="ab-decisive ok">得分达到自动命中阈值且领先明显，无阈值拦截</div>`;
    return `<div class="ab-thr-grid">${rows}</div>${decisive}
        <div class="muted">词元权重 ${pct(th.token_weight.value)} / 门牌号权重 ${pct(1 - th.token_weight.value)}</div>`;
}

function renderProfileColumn(pr) {
    const delta = pr.score_delta_vs_first;
    const deltaTxt = (delta !== null && pr.index > 0)
        ? ` <span class="${delta === 0 ? "muted" : delta > 0 ? "ab-delta-up" : "ab-delta-down"}">` +
          `(${delta > 0 ? "+" : ""}${pct(delta)})</span>`
        : "";
    const runner = pr.runner_up_id
        ? `<div class="muted">次高分 #${pr.runner_up_id}：${pct(pr.runner_up_score)}</div>` : "";
    return `
    <div class="ab-col">
        <div class="ab-col-head"><span class="ab-letter sm">${"ABCD"[pr.index]}</span> ${escapeHtml(pr.profile)}</div>
        <span class="status ${pr.status}">${STATUS_LABEL[pr.status] || pr.status}</span>
        <div class="ab-best">首选 #${pr.best_id ?? "—"} · <b>${pr.best_score === null ? "—" : pct(pr.best_score)}</b>${deltaTxt}</div>
        ${runner}
        <div class="muted">${escapeHtml(pr.reason)}</div>
        ${triggerBlock(pr.thresholds)}
    </div>`;
}

function renderDiffTable(qr) {
    const diff = qr.candidate_diff;
    if (!diff || diff.ranking.length === 0) return '<div class="muted">无候选</div>';
    const heads = qr.profiles.map((pr) =>
        `<th>${"ABCD"[pr.index]} ${escapeHtml(pr.profile)}</th>`).join("");
    const bestIds = new Set(qr.profiles.map((pr) => pr.best_id));
    const rows = diff.ranking.map((row) => {
        const cells = row.cells.map((cell) => {
            if (!cell.match) return '<td class="ab-cell absent">—</td>';
            const isBest = bestIds.has(row.address_id) && cell.match.rank === 1;
            return `<td class="ab-cell${isBest ? " best" : ""}">` +
                `#${cell.match.rank} · ${pct(cell.match.score)}</td>`;
        }).join("");
        return `<tr>
            <td class="ab-addr-cell">#${row.address_id} ${escapeHtml(row.full_text)}
                ${row.house ? `<span class="muted">（门牌 ${escapeHtml(row.house)}）</span>` : ""}</td>
            ${cells}
        </tr>`;
    }).join("");
    return `<div class="table-wrap ab-diff-wrap"><table class="ab-diff">
        <thead><tr><th>候选地址（各档排名 · 得分）</th>${heads}</tr></thead>
        <tbody>${rows}</tbody></table></div>`;
}

function renderExperimentResult(exp) {
    const s = exp.summary;
    const profHeads = s.profiles.map((p) => {
        return `<div class="ab-sum-cell">
            <b>${escapeHtml(p.name)}</b><br>
            <span class="status auto_matched">${p.auto_matched}</span>
            <span class="status needs_review">${p.needs_review}</span>
            <span class="status no_match">${p.no_match}</span>
        </div>`;
    }).join("");
    const snapshot = typeof exp.table_snapshot === "string"
        ? JSON.parse(exp.table_snapshot) : exp.table_snapshot;

    const queries = exp.results.map((qr) => {
        const da = qr.disagreement;
        const range = da.best_score_range.delta === null ? "" :
            ` · 首选分差区间 ${pct(da.best_score_range.min)}~${pct(da.best_score_range.max)}` +
            `（极差 ${pct(da.best_score_range.delta)}）`;
        const groups = Object.entries(da.status_groups).map(([st, names]) =>
            `<span class="status ${st}">${STATUS_LABEL[st] || st}：${escapeHtml(names.join("、"))}</span>`
        ).join(" ");
        return `
        <div class="ab-query${da.conclusion_differs ? " disagree" : ""}">
            <div class="query-line">
                ${da.conclusion_differs ? '<span class="ab-flag">⚠ 结论不一致</span>' : '<span class="ab-flag agree">✓ 各档一致</span>'}
                #${qr.index + 1} ${escapeHtml(qr.query)}
            </div>
            <div class="muted">归一化：${escapeHtml(qr.profiles[0].normalized.text || "（空）")}
                · 门牌号 ${escapeHtml(qr.profiles[0].normalized.house || "无")}${range}</div>
            <div class="ab-groups">${groups}</div>
            <div class="ab-cols">${qr.profiles.map(renderProfileColumn).join("")}</div>
            <details class="ab-details" ${da.conclusion_differs ? "open" : ""}>
                <summary>候选差异（${qr.candidate_diff.ranking.length} 个候选进入并集）</summary>
                ${renderDiffTable(qr)}
                <div class="ab-fulldiff">
                    ${qr.profiles.map((pr) => `
                    <details class="ab-cand-detail">
                        <summary>${"ABCD"[pr.index]} ${escapeHtml(pr.profile)} 的完整候选（${pr.candidates.length}）</summary>
                        ${pr.candidates.map((c, i) => renderCandidate(c, i, {})).join("")}
                    </details>`).join("")}
                </div>
            </details>
        </div>`;
    }).join("");

    $("#ab-result").innerHTML = `
    <div class="card ab-result-card">
        <div class="ab-result-head">
            <h2>实验结果 <span class="ab-eid">${escapeHtml(exp.id)}</span></h2>
            <span class="muted">${escapeHtml(exp.created_at)} · 地址表快照 ${snapshot.count} 条
                · digest ${escapeHtml(String(snapshot.digest).slice(0, 12))}…</span>
        </div>
        <div class="ab-summary">
            <div class="ab-sum-cell"><b>查询</b><br><span class="muted">共 ${s.query_count} 条</span><br>
                <span class="${s.disagreement_count ? "ab-flag" : "ab-flag agree"}">不一致 ${s.disagreement_count}</span></div>
            ${profHeads}
        </div>
        <p class="hint">状态计数依次为：自动命中 / 待确认 / 无匹配。</p>
        ${queries}
    </div>`;
}

async function submitExperiment() {
    const profiles = readProfileEditor();
    const queries = $("#ab-queries").value.split("\n").map((q) => q.trim()).filter(Boolean);
    $("#ab-errors").innerHTML = "";
    $("#ab-result").innerHTML = "";
    const btn = $("#btn-ab-run");
    btn.disabled = true;
    $("#ab-run-summary").innerHTML = '<span class="muted">实验运行中…</span>';
    try {
        const res = await fetch("/api/experiments", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ profiles, queries }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            renderExperimentErrors(data.errors || [{ type: "request", index: null, field: null, message: data.detail || "请求失败" }]);
            $("#ab-run-summary").innerHTML = "";
            return;
        }
        $("#ab-run-summary").innerHTML =
            `<span class="ok-msg">实验 ${escapeHtml(data.id)} 完成：${data.summary.query_count} 条查询，` +
            `结论不一致 ${data.summary.disagreement_count} 条，已持久化</span>`;
        renderExperimentResult(data);
        loadExperimentHistory();
    } catch (err) {
        renderExperimentErrors([{ type: "request", index: null, field: null, message: err.message }]);
        $("#ab-run-summary").innerHTML = "";
    } finally {
        btn.disabled = false;
    }
}

$("#btn-ab-run").addEventListener("click", submitExperiment);

// -------------------------------------------------------------- history

async function loadExperimentHistory() {
    const box = $("#ab-history");
    if (!box) return;
    try {
        const data = await api("/api/experiments?limit=50");
        if (data.items.length === 0) {
            box.innerHTML = '<p class="muted">暂无实验记录。</p>';
            return;
        }
        box.innerHTML = `<div class="table-wrap"><table>
            <thead><tr><th>实验编号</th><th>提交时间</th><th>档案</th><th>查询</th><th>不一致</th></tr></thead>
            <tbody>${data.items.map((it) => `<tr>
                <td><a href="#" class="ab-open" data-id="${escapeHtml(it.id)}">${escapeHtml(it.id)}</a></td>
                <td>${escapeHtml(it.created_at)}</td>
                <td>${it.profiles.map((p) => escapeHtml(p.name)).join(" / ")}</td>
                <td>${it.query_count}</td>
                <td>${it.summary.disagreement_count ?
                    `<span class="ab-flag">${it.summary.disagreement_count}</span>` :
                    '<span class="ab-flag agree">0</span>'}</td>
            </tr>`).join("")}</tbody></table></div>
            ${data.total > 50 ? `<p class="muted">共 ${data.total} 个实验，仅显示最近 50 个</p>` : ""}`;
    } catch (err) {
        box.innerHTML = `<p class="err-msg">${escapeHtml(err.message)}</p>`;
    }
}

$("#btn-ab-refresh-history").addEventListener("click", loadExperimentHistory);

$("#ab-history").addEventListener("click", async (event) => {
    const link = event.target.closest(".ab-open");
    if (!link) return;
    event.preventDefault();
    try {
        const data = await api(`/api/experiments/${encodeURIComponent(link.dataset.id)}`);
        renderExperimentResult(data);
        window.scrollTo({ top: $("#ab-result").offsetTop - 60, behavior: "smooth" });
    } catch (err) {
        alert(err.message);
    }
});
