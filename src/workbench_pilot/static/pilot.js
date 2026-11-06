'use strict';
const $ = id => document.getElementById(id);
const csrf = document.querySelector('meta[name="pilot-csrf"]').content;
const terminal = new Set(['completed','denied','cancelled','failed','uncertain','stale','expired','interrupted']);
let current = null, catalogue = [], pollGeneration = 0, busyRequest = false, lastStamp = '';

function showError(error) {
  $('error').textContent = error.message || String(error);
  $('error').hidden = false;
}
function clearError() { $('error').hidden = true; }
async function api(path, body) {
  const options = body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-Pilot-CSRF': csrf},
    body: JSON.stringify(body)
  };
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try { const result = await response.json(); if (typeof result.detail === 'string') detail = result.detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
}
function element(tag, text, parent, className = '') {
  const e = document.createElement(tag); e.textContent = text; e.className = className;
  parent.appendChild(e); return e;
}
function controls() {
  const active = current && !terminal.has(current.status);
  $('start').disabled = busyRequest || active;
  $('remember').disabled = busyRequest || active;
  $('forget').disabled = busyRequest || active;
  $('stop').disabled = !active && $('audio').paused;
  $('approve').disabled = busyRequest || !current || current.status !== 'pending';
  $('deny').disabled = $('approve').disabled;
  document.querySelectorAll('[data-fault]').forEach(b => {
    b.disabled = busyRequest || !current || current.status !== 'pending' || current.proposal?.kind !== 'fill_draft';
  });
}
async function facts() {
  const data = await api('/api/pilot/facts');
  $('facts').replaceChildren();
  for (const fact of data.facts) {
    const box = element('div', '', $('facts'), 'fact');
    element('span', fact.key + ' · revision ' + fact.revision, box);
    element('strong', fact.value, box);
  }
}
function render(row, forcePreview = false) {
  current = row;
  $('status').textContent = row.status;
  $('status').dataset.state = row.status;
  $('run-label').textContent = row.id.slice(0, 12);
  $('run-label').dataset.runId = row.id;
  $('empty').hidden = true; $('action').hidden = false;
  $('transcript').textContent = row.simulation?.transcript || 'Synthetic memory proposal; no microphone or model call.';
  const proposal = row.proposal;
  $('binding').replaceChildren();
  if (proposal) {
    const fields = proposal.binding.page ? {
      'Target': proposal.binding.page.origin,
      'Account': proposal.binding.page.account,
      'Recipient': proposal.binding.page.recipient,
      'Surface': proposal.binding.page.surface,
      'Fact revision': proposal.binding.fact.revision,
      'Action': proposal.kind
    } : {'Action': proposal.kind, 'Fact': proposal.args.fact_key,
         'Revision': proposal.binding.fact.revision ?? 'new', 'Scope': 'synthetic local memory'};
    for (const [label, value] of Object.entries(fields)) {
      const field = element('div', '', $('binding'));
      element('strong', label, field); element('span', String(value), field);
    }
    $('arguments').textContent = JSON.stringify(proposal.args, null, 2);
    $('expiry').textContent = 'Expires ' + new Date(proposal.expires_at * 1000).toLocaleTimeString();
  } else {
    $('arguments').textContent = 'Preparing the owned fixture…'; $('expiry').textContent = '';
  }
  $('run-reason').textContent = row.reason || (row.cancel_requested ?
    'Cancellation was requested during execution; inspect the actual receipt. No undo is claimed.' : '');
  $('receipt-wrap').hidden = !row.receipt;
  $('receipt').textContent = row.receipt ? JSON.stringify(row.receipt, null, 2) : '';
  if (row.preview_available && (lastStamp !== row.id + ':' + row.status || forcePreview)) {
    $('preview').src = `/api/pilot/runs/${row.id}/preview.png?v=${Date.now()}`;
    $('preview').hidden = false;
    $('preview-note').textContent = 'Screenshot from the real managed Chromium context. Saved text is independently read back in the receipt.';
  } else if (!row.preview_available) {
    $('preview').hidden = true;
    $('preview-note').textContent = row.proposal?.kind === 'fill_draft' ? 'Preview not available.' : 'No browser preview for this operation.';
  }
  lastStamp = row.id + ':' + row.status;
  controls();
}
async function watch(runId, generation) {
  while (generation === pollGeneration) {
    try {
      const row = await api('/api/pilot/runs/' + runId);
      if (generation !== pollGeneration) return;
      render(row);
      const events = await api(`/api/pilot/runs/${runId}/events`);
      if (generation !== pollGeneration) return;
      $('events').textContent = events.map(e => `${e.event}: ${e.from_status || 'new'} → ${e.to_status}`).join('\n');
      if (terminal.has(row.status)) { await facts(); return; }
    } catch (error) { showError(error); return; }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
}
function acceptRun(row) {
  render(row, true); sessionStorage.setItem('g1-run-id', row.id);
  const generation = ++pollGeneration;
  void watch(row.id, generation);
}
async function submit(operation) {
  if (busyRequest) return;
  clearError(); busyRequest = true; controls();
  try { await operation(); } catch (error) {
    showError(error);
    if (current) {
      try { render(await api('/api/pilot/runs/' + current.id)); } catch (_) {}
    }
  } finally { busyRequest = false; controls(); }
}
$('start').onclick = () => submit(async () => {
  const row = await api('/api/pilot/runs', {request_id: crypto.randomUUID(),
    fixture_id: $('fixture').value, surface: $('surface').value});
  acceptRun(row);
});
$('fixture').onchange = () => {
  if (!current || terminal.has(current.status))
    $('transcript').textContent = catalogue.find(f => f.id === $('fixture').value)?.transcript || '';
};
for (const [id, approved] of [['approve', true], ['deny', false]]) {
  $(id).onclick = () => submit(async () => {
    if (!current?.proposal) return;
    const action = current.proposal;
    const row = await api('/api/pilot/approvals/' + action.id, {approved, digest: action.digest});
    acceptRun(row);
  });
}
$('stop').onclick = () => {
  $('audio').pause(); $('audio').currentTime = 0;
  if (current && !terminal.has(current.status)) void submit(async () => {
    acceptRun(await api(`/api/pilot/runs/${current.id}/cancel`, {}));
  });
};
$('tone').onclick = async () => {
  try { $('audio').currentTime = 0; await $('audio').play(); }
  catch (_) { showError(new Error('Test-tone playback was blocked. This does not indicate TTS support.')); }
};
for (const [id, deleting] of [['remember', false], ['forget', true]]) {
  $(id).onclick = () => submit(async () => {
    const row = await api('/api/pilot/memory/proposals', {request_id: crypto.randomUUID(),
      key: $('fact-key').value, value: deleting ? null : $('fact-value').value, delete: deleting});
    acceptRun(row);
  });
}
document.querySelectorAll('[data-fault]').forEach(button => {
  button.onclick = () => submit(async () => {
    if (!current) return;
    const row = await api(`/api/pilot/runs/${current.id}/fault`, {kind: button.dataset.fault});
    render(row, true); await facts();
  });
});
(async () => {
  try {
    const info = await api('/api/pilot/status');
    $('capabilities').textContent = JSON.stringify(info, null, 2);
    $('faults').hidden = !info.fault_injection;
    catalogue = await api('/api/pilot/fixtures');
    for (const fixture of catalogue) {
      const option = element('option', fixture.title, $('fixture')); option.value = fixture.id;
    }
    $('transcript').textContent = catalogue[0]?.transcript || '';
    await facts();
    const previous = sessionStorage.getItem('g1-run-id');
    if (previous) {
      try { acceptRun(await api('/api/pilot/runs/' + previous)); }
      catch (_) { sessionStorage.removeItem('g1-run-id'); }
    }
  } catch (error) { showError(error); }
})();

$('audio').addEventListener('play', controls);
$('audio').addEventListener('pause', controls);
$('audio').addEventListener('ended', controls);
