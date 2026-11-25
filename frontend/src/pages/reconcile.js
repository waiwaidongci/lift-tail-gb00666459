import {api, download} from '../api/client.js';
import {empty, errorSlot} from '../components/common/layout.js';
import {table} from '../components/common/table.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {esc, time} from '../utils/html.js';

const sideLabel = side => side === 'lift' ? '货梯启停记录' : '搬运签收单';
const statusLabel = {
  confirmed: '已确认配对',
  conflict: '冲突：可进入最优配法但未确认',
  unmatched: '无匹配',
};

function versionOptions(rows, selected, emptyLabel) {
  if (!rows.length) return `<option value="">${esc(emptyLabel)}</option>`;
  return rows.map(row => `<option value="version:${row.id}" ${"version:" + row.id === selected ? 'selected' : ''}>#${row.id} · ${esc(row.name)} · ${esc(row.checksum.slice(0, 8))}</option>`).join('');
}

function sourceOptions(rows, datasets, selected, emptyLabel, includeDatasets = true) {
  const versionOptionsHtml = versionOptions(rows, selected, emptyLabel);
  const datasetOptions = includeDatasets && datasets.length
    ? `<optgroup label="既有台账数据版本">${datasets.map(row => `<option value="dataset:${row.id}" ${"dataset:" + row.id === selected ? 'selected' : ''}>数据 #${row.id} · ${esc(row.name)} · ${esc(row.checksum.slice(0, 8))}</option>`).join('')}</optgroup>`
    : '';
  return `<optgroup label="对账输入版本">${versionOptionsHtml}</optgroup>${datasetOptions}`;
}

function importForm(kind, title, hint) {
  if (state.account.role !== 'analyst') return `<p class="hint">分析员可导入${title}版本。</p>`;
  const submitLabel = kind === 'lift' ? '保存并锁定货梯输入版本' : '保存并锁定签收输入版本';
  const templateLabel = kind === 'lift' ? '下载货梯表头样例' : '下载签收表头样例';
  return `<form id="import-${kind}" data-import-version="${kind}">
    <h3>${esc(title)}</h3>
    <p class="hint">${esc(hint)}</p>
    <label>${kind === 'lift' ? '货梯版本名称' : '签收版本名称'}<input name="name" maxlength="180" placeholder="${esc(title)}"></label>
    <label>${kind === 'lift' ? '选择货梯 CSV 文件' : '选择签收 CSV 文件'}<input name="csv_file" type="file" accept=".csv,text/csv" data-testid="file-${kind}"></label>
    <label>${kind === 'lift' ? '或粘贴货梯 CSV' : '或粘贴签收 CSV'}<textarea name="csv" rows="5" placeholder="record_id,elevator_id,started_at,stopped_at"></textarea></label>
    <button type="submit" data-testid="save-${kind}-version">${submitLabel}</button>
    <button type="button" class="secondary" data-template="${kind}">${templateLabel}</button>
    ${errorSlot}
  </form>`;
}

function attributionPanel(data) {
  if (!data) return '';
  const record = data.record;
  const counterpart = data.counterpart;
  const candidates = data.candidates || [];
  const candidateRows = candidates.map(pair => ({
    货梯启停记录: pair.lift_record_id,
    搬运签收单: pair.receipt_record_id,
    货梯编号: pair.elevator_id,
    重叠分钟: pair.overlap_minutes,
  }));
  return `<section data-testid="attribution-result" class="result-card">
    <h3>记录号 ${esc(data.record_no)} 的归属</h3>
    <p><strong>${esc(sideLabel(data.side))}</strong> · 货梯 ${esc(record.elevator_id)} ·
      <span data-status="${esc(record.status)}">${esc(statusLabel[record.status] || record.status)}</span></p>
    <p class="hint">本侧搬运/启停区间：${esc(record.interval_start)} 至 ${esc(record.interval_end)}</p>
    ${data.status === 'confirmed' && counterpart ? `<p class="notice" data-testid="attributed-counterpart">双方归属已确认：${esc(sideLabel(counterpart.side))}记录 <strong>${esc(counterpart.record_id)}</strong>，重叠 ${esc(record.overlap_minutes)} 分钟。</p>` : ''}
    ${data.status === 'conflict' ? `<p class="notice">该记录能进入至少一种最优配法，但配对不是所有最优配法共有，因此标记为冲突，不做唯一归属。</p>${table(candidateRows)}` : ''}
    ${data.status === 'unmatched' ? '<p>该记录无法进入任何最优配法，标记为无匹配。</p>' : ''}
  </section>`;
}

export async function reconcile() {
  const [versions, jobs, datasets] = await Promise.all([
    api('/reconcile/versions'),
    api('/reconciliations'),
    api('/datasets'),
  ]);
  const lifts = versions.filter(row => row.kind === 'lift');
  const receipts = versions.filter(row => row.kind === 'receipt');
  const selectedId = state.reconcileId || state.runId || jobs[0]?.id || null;
  const job = selectedId ? await api(`/reconciliations/${selectedId}`) : null;
  if (job) state.reconcileId = job.id;

  const lockSource = (side, fallback) => {
    const item = job?.input_lock?.[side];
    if (!item) return fallback;
    return `${item.source === 'analysis_dataset' ? 'dataset' : 'version'}:${item.id}`;
  };
  const liftFallback = lifts[0] ? `version:${lifts[0].id}` : '';
  const receiptFallback = receipts[0] ? `version:${receipts[0].id}` : (datasets[0] ? `dataset:${datasets[0].id}` : '');
  const liftSelected = state.liftSource || lockSource('lift', liftFallback);
  const receiptSelected = state.receiptSource || lockSource('receipt', receiptFallback);
  const locked = job?.input_lock;
  const attribution = state.reconcileAttribution?.reconciliation_id === job?.id ? state.reconcileAttribution : null;
  const result = job?.result;
  const summary = result?.summary;
  const running = ['queued', 'running'].includes(job?.state);

  const hasLiftSource = lifts.length;
  const hasReceiptSource = receipts.length || datasets.length;
  const createForm = state.account.role === 'analyst' ? `<form id="create-reconciliation" class="inline-form" data-testid="reconciliation-create">
    <label>货梯启停版本<select name="lift_source" required ${hasLiftSource ? '' : 'disabled'}>${sourceOptions(lifts, [], liftSelected, '请先导入货梯启停 CSV', false)}</select></label>
    <label>搬运签收版本<select name="receipt_source" required ${hasReceiptSource ? '' : 'disabled'}>${sourceOptions(receipts, datasets, receiptSelected, '请先导入签收 CSV')}</select></label>
    <button type="submit" ${hasLiftSource && hasReceiptSource ? '' : 'disabled'}>锁定两份版本并对账</button>
    ${errorSlot}
  </form>` : '<p class="hint">分析员可锁定版本并发起对账。</p>';

  const jobPicker = jobs.length ? `<label>对账任务<select id="pick-reconciliation">${jobs.map(row => `<option value="${row.id}" ${row.id === job?.id ? 'selected' : ''}>#${row.id} · ${({queued:'排队',running:'计算中',completed:'已完成',failed:'需重试'})[row.state] || row.state} · ${time(row.created)}</option>`).join('')}</select></label>` : empty('先导入两份输入版本并发起对账。');

  const retry = job && job.state !== 'completed' && state.account.role === 'analyst'
    ? `<button type="button" id="retry-reconciliation" class="secondary" data-testid="retry-reconciliation">沿用原版本重试本任务 #${job.id}</button>` : '';
  const statusBlock = job ? `<p data-testid="reconciliation-status" class="${job.state === 'failed' ? 'error' : running ? '' : 'notice'}" ${running ? 'data-poll' : ''}>
      任务 #${job.id}：${({queued:'排队中',running:'对账计算中',completed:'已完成',failed:'中断或需修正'})[job.state] || job.state}
      ${job.error ? ` · ${esc(job.error)}` : ''}
    </p>${retry}<p class="hint">创建于 ${time(job.created)} · 开始 ${time(job.started)} · 完成 ${time(job.finished)} · 尝试 ${job.attempts || 0} 次。重试只更新本任务，不新增对账结果。</p>` : '';

  const metrics = summary ? table([
    {指标:'货梯启停记录', 数值:summary.lift_records},
    {指标:'搬运签收记录', 数值:summary.receipt_records},
    {指标:'最优配对趟数', 数值:summary.optimal_pair_count},
    {指标:'最长总重叠（分钟）', 数值:summary.optimal_overlap_minutes},
    {指标:'所有最优配法共有配对', 数值:summary.confirmed_pair_count},
    {指标:'冲突记录数', 数值:summary.conflict_record_count},
    {指标:'无匹配记录数', 数值:summary.unmatched_record_count},
  ]) : '';

  const confirmedTable = result ? table(result.confirmed_pairs.map(pair => ({
    货梯启停记录: pair.lift_record_id,
    搬运签收单: pair.receipt_record_id,
    货梯编号: pair.elevator_id,
    重叠分钟: pair.overlap_minutes,
  }))) : '';
  const conflictTable = result ? table(result.conflict_records.map(record => ({
    侧别: sideLabel(record.side),
    记录号: record.record_id,
    货梯编号: record.elevator_id,
    状态: statusLabel.conflict,
    可进入的最优配对: record.candidates.map(pair => `${pair.lift_record_id} ↔ ${pair.receipt_record_id}（${pair.overlap_minutes} 分钟）`).join('；'),
  }))) : '';
  const unmatchedTable = result ? table(result.unmatched_records.map(record => ({
    侧别: sideLabel(record.side),
    记录号: record.record_id,
    货梯编号: record.elevator_id,
    状态: statusLabel.unmatched,
  }))) : '';

  const lookupForm = job?.result ? `<form id="record-lookup" class="inline-form" data-testid="record-lookup">
    <label>在任务 #${job.id} 内查询<input name="record_no" required list="reconciliation-record-numbers" placeholder="例如 LQ-2401 或 QS-2401" data-testid="record-search"></label>
    <datalist id="reconciliation-record-numbers">${result.records.map(record => `<option value="${esc(record.record_id)}">${esc(sideLabel(record.side))}</option>`).join('')}</datalist>
    <button type="submit">查询归属</button>
    ${errorSlot}
  </form>${attributionPanel(attribution)}` : '';

  const globalResult = state.globalAttribution ? attributionPanel(state.globalAttribution) +
    `<p class="hint">该归属来自对账任务 <a href="#6/${state.globalAttribution.reconciliation_id}">#${state.globalAttribution.reconciliation_id}</a>。</p>` : '';
  const globalSearch = `<section data-testid="global-record-query"><h2>按记录号查双方归属</h2>
    <form id="global-record-lookup" class="inline-form" data-testid="global-record-lookup">
      <label>记录号<input name="record_no" required placeholder="输入任一侧记录号" data-testid="global-record-search"></label>
      <button type="submit">查询双方归属</button>
      ${errorSlot}
    </form>${globalResult}
    <p class="hint">不指定任务时查询最近 100 个已完成对账；也可在下方选择具体对账任务后在任务范围内查询。</p>
  </section>`;

  const quickForm = state.account.role === 'analyst' ? `<section><h2>1. 提供两份输入并对账</h2>
    <form id="quick-reconcile" data-testid="quick-reconcile">
      <div class="grid two">
        <label>货梯启停记录 CSV<textarea name="lift_csv" rows="7" data-testid="quick-lift-csv" placeholder="record_id,elevator_id,started_at,stopped_at"></textarea><input name="lift_file" type="file" accept=".csv,text/csv" data-testid="quick-lift-file"></label>
        <label>搬运签收单 CSV<textarea name="receipt_csv" rows="7" data-testid="quick-receipt-csv" placeholder="task_id,elevator_id,started_at,duration_minutes,wait_minutes"></textarea><input name="receipt_file" type="file" accept=".csv,text/csv" data-testid="quick-receipt-file"></label>
      </div>
      <button type="submit" data-testid="quick-reconcile-submit">锁定两份版本并开始对账</button>
      ${errorSlot}
    </form>
    <p class="hint">提交时会分别保存不可变版本和 SHA-256，并把两份版本号、校验值及记录快照锁定到同一对账任务。</p>
  </section>` : '';

  return `<section data-testid="reconciliation-page">
    <h2>结算前货梯启停 × 搬运签收对账</h2>
    <p>每次锁定两份输入版本：一份<strong>货梯启停记录</strong>版本和一份<strong>搬运签收单</strong>版本。签收的搬运区间从排队结束算到交付，与同梯启停区间有重叠才可配对，两边记录各用一次。</p>
    <p class="hint">先让配对趟数最多，再让总重叠时长最长。只确认所有最优配法共有的配对；能进最优配法却未确认的记录标冲突，其余标无匹配。中断后续跑沿用原版本，重试不新增对账结果。</p>
  </section>
  ${globalSearch}
  ${quickForm}
  <section><h2>2. 分别导入输入版本</h2><div class="grid two">${importForm('lift', '导入货梯启停记录', '字段：record_id、elevator_id、started_at、stopped_at；也兼容中文表头。')}${importForm('receipt', '导入搬运签收单', '可用 receipt_id、elevator_id、queue_ended_at、delivered_at，或沿用 started_at、wait_minutes、duration_minutes。')}</div></section>
  <section><h2>3. 从版本库选择并对账</h2>${createForm}${locked ? `<p class="hint" data-testid="locked-versions">已锁定：货梯版本 #${locked.lift.id} <code>${esc(locked.lift.checksum.slice(0, 12))}</code>；签收版本 #${locked.receipt.id} <code>${esc(locked.receipt.checksum.slice(0, 12))}</code>。中断续跑和重试均沿用此锁定。</p>` : ''}</section>
  <section><h2>4. 对账任务</h2>${jobPicker}${statusBlock}${job?.result ? `<p class="notice">${esc(job.result.headline)}</p>${metrics}` : empty(running ? '任务正在执行，页面会自动刷新。' : '发起对账后在此查看最优配对和归属。')}</section>
  ${job?.result ? `<section><h2>5. 任务内按记录号查双方归属</h2>${lookupForm}</section>
  <section data-testid="confirmed-section"><h2>所有最优配法共有的确认配对</h2>${confirmedTable}</section>
  <section data-testid="conflict-section"><h2>冲突记录（可进入最优配法但未确认）</h2>${conflictTable}</section>
  <section data-testid="unmatched-section"><h2>无匹配记录</h2>${unmatchedTable}</section>
  <section><h2>对账口径</h2><ul>${result.notes.map(note => `<li>${esc(note)}</li>`).join('')}</ul></section>` : ''}`;
}

export function bindReconcile(root, refresh) {
  bindForm(root, '#quick-reconcile', async form => {
    let liftCsv = form.elements.lift_csv.value;
    let receiptCsv = form.elements.receipt_csv.value;
    const liftFile = form.elements.lift_file.files?.[0];
    const receiptFile = form.elements.receipt_file.files?.[0];
    if (liftFile) liftCsv = await liftFile.text();
    if (receiptFile) receiptCsv = await receiptFile.text();
    if (!liftCsv.trim() || !receiptCsv.trim()) {
      throw new Error('请分别提供货梯启停记录和搬运签收单的 CSV 内容或文件。');
    }
    const job = await api('/reconciliations', {
      lift_csv: liftCsv,
      receipt_csv: receiptCsv,
      lift_name: liftFile?.name || '页面快速货梯启停记录',
      receipt_name: receiptFile?.name || '页面快速搬运签收单',
    });
    state.reconcileId = job.id;
    state.runId = job.id;
    state.reconcileAttribution = null;
    state.status = job.created ? `已锁定两份版本并生成对账任务 #${job.id}。` : `相同两份版本已有对账结果 #${job.id}，重试未新增结果。`;
    location.hash = `6/${job.id}`;
  }, refresh);

  root.querySelectorAll('[data-import-version]').forEach(form => {
    bindForm(root, `#${form.id}`, async current => {
      const kind = current.dataset.importVersion;
      const file = current.elements.csv_file?.files?.[0];
      let csv = current.elements.csv.value;
      let name = current.elements.name.value;
      if (file) {
        csv = await file.text();
        name = name || file.name;
      }
      if (!csv.trim()) throw new Error('请选择 CSV 文件或粘贴 CSV 内容。');
      const saved = await api('/reconcile/versions', {kind, name, csv});
      if (kind === 'lift') state.liftSource = `version:${saved.id}`;
      if (kind === 'receipt') state.receiptSource = `version:${saved.id}`;
      state.status = `已保存${kind === 'lift' ? '货梯启停' : '搬运签收'}版本 #${saved.id}。`;
    }, refresh);
  });

  root.querySelector('[data-template="lift"]')?.addEventListener('click', async event => {
    try {await download('/reconcile/example/lift.csv', 'elevator-start-stop-example.csv');}
    catch (error) {state.status = error.message; refresh();}
  });
  root.querySelector('[data-template="receipt"]')?.addEventListener('click', async event => {
    try {await download('/reconcile/example/receipt.csv', 'handling-signoff-example.csv');}
    catch (error) {state.status = error.message; refresh();}
  });

  bindForm(root, '#create-reconciliation', async form => {
    const payload = {};
    for (const [side, field] of [['lift', 'lift_source'], ['receipt', 'receipt_source']]) {
      const value = form.elements[field].value;
      const [source, id] = value.split(':');
      const idField = side === 'lift'
        ? (source === 'dataset' ? 'lift_dataset_id' : 'lift_version_id')
        : (source === 'dataset' ? 'receipt_dataset_id' : 'receipt_version_id');
      payload[idField] = Number(id);
      if (side === 'lift') state.liftSource = value;
      if (side === 'receipt') state.receiptSource = value;
    }
    const job = await api('/reconciliations', payload);
    state.reconcileId = job.id;
    state.runId = job.id;
    state.reconcileAttribution = null;
    state.status = job.created ? `已锁定版本并生成对账任务 #${job.id}。` : `相同两份版本已有对账结果 #${job.id}，重试未新增结果。`;
    if (`#6/${job.id}` !== location.hash) location.hash = `#6/${job.id}`;
  }, refresh);

  root.querySelector('#pick-reconciliation')?.addEventListener('change', event => {
    state.reconcileId = Number(event.target.value);
    state.reconcileAttribution = null;
    location.hash = `6/${event.target.value}`;
  });

  root.querySelector('#retry-reconciliation')?.addEventListener('click', async event => {
    const id = event.currentTarget.dataset.id || state.reconcileId;
    event.currentTarget.disabled = true;
    try {
      const job = await api(`/reconciliations/${id}/retry`, {}, 'POST');
      state.reconcileId = job.id;
      state.status = '已沿用原来锁定的两份版本续跑；没有新增对账结果。';
      await refresh();
    } catch (error) {
      state.status = error.message;
      await refresh();
    }
  });

  bindForm(root, '#global-record-lookup', async form => {
    const recordNo = form.elements.record_no.value.trim();
    const data = await api(`/reconcile/lookup?record_no=${encodeURIComponent(recordNo)}`);
    state.globalAttribution = data;
    state.reconcileAttribution = data;
    state.reconcileId = data.reconciliation_id;
  }, refresh);

  bindForm(root, '#record-lookup', async form => {
    const recordNo = form.elements.record_no.value.trim();
    const id = state.reconcileId || state.runId;
    const data = await api(`/reconciliations/${id}/lookup?record_no=${encodeURIComponent(recordNo)}`);
    state.reconcileAttribution = data;
  }, refresh);
}
