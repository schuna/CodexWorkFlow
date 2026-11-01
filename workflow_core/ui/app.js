'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const token = $('meta[name="workflow-token"]').content;
const labels = {ready:'실행 준비',completed:'완료',running:'실행 중',verifying:'검증 중',recording:'기록 중',failed:'실패',unverified:'미검증',needs_input:'정보 필요',interrupted:'중단됨'};
const reasons = {cancelled:'사용자가 중단했습니다.',interrupted:'실행이 중단되었습니다.',timeout:'실행 시간 한도에 도달했습니다.',nonzero_exit:'Codex가 오류 코드로 종료되었습니다. 설치·계정·실행 권한을 확인하세요.',execution_failed:'Codex가 실행 실패를 보고했습니다.',verification_failed:'지정 검증이 실패했습니다. 아래 실패 원인을 확인한 뒤 이어서 해결하세요.',incomplete_response:'완료 이벤트를 받지 못했습니다.',invalid_response:'완료 기준에 대한 응답 형식을 확인하지 못했습니다.',output_too_large:'출력 크기 한도를 넘겨 완료로 판정하지 않았습니다.',FileNotFoundError:'실행 파일을 찾을 수 없습니다.',process_control_error:'프로세스를 제어하는 중 오류가 발생했습니다.'};
let snapshot, selected, environment, draft, editorRevision, initialProgressHash, activeTab = 'summary', lastSignature = '', fetching = false;
let diffText = null;
let seenWorkerError = null;

async function api(path, data) {
  const response = await fetch('/api/' + path, {method:data === undefined ? 'GET' : 'POST', headers:{'X-Workflow-Token':token,'Content-Type':'application/json'}, body:data === undefined ? undefined : JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '요청을 처리하지 못했습니다.');
  return result;
}
function showError(error) {
  const dialog = $('dialog[open]');
  if (dialog) {
    let message = $('.form-error', dialog);
    if (!message) {message = document.createElement('p'); message.className = 'form-error'; message.setAttribute('role','alert'); dialog.append(message);}
    message.textContent = error.message || String(error); message.scrollIntoView({block:'nearest'}); return;
  }
  const box = $('#error');
  box.hidden = false;
  box.replaceChildren();
  const close = document.createElement('button');
  close.textContent = '닫기 ×';
  close.onclick = () => {box.hidden = true;};
  box.append(close, document.createTextNode(error.message || String(error)));
}
async function act(path, data = {}) {
  $('#error').hidden = true;
  seenWorkerError = null;
  try {const result = await api(path, data); diffText = null; await refresh(true); return result;}
  catch (error) {showError(error); return null;}
}
function badge(status) {return `<span class="status ${escape(status)}">${escape(labels[status] || status)}</span>`;}
function metric(value) {return value == null ? '알 수 없음' : Number(value).toLocaleString('ko-KR');}
function usageText(totals) {
  if (!totals || !totals.run_count) return '실행 전';
  if (totals.complete) return metric(totals.total_tokens) + ' 토큰';
  return '관측분 ' + metric(totals.observed_tokens) + ' · ' + totals.missing_run_count + '회 미제공';
}
function currentTask() {return snapshot?.state?.tasks.find(t => t.id === selected);}
function isBlocked() {return snapshot?.busy || snapshot?.active || snapshot?.conflict || (snapshot?.execution && snapshot.execution.phase !== 'recorded');}

async function refresh(force = false) {
  if (fetching) return;
  fetching = true;
  try {
    const next = await api('status');
    const wasBusy = snapshot?.busy || snapshot?.active;
    snapshot = next;
    $('#connection').textContent = '연결됨';
    const parts = next.project.replace(/\\/g, '/').split('/').filter(Boolean);
    $('#project-name').textContent = parts.at(-1) || next.project;
    $('#project-path').textContent = next.project;
    if (next.state && !next.state.tasks.some(t => t.id === selected)) selected = next.ready[0] || next.execution?.task_id || next.state.tasks[0]?.id;
    if (next.active && next.execution?.task_id) selected = next.execution.task_id;
    if (wasBusy && !next.busy && !next.active) {
      diffText = null; loadEnvironment();
      if (next.execution?.task_id === selected && next.state.tasks.find(t => t.id === selected)?.status === 'completed' && next.ready.length) selected = next.ready[0];
    }
    const signature = JSON.stringify(next);
    if (force || signature !== lastSignature) {lastSignature = signature; render();}
    if (next.worker_error && next.worker_error !== seenWorkerError) showError(new Error(next.worker_error));
    seenWorkerError = next.worker_error;
  } catch (error) {
    $('#connection').textContent = '연결 끊김 · 재시도 중';
    if (force) showError(error);
  } finally {fetching = false;}
}
async function loadEnvironment() {
  try {environment = await api('environment'); render();} catch (error) {showError(error);}
}
function environmentRows() {
  if (!environment) return '<p class="muted">실행 환경 확인 중…</p>';
  return `<div class="environment-row"><span>Codex 설치</span><strong>${environment.codex_installed ? '확인됨' : '설치 필요'}</strong></div>
    <div class="environment-row"><span>계정 인증</span><strong>${environment.authenticated === true ? '연결됨' : environment.authenticated === false ? '로그인 필요' : '확인 필요'}</strong></div>
    <div class="environment-row"><span>Git 프로젝트</span><strong>${environment.git ? '확인됨' : '확인 필요'}</strong></div>
    <div class="environment-row"><span>프로젝트 지침</span><strong>${environment.instructions ? '기존 지침 사용' : '추가 지침 없음'}</strong></div>
    ${environment.codex_installed && environment.authenticated === false ? '<button class="secondary" id="login">계정 연결</button>' : ''}`;
}
function render() {
  if (!snapshot) return;
  $('#edit-nav').disabled = !!(snapshot.busy || snapshot.active);
  $('#open-project').disabled = !!(snapshot.busy || snapshot.active);
  const main = $('#main');
  if (!snapshot.initialized) {
    main.innerHTML = `<div class="page-intro"><div><span class="eyebrow">YOUR NEXT CHAPTER</span><h1>다음 작업을 시작할 준비.</h1><p>목표를 정하고, 한 번에 하나씩 완성하세요.</p></div></div>
      <div class="empty-layout"><section class="card welcome"><div class="welcome-mark">↗</div><span class="eyebrow">START WITH A GOAL</span><h2>어떤 결과를 만들고 싶으세요?</h2><p>목표를 작업으로 나누고 완료 기준을 검토해 보세요.<br>실행부터 검증, 결과 기록까지 한 흐름으로 이어집니다.</p><button class="primary" id="setup">목표와 작업 만들기 <span>↗</span></button></section><section class="card environment"><h3>프로젝트 준비 상태</h3>${environmentRows()}</section></div>`;
    $('#setup').onclick = () => openEditor();
    bindLogin();
    return;
  }
  const state = snapshot.state;
  const done = state.tasks.filter(t => t.status === 'completed').length;
  const task = currentTask();
  const latest = task?.executions?.at(-1);
  const totals = snapshot.task_usage?.[selected];
  const record = snapshot.execution;
  const runningTask = state.tasks.find(t => t.id === record?.task_id);
  const blocked = isBlocked();
  let phase = record?.phase;
  if (!snapshot.active && !snapshot.busy && phase && !['recorded','recording'].includes(phase)) phase = 'interrupted';
  const stages = ['준비','실행','검증','기록','완료'];
  const phaseIndex = {running:1,verifying:2,recording:3,recorded:4}[phase] ?? 0;
  const showFinished = task?.status === 'completed';
  const currentStage = (snapshot.active || snapshot.busy) ? (phase === 'recorded' ? 0 : phaseIndex) : showFinished ? 4 : 0;
  let notice = '';
  if (snapshot.conflict) {
    notice = '<section class="notice"><strong>진행 기록이 외부에서 수정되었습니다.</strong>저장된 상태와 외부 수정 내용을 비교한 뒤 필요한 내용을 보존하세요.<br><button class="secondary" id="show-conflict">두 내용 비교하고 해결</button></section>';
  } else if (record && record.phase !== 'recorded' && !snapshot.busy && !snapshot.active) {
    if (record.phase === 'recording') {
      notice = `<section class="notice"><strong>실행 결과가 기록을 기다리고 있습니다.</strong>저장 문제를 해결한 뒤 기록만 다시 시도하세요. 코드와 검증은 반복 실행하지 않습니다.
        <div class="button-group"><button class="secondary" id="retry-record">기록 다시 시도</button></div>
        <details class="reconcile"><summary>실행 중 계획이 변경된 경우</summary><p>실행 기준: 리비전 ${record.source_revision} / 현재: ${state.revision}. 변경된 상태를 검토한 후 결과를 실패로 기록할 수 있습니다.</p><button class="secondary" id="reconcile">현재 상태에 실패로 기록</button></details></section>`;
    } else {
      notice = `<section class="notice"><strong>중단된 실행을 확인해 주세요.</strong>남은 프로세스와 부분 변경을 확인하세요. 복구는 결과만 저장하며 코드를 실행하지 않습니다.
        <label><input type="checkbox" id="processes-checked">남은 실행 프로세스가 종료되었고, 부분 변경을 확인했습니다.</label><button class="secondary" id="recover" disabled>중단 상태 복구</button></section>`;
    }
  }
  if (environment && (!environment.codex_installed || environment.authenticated === false || !environment.git)) {
    notice += `<section class="notice"><strong>실행 환경을 확인해 주세요.</strong>${!environment.codex_installed ? 'Codex 설치를 확인한 뒤 새로고침하세요. ' : ''}${!environment.git ? 'Git 프로젝트 폴더를 선택하세요. ' : ''}${environment.authenticated === false ? '계정을 연결하면 작업을 시작할 수 있습니다. <button class="secondary" id="login">계정 연결</button>' : ''}</section>`;
  }
  const blocker = snapshot.run_blockers?.[selected];
  if (blocker) {
    const message = {'task retry limit reached':'작업별 추가 실행 횟수 한도에 도달했습니다.','task token limit reached':'작업별 누적 토큰 한도에 도달했습니다.','usage unknown; cannot enforce task token limit':'이전 실행 사용량이 없어 설정한 토큰 한도를 확인할 수 없습니다.'}[blocker] || blocker;
    notice += `<section class="notice"><strong>후속 실행이 제한되었습니다.</strong>${escape(message)}<br><button class="secondary" id="review-limits">실행 한도 검토</button></section>`;
  }
  const allowed = !blocked && snapshot.ready.includes(selected) && environment?.codex_installed !== false && environment?.authenticated !== false && environment?.git !== false;
  let action = '작업 시작';
  if (task && ['failed','interrupted','unverified','needs_input'].includes(task.status)) action = '이어서 해결';
  else if (done > 0) action = '다음 작업 시작';
  if (done === state.tasks.length) action = '모든 작업 완료';
  const active = !!(snapshot.active || snapshot.busy);
  const button = active ? `<button class="danger" id="stop" ${!['running','verifying'].includes(record?.phase) ? 'disabled' : ''}>작업 중단 <span>■</span></button>` : `<button class="primary" id="run" ${!allowed ? 'disabled' : ''}>${action} <span>↗</span></button>`;
  const reason = task?.dependencies?.length ? '앞선 작업의 완료를 확인한 뒤 실행합니다.' : '다른 작업 없이 독립적으로 시작할 수 있습니다.';
  const liveProgress = active && record?.progress?.message ? `<div class="live-progress"><strong>실시간 진행 상태</strong><span>${escape(record.progress.message)}${record.progress.event_count ? ' · 이벤트 ' + metric(record.progress.event_count) + '건' : ''}</span></div>` : '';
  main.innerHTML = `<div class="page-intro"><div><span class="eyebrow">PROJECT OVERVIEW</span><h1>${escape(state.goal)}</h1><p>작업에 집중하세요. 결과는 다음 단계로 이어집니다.</p><div class="overview-meta"><span class="chip">${state.tasks.length}개 작업</span><span>${done}개 완료</span><span>리비전 ${state.revision}</span></div></div><button class="secondary" id="edit-plan" ${active ? 'disabled' : ''}>계획 수정 <span>↗</span></button></div>
    ${notice}<div class="board-grid"><div><section class="card"><div class="card-head"><h3>${active ? '현재 진행 중인 작업' : '지금 진행할 작업'}</h3><span>${active ? escape(labels[record?.phase] || '처리 중') : allowed ? '실행 준비됨' : task?.status === 'completed' ? '작업 완료' : '상태 확인'}</span></div>
    <div class="stage-rail">${stages.map((name,i) => `<span class="stage ${i < currentStage ? 'done' : i === currentStage ? 'current' : ''}"><i>${i < currentStage ? '✓' : String(i+1).padStart(2,'0')}</i>${name}</span>`).join('')}</div>
    <div class="current-task"><div class="task-kicker"><span class="eyebrow">${active ? 'IN PROGRESS' : 'FOCUS TASK'} / ${String(state.tasks.indexOf(task)+1).padStart(2,'0')}</span>${task ? badge(task.status) : ''}</div><h2>${escape(task?.title || '작업을 선택하세요')}</h2><ul class="criteria">${(task?.acceptance_criteria || []).map(c => `<li>${escape(c)}</li>`).join('')}</ul>
    ${task?.status === 'needs_input' ? '<p class="muted">목표와 계획에서 필요한 답변을 추가한 뒤 이어서 실행하세요.</p><button class="text-button" id="answer">정보 보완하기 ↗</button>' : ''}${liveProgress}
    <div class="action-row"><span>${active ? '브라우저를 닫아도 서비스가 실행 중이면 작업은 유지됩니다.' : escape(reason)}<br>검증 · 결과 기록 · 다음 작업 준비는 자동으로 처리합니다.</span>${button}</div></div></section>
    <section class="card task-list"><div class="card-head"><h3>작업 흐름</h3><span>${done} / ${state.tasks.length} 완료</span></div>${state.tasks.map((t,i) => `<button class="task-row ${escape(t.status)} ${t.id === selected ? 'chosen' : ''}" data-task="${escape(t.id)}" aria-pressed="${t.id === selected}" ${active ? 'disabled' : ''}><span class="task-number">${t.status === 'completed' ? '✓' : String(i+1).padStart(2,'0')}</span><span class="task-name">${escape(t.title)}<span class="task-id">${t.dependencies.length ? '앞선 작업 ' + t.dependencies.length + '개 완료 필요' : '독립 작업'}</span></span>${badge(t.status)}</button>`).join('')}</section></div>
    <aside class="insights"><section class="card insight"><h3>프로젝트 진행 <span>PROGRESS</span></h3><div class="big-number">${done}<small>/ ${state.tasks.length} 작업</small></div><div class="progress-track"><div class="progress-fill" id="progress-fill"></div></div><p>${done === state.tasks.length ? '계획한 작업을 모두 완료했습니다.' : '한 단계씩, 목표에 가까워지고 있습니다.'}</p></section>
    <section class="card insight"><h3>맥락과 사용량 <span>CONTEXT</span></h3><div class="big-number" id="task-total-tokens">${totals?.run_count ? metric(totals.total_tokens) : '실행 전'}${totals?.total_tokens != null ? '<small>토큰</small>' : ''}</div><p>작업 누적 · 실패와 재시도 포함${totals?.run_count ? ' · ' + totals.run_count + '회 실행' : ''}</p>${totals?.missing_run_count ? '<p class="pill-bad">' + escape(usageText(totals)) + ' · 총량 알 수 없음</p>' : ''}<div class="metric-line"><span>최근 실행</span><strong>${latest ? metric(latest.tokens) + (latest.tokens != null ? ' 토큰' : '') : '—'}</strong></div><div class="metric-line"><span>프로젝트 누적</span><strong id="project-total-tokens">${escape(usageText(snapshot.project_usage))}</strong></div><div class="metric-line"><span>다음 입력 맥락</span><strong>${snapshot.context_chars[selected] == null ? '—' : metric(snapshot.context_chars[selected]) + '자'}</strong></div><div class="metric-line"><span>누적 입력 / 출력</span><strong>${totals?.run_count ? metric(totals.input_tokens) + ' / ' + metric(totals.output_tokens) : '—'}</strong></div><div class="metric-line"><span>누적 캐시 입력</span><strong>${totals?.run_count ? metric(totals.cached_input_tokens) : '—'}</strong></div>${latest?.over_token_limit ? '<p class="pill-bad">작업 토큰 한도를 초과했습니다.</p>' : ''}</section>
    <section class="card insight tip"><div class="leaf">↗</div><h3>다음 작업도 가볍게.</h3><p>필요한 작업과 인계만 다음 실행에 담습니다. 이전 대화 전체를 다시 전달하지 않습니다.</p></section></aside></div>
    <section class="card result"><div class="result-tabs"><button class="result-tab ${activeTab === 'summary' ? 'active' : ''}" data-tab="summary">실행 결과</button><button class="result-tab ${activeTab === 'diff' ? 'active' : ''}" data-tab="diff">변경 내용</button><button class="result-tab ${activeTab === 'handoff' ? 'active' : ''}" data-tab="handoff">인계와 맥락</button></div><div class="result-content" id="result-content"></div></section>`;
  $('#progress-fill').style.width = (state.tasks.length ? done / state.tasks.length * 100 : 0) + '%';
  $('#edit-plan').onclick = () => openEditor();
  if ($('#answer')) $('#answer').onclick = () => openEditor();
  if ($('#review-limits')) $('#review-limits').onclick = () => openEditor();
  if ($('#run')) $('#run').onclick = () => act('run', {task_id:selected,revision:state.revision});
  if ($('#stop')) $('#stop').onclick = async event => {event.currentTarget.disabled = true; await act('stop');};
  if ($('#retry-record')) $('#retry-record').onclick = () => act('record');
  if ($('#reconcile')) $('#reconcile').onclick = () => act('reconcile', {revision:state.revision});
  if ($('#processes-checked')) $('#processes-checked').onchange = event => {$('#recover').disabled = !event.target.checked;};
  if ($('#recover')) $('#recover').onclick = () => act('recover', {processes_checked:$('#processes-checked').checked});
  if ($('#show-conflict')) $('#show-conflict').onclick = openConflict;
  $$('[data-task]').forEach(button => button.onclick = () => {selected = button.dataset.task; activeTab = 'summary'; render();});
  $$('[data-tab]').forEach(button => button.onclick = () => {activeTab = button.dataset.tab; render();});
  renderResult(); bindLogin();
}
function bindLogin() {if ($('#login')) $('#login').onclick = () => act('login');}

function renderResult() {
  const box = $('#result-content');
  if (!box) return;
  let task = currentTask();
  if (!task) {box.textContent = '작업을 선택하세요.'; return;}
  if (activeTab === 'diff') {
    box.innerHTML = '<p class="muted">현재 작업 트리 전체 변경입니다. 실행 전부터 있던 변경도 포함합니다. 최대 300,000자를 표시합니다.</p><pre id="diff-output">변경 내용을 불러오고 있습니다…</pre>';
    if (diffText != null) $('#diff-output').textContent = diffText || '현재 표시할 변경이 없습니다.';
    else api('diff').then(result => {diffText = result.text; const output = $('#diff-output'); if (output) output.textContent = diffText || '현재 표시할 변경이 없습니다.';}).catch(showError);
    return;
  }
  if (activeTab === 'handoff') {
    const state = snapshot.state;
    const deps = (task.dependencies || []).map(id => state.tasks.find(t => t.id === id)).filter(Boolean);
    const depText = deps.length ? deps.map(d => `${d.title} (${labels[d.status] || d.status})${d.last_result?.summary ? '\n- 요약: ' + d.last_result.summary : ''}${d.changed_files?.length ? '\n- 변경 경로: ' + d.changed_files.join(', ') : ''}`).join('\n\n') : '해당 없음';
    box.innerHTML = `<dl class="detail-list"><dt>목표</dt><dd>${escape(state.goal)}</dd><dt>작업 맥락</dt><dd>${escape(task.title)}<br>${escape(task.acceptance_criteria.join('\n'))}</dd><dt>선행 작업 인계</dt><dd>${escape(depText)}</dd><dt>제약</dt><dd>${escape(task.constraints.join('\n') || '해당 없음')}</dd><dt>프로젝트 결정</dt><dd>${escape(state.decisions.join('\n') || '해당 없음')}</dd><dt>미해결 사항</dt><dd>${escape(state.open_questions.join('\n') || '해당 없음')}</dd><dt>추가 정보</dt><dd>${escape(task.user_notes || '해당 없음')}</dd><dt>다음 행동</dt><dd>${escape(task.next_action)}</dd><dt>근거</dt><dd>현재 상태 리비전 ${state.revision}. 기존 지침은 실행기가 읽습니다.</dd></dl>`;
    return;
  }
  if (!task.last_result && !task.verification.length && snapshot.execution?.phase === 'recorded') {
    task = snapshot.state.tasks.find(t => t.id === snapshot.execution.task_id) || task;
  }
  if (!task.last_result && !task.verification.length) {
    box.innerHTML = '<div class="empty-result"><span class="empty-symbol">▤</span><span>작업이 끝나면 변경 요약과 검증 결과가 여기에 표시됩니다.</span></div>';
    return;
  }
  const result = task.last_result;
  box.innerHTML = `<h3>${escape(task.title)} · 실행 결과</h3>${result?.summary ? '<p>' + escape(result.summary) + '</p>' : ''}${result?.reason ? '<p>실행 상태: ' + escape(reasons[result.reason] || result.reason) + '</p>' : ''}${result?.exit_code != null ? '<p class="muted">종료 코드 ' + escape(result.exit_code) + '</p>' : ''}
    ${task.verification.length ? task.verification.map(check => `<div class="check ${check.passed ? '' : 'failed'}"><span class="check-icon">${check.passed ? '✓' : '×'}</span><span><strong>${check.passed ? '검증 통과' : '검증 실패'}</strong> · 종료 코드 ${escape(check.exit_code ?? '알 수 없음')}<br><code>${escape(check.command.map(c => String(c)).join(' '))}</code>${check.reason ? '<br>' + escape(reasons[check.reason] || check.reason) : ''}</span></div>${!check.passed ? '<details class="failure-evidence"><summary>' + escape(check.diagnostics?.summary || '상세 오류가 보존되지 않은 이전 검증입니다.') + '</summary><pre>' + escape(check.diagnostics?.excerpt || '새 검증부터 제한된 오류 요약을 보존합니다.') + '</pre>' + (check.diagnostics?.truncated ? '<small>오류 출력의 일부만 표시합니다.</small>' : '') + '</details>' : ''}`).join('') : '<p class="muted">검증이 실행되지 않았습니다.</p>'}
    <p>이번 실행에서 달라진 경로: ${escape(task.changed_files.join(', ') || '없음')}</p>
    ${(task.executions || []).length ? '<details class="run-history"><summary>실행 이력 ' + task.executions.length + '회 · ' + escape(usageText(snapshot.task_usage?.[task.id])) + '</summary><ul>' + task.executions.slice(-10).map((entry,i) => '<li>' + (Math.max(0,task.executions.length - 10) + i + 1) + '회 · ' + escape(labels[entry.status] || (task.failed_approaches.some(f => f.run_id === entry.run_id) ? '실패' : '기록됨')) + ' · ' + metric(entry.tokens) + (entry.tokens != null ? ' 토큰' : '') + '</li>').join('') + '</ul></details>' : ''}
    <div class="minor-actions"><button class="text-button" id="verify-again" ${isBlocked() ? 'disabled' : ''}>검증 다시 실행 ↗</button><span class="environment-pill">${snapshot.execution?.phase === 'recorded' && snapshot.execution.task_id === task.id ? '상태와 인계 자동 저장됨' : ''}</span></div>`;
  $('#verify-again').onclick = () => act('verify', {task_id:task.id,revision:snapshot.state.revision});
}

function openEditor() {
  if (isBlocked()) {showError(new Error('실행 결과 기록 또는 복구를 먼저 완료해 주세요.')); return;}
  const state = snapshot.state;
  $$('.form-error').forEach(e => e.remove());
  initialProgressHash = snapshot.existing_progress?.sha256;
  $('#existing-progress').hidden = !snapshot.existing_progress;
  $('#initial-progress-note').value = snapshot.existing_progress?.text || '';
  editorRevision = state?.revision;
  draft = state ? structuredClone({goal:state.goal,tasks:state.tasks,policy:state.policy}) : {goal:'',tasks:[],policy:{max_retries:1}};
  $('#goal').value = draft.goal;
  const fields = {'max-retries':'max_retries','max-seconds':'max_seconds','verification-seconds':'verification_seconds','token-limit':'task_token_limit','context-limit':'context_char_limit'};
  Object.entries(fields).forEach(([id,key]) => {$('#' + id).value = draft.policy[key] ?? (key === 'max_retries' ? 1 : '');});
  $('#suggest').disabled = draft.tasks.some(t => t.attempts);
  $('#suggest-note').textContent = draft.tasks.some(t => t.attempts) ? '실행 이력이 있는 작업은 유지됩니다. 필요하면 새 작업을 추가하세요.' : '목표의 각 줄을 작업 초안으로 정리합니다.';
  renderEditors(); $('#plan-dialog').showModal();
}
function captureEditors() {
  $$('.task-editor').forEach((card,i) => {
    const t = draft.tasks[i];
    t.title = $('[name=title]',card).value;
    t.acceptance_criteria = $('[name=criteria]',card).value.split('\n').filter(s => s.trim());
    t.command_lines = $('[name=commands]',card).value.split('\n').filter(s => s.trim());
    t.constraints = $('[name=constraints]',card).value.split('\n').filter(s => s.trim());
    t.user_notes = $('[name=notes]',card).value;
    t.dependencies = [...$('[name=dependencies]',card).selectedOptions].map(o => o.value);
  });
}
function renderEditors() {
  $('#task-editors').innerHTML = draft.tasks.map((t,i) => `<section class="task-editor"><header><span>작업 ${String(i+1).padStart(2,'0')}</span><button type="button" class="text-button remove-task" data-index="${i}" ${t.attempts ? 'disabled' : ''}>삭제</button></header>
    <label>작업 제목<input name="title" value="${escape(t.title)}" required></label><label>완료 기준 · 한 줄에 하나<textarea name="criteria" rows="2" required>${escape(t.acceptance_criteria.join('\n'))}</textarea></label>
    <label>검증 명령 · 한 줄에 하나<textarea name="commands" rows="2" placeholder="python -m unittest discover -s tests -v">${escape((t.command_lines || []).join('\n'))}</textarea></label><small>실행할 명령을 검토하세요. 지정하지 않으면 결과는 미검증으로 남습니다. 공백이 있는 경로는 큰따옴표로 감쌉니다.</small>
    <details ${t.status === 'needs_input' ? 'open' : ''}><summary>제약 · 선행 작업 · 추가 정보</summary><div class="editor-two"><label>필수 제약 · 한 줄에 하나<textarea name="constraints" rows="2">${escape(t.constraints.join('\n'))}</textarea></label><label>먼저 완료할 작업<select name="dependencies" multiple size="3">${draft.tasks.filter(other => other.id !== t.id).map(other => `<option value="${escape(other.id)}" ${t.dependencies.includes(other.id) ? 'selected' : ''}>${escape(other.title)}</option>`).join('')}</select></label></div><label>추가 정보 · 질문에 대한 답변<textarea name="notes" rows="3">${escape(t.user_notes || '')}</textarea></label></details></section>`).join('');
  $$('.remove-task').forEach(button => button.onclick = () => {
    captureEditors(); const [removed] = draft.tasks.splice(Number(button.dataset.index),1);
    draft.tasks.forEach(t => {t.dependencies = t.dependencies.filter(id => id !== removed.id);}); renderEditors();
  });
}
$('#suggest').onclick = async () => {
  $('#suggest').disabled = true;
  try {const result = await api('suggest',{goal:$('#goal').value}); draft.tasks = result.tasks; renderEditors(); $('#suggest-note').textContent = '작업 초안입니다. 제목·완료 기준·검증 명령을 검토하세요.';}
  catch (error) {showError(error);} finally {$('#suggest').disabled = false;}
};
$('#add-task').onclick = () => {
  captureEditors(); draft.tasks.push({id:'task-' + crypto.randomUUID().slice(0,8),title:'',acceptance_criteria:[],constraints:[],dependencies:[],command_lines:draft.tasks[0]?.command_lines || [],user_notes:''}); renderEditors();
};
$('#plan-form').onsubmit = async event => {
  event.preventDefault(); captureEditors();
  const policy = structuredClone(draft.policy);
  const fields = {'max-retries':'max_retries','max-seconds':'max_seconds','verification-seconds':'verification_seconds','token-limit':'task_token_limit','context-limit':'context_char_limit'};
  Object.entries(fields).forEach(([id,key]) => {const value = $('#' + id).value; if(value === '') delete policy[key]; else policy[key] = Number(value);});
  try {await api('plan',{goal:$('#goal').value,tasks:draft.tasks,policy,revision:editorRevision,existing_progress_sha256:initialProgressHash,progress_note:initialProgressHash ? $('#initial-progress-note').value : undefined}); $('#plan-dialog').close(); window.scrollTo({top:0}); selected = null; diffText = null; await refresh(true);}
  catch (error) {showError(error);}
};
function openConflict() {
  $$('.form-error', $('#conflict-dialog')).forEach(e => e.remove());
  $('#generated-progress').textContent = snapshot.conflict.generated;
  $('#external-progress').textContent = snapshot.conflict.external;
  $('#conflict-note').value = '';
  $('#conflict-dialog').dataset.revision = snapshot.state.revision;
  $('#conflict-dialog').dataset.hash = snapshot.conflict.external_sha256;
  $('#conflict-dialog').showModal();
}
$('#resolve-conflict').onclick = async () => {
  const dialog = $('#conflict-dialog');
  try {await api('resolve',{revision:Number(dialog.dataset.revision),external_sha256:dialog.dataset.hash,note:$('#conflict-note').value}); dialog.close(); await refresh(true);}
  catch (error) {showError(error);}
};
$('#open-project').onclick = () => {$$('.form-error', $('#project-dialog')).forEach(e => e.remove()); $('#folder').value = snapshot.project; $('#project-dialog').showModal();};
$('#choose-folder').onclick = async () => {try {const result = await api('choose-folder',{}); if(result.project) $('#folder').value = result.project;} catch(error) {showError(error);}};
$('#project-form').onsubmit = async event => {
  event.preventDefault();
  try {await api('open',{project:$('#folder').value}); $('#project-dialog').close(); selected=null; diffText=null; environment=null; await refresh(true); await loadEnvironment();} catch(error) {showError(error);}
};
$('#edit-nav').onclick = () => openEditor();
$('#board-nav').onclick = () => window.scrollTo({top:0,behavior:'smooth'});
$('#refresh').onclick = () => {refresh(true); loadEnvironment();};
$$('.close-dialog').forEach(button => button.onclick = () => button.closest('dialog').close());
refresh(true).then(loadEnvironment);
setInterval(() => refresh(), 1500);
