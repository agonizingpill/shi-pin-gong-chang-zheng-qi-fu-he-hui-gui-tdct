import {BASELINE, LINE, PLAN, SHIFT} from '../constants.js';
import {busyPlan, esc, clock, number} from '../utils.js';
import {feedback, heading, notice, report, shiftStamp, status} from '../components/ui.js';

function requestForm(shifts, draft) {
  return `<details class="accordion ${draft ? 'request-open' : ''}" ${draft ? 'open' : ''}><summary>建立负荷方案</summary>
    <form class="fields" data-path="/plans" data-route="plans"><label>目标班次<select name="shift_id" required>
      ${shifts.map(shift => `<option value="${shift.id}" ${draft === shift.id ? 'selected' : ''}>${esc(shift.code)} · ${esc(shift.product)} · ${SHIFT[shift.state]}</option>`).join('')}</select></label>
    <label>方案名称<input name="title" minlength="4" maxlength="80" placeholder="晚班供汽基线" required></label>
    <button>提交稳健估计</button>${feedback}</form></details>`;
}

function sourceTag(row) {
  const source = row.source || 'fitted';
  return `<span class="baseline-tag ${source}">${BASELINE[source]}</span>`;
}

function hourlyRow(row) {
  const note = row.source === 'manual' ? `<small class="baseline-note">${esc(row.note)}</small>` : '';
  return `<tr><td>${clock(row.start)}</td><td class="num">${number(row.observed_kg)}</td>
    <td class="num">${row.baseline_kg === null ? '—' : number(row.baseline_kg)}</td>
    <td>${sourceTag(row)}${note}</td></tr>`;
}

function hourForms(plan, result, role) {
  const unseen = result.profile.filter(row => row.unseen);
  if (!unseen.length) return '';
  if (role !== 'supervisor') {
    return `<p class="subtle">${unseen.length} 个小时的设备组合未在同线历史出现过，等待能源主管逐小时核定。</p>`;
  }
  if (plan.state === 'approved') {
    return `<p class="subtle">${unseen.length} 个核定值已随复核单锁定，不能再改。</p>`;
  }
  return `<div class="baseline-forms"><h3>逐小时核定</h3><p class="subtle">陌生设备组合没有可比历史，请按现场经验补核定值。补齐后才能出整班偏差。</p>
    ${unseen.map(row => `<form class="fields baseline-form" data-path="/plans/${plan.id}/baselines/${row.end_read_id}">
      <div class="baseline-head"><b>${clock(row.start)}</b><span>实耗 ${number(row.observed_kg)} kg</span>${sourceTag(row)}</div>
      <label>核定基线 kg<input name="baseline_kg" type="number" min="0" step="0.01" value="${row.baseline_kg === null ? '' : number(row.baseline_kg)}" required></label>
      <label>核定依据<input name="note" minlength="4" maxlength="500" value="${esc(row.note || '')}" placeholder="现场经验或同类设备折算" required></label>
      <button>${row.source === 'manual' ? '更正核定值' : '保存核定值'}</button>${feedback}</form>`).join('')}</div>`;
}

function planDetail(plan, role) {
  const result = plan.result;
  if (!result) {
    return `<section class="shift-paper"><div class="sheet-header"><div><p class="eyebrow">${esc(LINE[plan.shift.line])} / 修订 ${plan.revision}</p>
      <h2>${esc(plan.title)}</h2>${shiftStamp(plan.shift)}</div>${status(plan.state, PLAN)}</div>
      ${busyPlan(plan.state) ? notice('方案正在处理', '计算完成后，小时负荷曲线与特征筛选会在这里更新。') : notice('估计失败', plan.error || '检查班次覆盖后重新提交，系统会保留本次修订。')}</section>`;
  }
  const pending = result.pending_count || 0;
  const complete = !pending && result.baseline_kg !== null;
  const deviation = complete ? `${result.excess_kg > 0 ? '+' : ''}${number(result.excess_kg)}` : '待补';
  return `<section class="shift-paper"><div class="sheet-header"><div><p class="eyebrow">${esc(LINE[plan.shift.line])} / 修订 ${plan.revision}</p>
    <h2>${esc(plan.title)}</h2>${shiftStamp(plan.shift)}</div>${status(plan.state, PLAN)}</div>
    <div class="gauges"><div><span>班次实耗</span><strong>${number(result.observed_kg)}<small> kg</small></strong></div>
      <div><span>模型基线</span><strong>${result.baseline_kg === null ? '—' : number(result.baseline_kg)}<small>${result.baseline_kg === null ? '' : ' kg'}</small></strong></div>
      <div><span>整班偏差</span><strong>${deviation}<small>${complete ? ' kg' : ` · ${pending} 小时待补`}</small></strong></div></div>
    <div class="analysis-grid"><section class="pane">${report('小时耗汽曲线', '<span class="subtle">kg / 小时</span>')}
      <div class="legend"><span>蒸汽实耗</span><span>模型基线</span>${result.profile.some(row => row.flagged) ? '<span>尖峰</span>' : ''}${pending ? '<span>待补核定</span>' : ''}</div><canvas id="load-chart" role="img" aria-label="班内小时蒸汽实耗柱状图及基线曲线"></canvas>
      <details class="table-fold"><summary>逐小时核定状态</summary><table><thead><tr><th>时点</th><th class="num">实耗 kg</th><th class="num">基线 kg</th><th>状态</th></tr></thead><tbody>${result.profile.map(hourlyRow).join('')}</tbody></table></details>
      ${hourForms(plan, result, role)}
    </section><section class="pane">${report('特征筛选', '<span class="subtle">互信息得分</span>')}
      <ol class="features">${result.features.map(feature => `<li><span>${esc(feature.name)}${feature.selected ? '<i>入模</i>' : ''}</span><b>${number(feature.score, 4)}</b></li>`).join('')}</ol>
      <dl class="metrics"><div><dt>历史留出 MAE</dt><dd>${number(result.metrics.holdout_mae_kg)} kg/h</dd></div>
      <div><dt>留出班次</dt><dd>${esc(result.sample.holdout_shift)}</dd></div>
      <div><dt>建模 / 留出小时</dt><dd>${result.sample.training} / ${result.sample.holdout}</dd></div>
      <div><dt>待补核定 / 标记尖峰</dt><dd>${pending} / ${result.spikes.length}</dd></div></dl></section></div>
    <div class="sheet-footer plan-actions">${plan.evidence ? `<button class="ghost" data-evidence="${plan.id}">下载估计证据</button>` : ''}
    ${plan.state === 'estimated' && role === 'supervisor' ? `<form data-path="/plans/${plan.id}/approve" data-route="reviews"><button ${complete ? '' : 'disabled'}>审批并发起节能复核</button>${complete ? '' : `<p class="subtle">还有 ${pending} 个小时缺少核定值，补齐后才能签批。</p>`}${feedback}</form>` : ''}
    ${plan.state === 'approved' ? '<p class="subtle">核定值已随复核单锁定。</p>' : ''}
    ${plan.review_id ? `<a class="button ghost" href="#reviews/${plan.review_id}">查看节能复核</a>` : ''}<p id="download-feedback" class="feedback" role="status" hidden></p></div></section>`;
}

export function plansPage(plans, plan, role, shifts, draft) {
  return `${heading('负荷建模', '负荷估计', '用同线已封账的前序班次，建立目标班的耗汽基线。')}
    <div class="desktop"><aside class="roster">${report('负荷方案', `<span class="subtle">${plans.length} 份</span>`)}
    ${plans.map(item => `<a class="roster-item ${item.id === plan?.id ? 'selected' : ''}" href="#plans/${item.id}"><div class="plan-item"><b>${esc(item.title)}</b><small>${esc(item.shift.code)} / R${item.revision}</small></div>${status(item.state, PLAN)}</a>`).join('')}
    ${role !== 'viewer' && shifts.length ? requestForm(shifts, draft) : ''}</aside>
    ${plan ? planDetail(plan, role) : notice('尚无负荷方案', '先在班次采集完成交接封账，再选一个班次提交。')}</div>`;
}
