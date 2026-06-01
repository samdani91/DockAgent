const vscode = acquireVsCodeApi();

const messagesEl    = document.getElementById('messages');
const chatInput     = document.getElementById('chatInput');
const sendBtn       = document.getElementById('sendBtn');
const clearBtn      = document.getElementById('clearBtn');
const actionsHeader = document.getElementById('actionsHeader');
const actionsBody   = document.getElementById('actionsBody');
const actionsToggle = document.getElementById('actionsToggle');
const thresholdInput = document.getElementById('thresholdInput');

let isWaiting  = false;
let typingEl   = null;
let actionsOpen = true;

// ── Interactive control state ──
function updateControls() {
  sendBtn.disabled = isWaiting || chatInput.value.trim().length === 0;
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
  typingEl = null;
  isWaiting = false;
  updateControls();
  vscode.setState({ history: [], threshold: thresholdInput?.value ?? '0' });
  vscode.postMessage({ type: 'clear-history' });
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
  showTyping();
}

// ── Send a chat message ──
function sendMessage() {
  const text = chatInput.value.trim();
  if (!text || isWaiting) { return; }

  appendMessage('user', text);
  vscode.postMessage({ type: 'user-message', text });
  chatInput.value = '';
  chatInput.style.height = 'auto';
  showTyping();
  updateControls();
}

// ── Typing indicator (animated dots in chat) ──
function showTyping() {
  isWaiting = true;
  updateControls();
  if (typingEl) { return; }

  typingEl = document.createElement('div');
  typingEl.className = 'da-message da-message--assistant';

  const avatar = document.createElement('div');
  avatar.className = 'da-message__avatar';
  avatar.textContent = 'DA';

  const bubble = document.createElement('div');
  bubble.className = 'da-message__bubble';
  bubble.innerHTML =
    '<div class="da-typing">' +
    '<div class="da-typing__dot"></div>' +
    '<div class="da-typing__dot"></div>' +
    '<div class="da-typing__dot"></div>' +
    '</div>';

  typingEl.appendChild(avatar);
  typingEl.appendChild(bubble);
  messagesEl.appendChild(typingEl);
  scrollToBottom();
}

function hideTyping() {
  isWaiting = false;
  updateControls();
  if (typingEl) {
    typingEl.remove();
    typingEl = null;
  }
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
function appendMessage(variant, text) {
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

    case 'assistant-message':
      hideTyping();
      appendMessage('assistant', msg.text);
      break;

    case 'status-update':
      break;

    case 'pipeline-error':
      hideTyping();
      appendMessage('error', `⚠ ${msg.text}`);
      break;

    case 'pipeline-success':
      hideTyping();
      appendMessage('success', `✓ ${msg.text}`);
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
          appendMessage(entry.variant, text);
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
