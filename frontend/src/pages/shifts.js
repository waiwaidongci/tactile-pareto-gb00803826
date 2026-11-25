import {api} from '../api/client.js';
import {errorSlot} from '../components/common/layout.js';
import {table} from '../components/common/table.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {esc, time} from '../utils/html.js';

const canEdit = () => state.account && (state.account.role === 'analyst' || state.account.role === 'reviewer');
const enc = value => encodeURIComponent(String(value));

export const shiftStatusLabels = {
  collecting: '收单中 · 待收齐',
  mismatch: '核对不符 · 待补交',
  ready: '核对通过 · 可分析',
  closed: '已封班 · 版本固定',
};

function statusBadge(shift) {
  const label = shift.status_label || shiftStatusLabels[shift.status] || shift.status;
  return `<span class="badge badge--${esc(shift.status)}" data-shift-status="${esc(shift.status)}">${esc(label)}</span>`;
}

function formMeta(form) {
  if (!form) return '<p class="hint">尚未提交（先到的一份会照收）。</p>';
  return `<p class="form-meta">${esc(form.filename || '原表')} · 第 ${esc(form.revision || 1)} 交 · ${esc(form.row_count)} 行<br><span class="mono">SHA-256 ${esc(String(form.checksum || '').slice(0, 12))}</span> · ${time(form.created)}</p>`;
}

export function shiftTable(shifts) {
  if (!shifts || !shifts.length) {
    return '<p class="empty">还没有班次记录。在上方填写班次编号并交第一份原表，或载入演示班次。</p>';
  }
  const rows = shifts.map(s => ({
    code: {link: `#1/shift/${s.id}`, text: s.code},
    status: {badge: s.status_label || shiftStatusLabels[s.status] || s.status, tone: s.status},
    reject: s.forms?.reject ? `已交 · ${s.forms.reject.row_count} 行` : '待交',
    recheck: s.forms?.recheck ? `已交 · ${s.forms.recheck.row_count} 行` : '待交',
    mismatch_count: s.mismatch_count ? `${s.mismatch_count} 项` : '0',
    dataset: s.dataset_id ? {link: '#2', text: `版本 #${s.dataset_id}`} : '—',
    updated: time(s.updated),
    open: {link: `#1/shift/${s.id}`, text: '查进度 / 办理'},
  }));
  return table(rows, [
    {key: 'code', label: '班次'},
    {key: 'status', label: '班次进度'},
    {key: 'reject', label: '拒收单'},
    {key: 'recheck', label: '复检单'},
    {key: 'mismatch_count', label: '对不上'},
    {key: 'dataset', label: '整班数据版本'},
    {key: 'updated', label: '更新时间'},
    {key: 'open', label: '办理'},
  ]);
}

export async function shiftOverview() {
  let shifts = [];
  let loadError = '';
  try {
    shifts = await api('/shifts');
  } catch (error) {
    loadError = error.message;
  }
  return `<section id="shift-overview"><h2>班次进度</h2>
    <p class="hint">按班次收齐拒收单与复检单两份原表；复检单常先到，先到照收，另一份到齐后逐件核对。这里可查每个班次的收单、核对、不符与封班进度。</p>
    ${loadError ? `<p class="error" role="alert">${esc(loadError)}</p>` : shiftTable(shifts)}
    <p><a class="button secondary" href="#1">到「拒收与复检台账」交单 / 核对 / 封班</a></p>
  </section>`;
}

function steps(detail) {
  const reject = !!detail?.forms?.reject;
  const recheck = !!detail?.forms?.recheck;
  const done = detail?.status === 'ready' || detail?.status === 'closed';
  const mismatch = detail?.status === 'mismatch';
  const closed = detail?.status === 'closed';
  const items = [
    {label: '收到拒收单', on: reject},
    {label: '收到复检单（常先到）', on: recheck},
    {label: '两份到齐 · 逐件核对通过', on: done, bad: mismatch},
    {label: '封班 · 固定同一版本', on: closed},
  ];
  return `<ol class="steps" aria-label="班次办理步骤">${items.map((item, i) =>
    `<li class="${item.on ? 'is-done' : ''} ${item.bad ? 'is-bad' : ''}"><span class="step-dot">${item.bad ? '!' : item.on ? '✓' : i + 1}</span>${esc(item.label)}</li>`).join('')}</ol>`;
}

function mismatchTable(detail) {
  const items = detail?.mismatches || [];
  if (!items.length) return '';
  const rows = items.map(m => ({
    lot_id: m.lot_id,
    unit_id: m.unit_id,
    side: m.side === 'recheck' ? '复检单' : '拒收单',
    type: m.label || m.code,
    defect_code: m.defect_code || '—',
  }));
  return `<div class="mismatch"><h3>对不上的批次 / 单件（${items.length}）</h3>
    <p class="hint">请注明下列批次和单件；只需补交有误的那份原表，无误的一份继续沿用。</p>
    ${table(rows, [
      {key: 'lot_id', label: '批次'},
      {key: 'unit_id', label: '单件'},
      {key: 'side', label: '出问题的原表'},
      {key: 'type', label: '核对结果'},
      {key: 'defect_code', label: '相关缺陷代码'},
    ])}</div>`;
}

function shiftForm(detail, codes) {
  if (!canEdit()) {
    return `<p class="notice">当前为只读账号，可查询班次进度；交单、核对与封班请由分析员或复核员操作。</p>`;
  }
  const code = detail?.code || (state.shiftKey && /\D/.test(String(state.shiftKey)) ? state.shiftKey : '') || '';
  const reject = detail?.forms?.reject;
  const recheck = detail?.forms?.recheck;
  return `<form id="shift-form" class="shift-form">
    <div class="shift-bar">
      <label for="shift-code">班次</label>
      <input id="shift-code" name="shift" required maxlength="80" list="shift-codes" value="${esc(code)}" placeholder="如 20261125-早班 或 TG-1124" autocomplete="off">
      <datalist id="shift-codes">${codes.map(c => `<option value="${esc(c.code)}">${esc(c.status_label || '')}</option>`).join('')}</datalist>
      <button type="submit" name="action" value="open" class="secondary">查询 / 建立班次</button>
      <button type="button" id="shift-demo" class="secondary">载入演示班次</button>
    </div>
    <div class="slots">
      <fieldset class="slot" data-kind="reject">
        <p class="slot-title">拒收单 ${reject ? statusBadge({status: 'ready', status_label: '已交'}) : '<span class="badge">待交</span>'}</p>
        ${formMeta(reject)}
        <label class="file-label" for="file-reject">拒收单原表（CSV，含拒收登记行）
          <input id="file-reject" name="reject_file" type="file" accept=".csv,text/csv">
        </label>
        <button type="submit" name="action" value="upload-reject">${reject ? '补交拒收单' : '提交拒收单'}</button>
      </fieldset>
      <fieldset class="slot" data-kind="recheck">
        <p class="slot-title">复检单 ${recheck ? statusBadge({status: 'ready', status_label: '已交'}) : '<span class="badge">待交（常先到）</span>'}</p>
        ${formMeta(recheck)}
        <label class="file-label" for="file-recheck">复检单原表（CSV，含缺陷复检行）
          <input id="file-recheck" name="recheck_file" type="file" accept=".csv,text/csv">
        </label>
        <button type="submit" name="action" value="upload-recheck">${recheck ? '补交复检单' : '提交复检单'}</button>
      </fieldset>
    </div>
    ${errorSlot}
  </form>`;
}

function shiftDetailPanel(detail) {
  if (!detail) return '';
  const ds = detail.dataset_id;
  return `<div class="shift-detail" data-shift-id="${detail.id}" data-shift-status="${esc(detail.status)}">
    ${steps(detail)}
    <p class="notice" role="status">${esc(detail.progress_text || detail.status_label)}</p>
    ${mismatchTable(detail)}
    <div class="shift-actions">
      ${canEdit() ? `<form id="reconcile-form" class="inline-form">
        <button type="submit" class="secondary">逐件核对</button>${errorSlot}
      </form>
      <form id="close-form" class="inline-form">
        <button type="submit" id="close-shift">${detail.closed ? '重新封班（同一版本）' : '封班'}</button>${errorSlot}
      </form>` : '<p class="hint">只读账号可查询进度；逐件核对与封班由分析员 / 复核员操作。</p>'}
      ${ds ? `<button type="button" id="goto-analysis" class="secondary" data-dataset="${ds}">用整班版本 #${ds} 运行分析 →</button>` : ''}
    </div>
    <p class="hint">班次 #${detail.id} · 当前整班数据版本：${ds ? `#${ds}（SHA-256 ${esc(String(detail.pair_checksum || '').slice(0, 16))}…）` : '核对通过后生成'} · 版本号 v${esc(detail.version)}</p>
  </div>`;
}

export async function shiftWorkspace() {
  const shifts = await api('/shifts');
  let detail = null;
  let loadError = '';
  if (state.shiftKey) {
    try {
      detail = await api(`/shifts/${enc(state.shiftKey)}`);
    } catch (error) {
      loadError = error.message;
    }
  }
  return `<section id="shift-workspace">
    <h2>按班次交两份原表 · 查班次进度</h2>
    <p class="hint">拒收单和复检单分开交，复检单常先到。先到的一份照收；另一份到齐后按「批次 + 单件」逐件核对。对不上会注明批次和单件，补交时只换有误的那份、无误那份沿用。核对通过的整班数据才能分析；封班后相同原单重传仍回到同一版本，换内容会被拒收。</p>
    ${shiftForm(detail, shifts)}
    ${loadError ? `<p class="error" role="alert">${esc(loadError)}</p>` : ''}
    ${detail ? shiftDetailPanel(detail) : '<p class="hint">填写班次并交第一份原表后，这里显示逐件核对结果与封班操作；也可在下方列表打开已有班次。</p>'}
    <h3>班次进度总表</h3>
    ${shiftTable(shifts)}
  </section>`;
}

export function bindShifts(root, refresh) {
  bindForm(root, '#shift-form', async (form, submitter) => {
    const action = submitter?.value || 'open';
    const code = form.elements.shift.value.trim();
    if (!code) throw new Error('请先填写班次编号。');
    const codePath = enc(code);

    if (action === 'open') {
      const view = await api('/shifts', {code});
      state.shiftKey = view.id;
      location.hash = `#1/shift/${view.id}`;
      state.status = '班次已打开，可分别提交两份原表。';
      return;
    }
    if (action === 'upload-reject' || action === 'upload-recheck') {
      const kind = action === 'upload-reject' ? 'reject' : 'recheck';
      const input = form.querySelector(kind === 'reject' ? '#file-reject' : '#file-recheck');
      const file = input?.files?.[0];
      if (!file) throw new Error(kind === 'reject' ? '请先选择拒收单 CSV 文件。' : '请先选择复检单 CSV 文件。');
      const view = await api(`/shifts/${codePath}/forms`, {kind, name: file.name, csv: await file.text()});
      state.shiftKey = view.id;
      if (view.dataset_id) state.datasetId = view.dataset_id;
      location.hash = `#1/shift/${view.id}`;
      state.status = view.notice
        || (view.status === 'ready' ? '两份原表已逐件核对通过，整班数据可用于分析。'
          : view.status === 'mismatch' ? '已收单，但逐件核对有对不上的批次/单件，请按明细补交。'
            : '先到的原表已照收，另一份到齐后自动逐件核对。');
      return;
    }
    if (action === 'reconcile') {
      const view = await api(`/shifts/${codePath}/reconcile`, {});
      state.status = view.passed ? '逐件核对通过，整班数据可用于分析。' : `逐件核对发现 ${view.mismatch_count} 项对不上，请补交。`;
      await refresh();
      return;
    }
    if (action === 'close') {
      const view = await api(`/shifts/${codePath}/close`, {});
      state.status = view.notice || '封班完成，整班数据版本固定。';
      await refresh();
    }
  }, refresh);

  bindForm(root, '#reconcile-form', async () => {
    const id = state.shiftKey;
    if (!id) throw new Error('请先打开班次。');
    const view = await api(`/shifts/${enc(id)}/reconcile`, {});
    state.status = view.passed ? '逐件核对通过。' : `仍有 ${view.mismatch_count} 项对不上。`;
  }, refresh);

  bindForm(root, '#close-form', async () => {
    const id = state.shiftKey;
    if (!id) throw new Error('请先打开班次。');
    const view = await api(`/shifts/${enc(id)}/close`, {});
    state.status = view.notice || '封班完成，版本固定。';
  }, refresh);

  root.querySelector('#shift-demo')?.addEventListener('click', async () => {
    const slot = root.querySelector('#shift-form [data-error]') || root.querySelector('#shift-action-error');
    try {
      const view = await api('/shifts/demo', {});
      state.shiftKey = view.id;
      if (view.dataset_id) state.datasetId = view.dataset_id;
      location.hash = `#1/shift/${view.id}`;
      state.status = '已载入演示班次：复检单先到、拒收单后到，已逐件核对通过。';
      await refresh();
    } catch (error) {
      if (slot) slot.textContent = error.message;
    }
  });

  root.querySelector('#goto-analysis')?.addEventListener('click', event => {
    state.datasetId = Number(event.currentTarget.dataset.dataset) || state.datasetId;
    location.hash = '#2';
  });
}
