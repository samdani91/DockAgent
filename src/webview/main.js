const vscode = acquireVsCodeApi();

const messagesEl    = document.getElementById('messages');
const chatInput     = document.getElementById('chatInput');
const sendBtn       = document.getElementById('sendBtn');
const clearBtn      = document.getElementById('clearBtn');
const stopBtn       = document.getElementById('stopBtn');
const actionsHeader = document.getElementById('actionsHeader');
const actionsBody   = document.getElementById('actionsBody');
const actionsToggle = document.getElementById('actionsToggle');
const thresholdInput = document.getElementById('thresholdInput');
const statusStrip = document.getElementById('statusStrip');
const statusLabel = document.getElementById('statusLabel');
const statusMeta  = document.getElementById('statusMeta');
const statusTime  = document.getElementById('statusTime');

let isWaiting  = false;
let actionsOpen = true;
let runStartedAt = 0;
let timerId = null;

// ── Interactive control state ──
function updateControls() {
  sendBtn.disabled = isWaiting || chatInput.value.trim().length === 0;
  clearBtn.disabled = isWaiting;
  document.querySelectorAll('.da-action-btn').forEach(btn => {
    btn.disabled = isWaiting;
    btn.setAttribute('aria-disabled', String(isWaiting));
  });
  if (thresholdInput) {
    thresholdInput.disabled = isWaiting;
  }
}

// ── Auto-resize textarea ──
chatInput.addEventListener('input', () => {
  chatInput.style.height = 'auto';
  chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';
  updateControls();
});

// ── Send on Enter, Shift+Enter for new line ──
chatInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

sendBtn.addEventListener('click', sendMessage);

// ── Clear chat (preserve welcome message) ──
clearBtn.addEventListener('click', () => {
  while (messagesEl.children.length > 1) {
    messagesEl.removeChild(messagesEl.lastChild);
  }
  endRun();
  vscode.setState({ history: [], threshold: thresholdInput?.value ?? '0' });
  vscode.postMessage({ type: 'clear-history' });
});

stopBtn.addEventListener('click', () => {
  if (!isWaiting || stopBtn.disabled) { return; }
  stopBtn.disabled = true;
  setStatus('Stopping');
  vscode.postMessage({ type: 'stop-run' });
});

// ── Save and constrain threshold state ──
thresholdInput?.addEventListener('keydown', (e) => {
  if (['-', '+', 'e', 'E', '.'].includes(e.key)) {
    e.preventDefault();
  }
});

thresholdInput?.addEventListener('input', () => {
  if (!thresholdInput.value) { return; }
  const next = Math.max(0, Math.min(20, parseInt(thresholdInput.value, 10) || 0));
  thresholdInput.value = String(next);
});

thresholdInput?.addEventListener('change', () => {
  const saved = vscode.getState() || {};
  const next = Math.max(0, Math.min(20, parseInt(thresholdInput.value, 10) || 0));
  thresholdInput.value = String(next);
  vscode.setState({ ...saved, threshold: thresholdInput.value });
});

// ── Collapsible quick actions ──
actionsToggle.addEventListener('click', () => {
  actionsOpen = !actionsOpen;
  actionsBody.style.display = actionsOpen ? '' : 'none';
  const icon = actionsToggle.querySelector('span');
  if (icon) {
    icon.textContent = actionsOpen ? '⌃' : '⌄';
  }
  actionsToggle.title = actionsOpen ? 'Collapse actions' : 'Expand actions';
  actionsToggle.setAttribute('aria-label', actionsToggle.title);
  actionsToggle.setAttribute('aria-expanded', String(actionsOpen));
  actionsHeader.classList.toggle('is-collapsed', !actionsOpen);
});

// ── Quick action buttons ──
document.querySelectorAll('.da-action-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    if (isWaiting) { return; }
    runModule(btn.dataset.module);
  });
});

const actionLabels = {
  generate:  'Generate Dockerfile',
  test:      'Generate Tests',
  flakiness: 'Detect & Repair Flakiness',
  all:       'Run Full Pipeline'
};

function runModule(module) {
  if (!module || isWaiting) { return; }

  const label = actionLabels[module] || module;
  appendMessage('user', label);
  const threshold = module === 'test'
    ? Math.max(0, Math.min(20, parseInt(thresholdInput?.value, 10) || 0))
    : 0;
  vscode.postMessage({ type: 'run-module', module, threshold });
  startRun('Starting');
}

// ── Send a chat message ──
function sendMessage() {
  const text = chatInput.value.trim();
  if (!text || isWaiting) { return; }

  appendMessage('user', text);
  vscode.postMessage({ type: 'user-message', text });
  chatInput.value = '';
  chatInput.style.height = 'auto';
  startRun('Thinking');
}

// ── Live status strip ──
// The elapsed timer runs client-side, so the strip keeps moving even when the
// backend is silent for minutes (long docker builds, sequential LLM calls).
function formatElapsed(ms) {
  const total = Math.floor(ms / 1000);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

function tickTimer() {
  if (!runStartedAt) { return; }
  statusTime.textContent = formatElapsed(Date.now() - runStartedAt);
}

function setStatus(label, meta) {
  statusLabel.textContent = label || 'Working';
  statusMeta.textContent = meta || '';
  statusMeta.hidden = !meta;
}

// `startedAt` is an absolute timestamp from the extension host, so a run that
// is already in flight resumes with the correct elapsed time after the panel
// is closed and reopened.
function startRun(label, startedAt, meta, stopping = false) {
  isWaiting = true;
  stopBtn.disabled = stopping;
  runStartedAt = startedAt || Date.now();
  setStatus(label, meta || '');
  statusStrip.hidden = false;
  tickTimer();
  if (timerId) { clearInterval(timerId); }
  timerId = setInterval(tickTimer, 1000);
  updateControls();
}

function endRun() {
  isWaiting = false;
  stopBtn.disabled = false;
  runStartedAt = 0;
  if (timerId) { clearInterval(timerId); timerId = null; }
  statusStrip.hidden = true;
  updateControls();
}

// ── Smart auto-scroll (only when already near bottom) ──
function isNearBottom() {
  return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 80;
}

function scrollToBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// ── Clipboard helper with execCommand fallback ──
function copyToClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text);
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;opacity:0';
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  document.body.removeChild(ta);
  return Promise.resolve();
}

// ── Append a message bubble ──
// variant: 'user' | 'assistant' | 'error' | 'success'
function appendMessage(variant, text, detail) {
  const atBottom = isNearBottom();

  const wrapper = document.createElement('div');
  wrapper.className = `da-message da-message--${variant}`;

  if (variant !== 'user') {
    const avatar = document.createElement('div');
    avatar.className = 'da-message__avatar';
    avatar.textContent = 'DA';
    wrapper.appendChild(avatar);
  }

  const bubble = document.createElement('div');
  bubble.className = 'da-message__bubble';
  bubble.innerHTML = renderMarkdown(text);

  // Inject copy button into every code block
  bubble.querySelectorAll('pre').forEach(pre => {
    const block = document.createElement('div');
    block.className = 'da-code-block';
    pre.parentNode.insertBefore(block, pre);
    block.appendChild(pre);

    const copyBtn = document.createElement('button');
    copyBtn.className = 'da-code-copy';
    copyBtn.textContent = 'Copy';
    copyBtn.title = 'Copy code';
    copyBtn.addEventListener('click', () => {
      const code = (pre.querySelector('code') || pre).textContent || '';
      copyToClipboard(code).then(() => {
        copyBtn.textContent = '✓ Copied';
        setTimeout(() => { copyBtn.textContent = 'Copy'; }, 2000);
      }).catch(() => {
        copyBtn.textContent = 'Failed';
        setTimeout(() => { copyBtn.textContent = 'Copy'; }, 2000);
      });
    });
    block.appendChild(copyBtn);
  });

  // Raw output (build logs, stack traces) stays folded away until asked for.
  if (detail) {
    const toggle = document.createElement('button');
    toggle.className = 'da-detail__toggle';
    toggle.textContent = 'Show details';
    toggle.setAttribute('aria-expanded', 'false');

    const body = document.createElement('pre');
    body.className = 'da-detail__body';
    body.textContent = detail;
    body.hidden = true;

    toggle.addEventListener('click', () => {
      body.hidden = !body.hidden;
      toggle.textContent = body.hidden ? 'Show details' : 'Hide details';
      toggle.setAttribute('aria-expanded', String(!body.hidden));
      if (!body.hidden && isNearBottom()) { scrollToBottom(); }
    });

    bubble.appendChild(toggle);
    bubble.appendChild(body);
  }

  wrapper.appendChild(bubble);
  if (variant !== 'user') {
    const meta = document.createElement('div');
    meta.className = 'da-message__meta';
    meta.textContent = variant === 'assistant' ? 'DockAgent' : variant;
    bubble.appendChild(meta);
  }
  messagesEl.appendChild(wrapper);

  if (atBottom) { scrollToBottom(); }
}

// ── Markdown renderer ──
// Supports: headings, **bold**, *italic*, `code`, ```blocks```, bullet lists, ordered lists
function renderMarkdown(text) {
  // Extract fenced code blocks before escaping so their content is preserved
  const blocks = [];
  let html = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const escaped = code.trim()
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
    const cls = lang ? ` class="lang-${lang}"` : '';
    blocks.push(`<pre><code${cls}>${escaped}</code></pre>`);
    return `\x00BLOCK${blocks.length - 1}\x00`;
  });

  // Escape remaining HTML
  html = html
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  // Inline code
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  // Headings
  html = html.replace(/^### (.+)$/gm, '<h4>$1</h4>');
  html = html.replace(/^## (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^# (.+)$/gm, '<h2>$1</h2>');
  // Bold and italic
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
  // Ordered list items (tag uniquely to avoid collision with ul)
  html = html.replace(/^\d+\. (.+)$/gm, '<li data-ol>$1</li>');
  // Unordered list items
  html = html.replace(/^[-*] (.+)$/gm, '<li>$1</li>');
  // Wrap consecutive ordered li runs
  html = html.replace(/(<li data-ol>[\s\S]*?<\/li>\n?)+/g, m =>
    '<ol>' + m.replace(/ data-ol/g, '') + '</ol>'
  );
  // Wrap consecutive unordered li runs (excluding already-wrapped ol items)
  html = html.replace(/(<li>(?!.*data-ol)[\s\S]*?<\/li>\n?)+/g, m =>
    '<ul>' + m + '</ul>'
  );
  // Paragraphs
  html = html.replace(/\n\n+/g, '</p><p>');
  html = html.replace(/\n/g, '<br>');

  // Restore code blocks
  html = html.replace(/\x00BLOCK(\d+)\x00/g, (_, i) => blocks[parseInt(i, 10)]);

  // Wrap in <p> if not already a block element
  if (!/^(<ul>|<ol>|<pre>|<h[1-6]>|<p>)/.test(html)) {
    html = '<p>' + html + '</p>';
  }

  return html;
}

// ── Messages from the extension host ──
window.addEventListener('message', event => {
  const msg = event.data;

  switch (msg.type) {

    // The host sends run-end after the reply is fully handled.
    case 'assistant-message':
      appendMessage('assistant', msg.text);
      break;

    // A pipeline milestone — the run continues, so the strip stays up.
    case 'pipeline-step':
      appendMessage('assistant', msg.text, msg.detail);
      break;

    case 'status-update':
      // A status line implies a run is active — if the webview was rebuilt
      // mid-run, this re-arms the strip rather than being dropped.
      if (!isWaiting) {
        startRun(msg.label, msg.startedAt, msg.meta, msg.stopping);
      }
      stopBtn.disabled = Boolean(msg.stopping);
      setStatus(msg.label, msg.meta);
      break;

    // Authoritative run lifecycle from the extension host.
    case 'run-begin':
      startRun(msg.label, msg.startedAt, msg.meta, msg.stopping);
      break;

    case 'run-end':
      endRun();
      break;

    case 'pipeline-error':
      appendMessage('error', `⚠ ${msg.text}`, msg.detail);
      break;

    case 'pipeline-success':
      appendMessage('success', `✓ ${msg.text}`, msg.detail);
      break;

    case 'run-all':
      runModule('all');
      break;

    case 'run-module':
      runModule(msg.module);
      break;

    case 'restore-history':
      if (Array.isArray(msg.history)) {
        for (const entry of msg.history) {
          const text = entry.variant === 'error'   ? `⚠ ${entry.text}`
                     : entry.variant === 'success' ? `✓ ${entry.text}`
                     : entry.text;
          // 'step' entries render like assistant messages; their raw detail is
          // intentionally not persisted, so nothing is expandable after reload.
          const variant = entry.variant === 'step' ? 'assistant' : entry.variant;
          appendMessage(variant, text);
        }
      }
      break;
  }
});

// ── Init — restore within-session state ──
updateControls();
const _saved = vscode.getState();
if (_saved?.threshold !== undefined && thresholdInput) {
  const next = Math.max(0, Math.min(20, parseInt(_saved.threshold, 10) || 0));
  thresholdInput.value = String(next);
}
