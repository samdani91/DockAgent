"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || function (mod) {
    if (mod && mod.__esModule) return mod;
    var result = {};
    if (mod != null) for (var k in mod) if (k !== "default" && Object.prototype.hasOwnProperty.call(mod, k)) __createBinding(result, mod, k);
    __setModuleDefault(result, mod);
    return result;
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.DockAgentPanel = void 0;
const vscode = __importStar(require("vscode"));
class DockAgentPanel {
    get _runActive() {
        return this._runStartedAt > 0;
    }
    constructor(_context) {
        this._context = _context;
        this._backendBaseUrl = 'http://127.0.0.1:8000';
        this._chatHistory = [];
        this._maxHistory = 200;
        // Authoritative run state. The webview's copy is destroyed whenever the view
        // is hidden, but the SSE loop keeps running here — so the host owns this and
        // replays it when the view comes back.
        this._runStartedAt = 0;
        this._runLabel = 'Working';
        this._runMeta = '';
        DockAgentPanel.currentPanel = this;
        // Restore history saved in previous session
        this._chatHistory = _context.workspaceState.get('dockagent.chatHistory', []);
    }
    resolveWebviewView(webviewView, _context, _token) {
        this._view = webviewView;
        // Note: retainContextWhenHidden is set on the provider registration in
        // extension.ts — it is not part of WebviewOptions.
        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [
                vscode.Uri.joinPath(this._context.extensionUri, 'src', 'webview')
            ]
        };
        webviewView.webview.html = this._getHtmlContent(webviewView.webview);
        webviewView.webview.onDidReceiveMessage(msg => this._handleMessage(msg), undefined, this._context.subscriptions);
        // Replay state once the webview has initialised. This runs on every
        // re-resolve, which is what makes a reopened panel pick up a run that is
        // still in flight rather than showing a blank UI.
        setTimeout(() => this._restoreState(), 250);
    }
    /** Push history and any in-flight run into a freshly resolved webview. */
    _restoreState() {
        if (this._chatHistory.length > 0) {
            this._view?.webview.postMessage({
                type: 'restore-history',
                history: this._chatHistory
            });
        }
        if (this._runActive) {
            this._view?.webview.postMessage({
                type: 'run-begin',
                label: this._runLabel,
                meta: this._runMeta,
                startedAt: this._runStartedAt
            });
        }
    }
    /** Mark a run as started; `startedAt` is absolute so elapsed time survives a reopen. */
    _beginRun(label) {
        this._runStartedAt = Date.now();
        this._runLabel = label;
        this._runMeta = '';
        this._view?.webview.postMessage({
            type: 'run-begin',
            label,
            startedAt: this._runStartedAt
        });
    }
    _endRun() {
        this._runStartedAt = 0;
        this._runLabel = 'Working';
        this._runMeta = '';
        this._view?.webview.postMessage({ type: 'run-end' });
    }
    postMessage(message) {
        this._view?.webview.postMessage(message);
    }
    // Post a message AND persist it to history
    _postPersistent(type, variant, text, detail) {
        this._addToHistory({ variant, text }); // detail is deliberately not persisted
        this._view?.webview.postMessage({ type, text, detail });
    }
    _addToHistory(entry) {
        this._chatHistory.push(entry);
        if (this._chatHistory.length > this._maxHistory) {
            this._chatHistory.shift();
        }
        this._context.workspaceState.update('dockagent.chatHistory', this._chatHistory);
    }
    async _handleMessage(message) {
        switch (message.type) {
            case 'user-message':
                this._addToHistory({ variant: 'user', text: String(message.text ?? '') });
                this._beginRun('Thinking');
                try {
                    await this._handleChatMessage(String(message.text ?? ''));
                }
                finally {
                    this._endRun();
                }
                break;
            case 'run-module': {
                const labels = {
                    generate: 'Generate Dockerfile',
                    test: 'Generate Tests',
                    flakiness: 'Detect & Repair Flakiness',
                    all: 'Run Full Pipeline'
                };
                const label = labels[message.module] ?? String(message.module);
                this._addToHistory({ variant: 'user', text: label });
                this._beginRun('Starting');
                try {
                    await this._handlePipelineModule(String(message.module ?? ''), typeof message.threshold === 'number' ? message.threshold : 0.0);
                }
                finally {
                    this._endRun();
                }
                break;
            }
            case 'open-file':
                vscode.workspace.openTextDocument(message.path)
                    .then(doc => vscode.window.showTextDocument(doc));
                break;
            case 'clear-history':
                this._chatHistory = [];
                this._context.workspaceState.update('dockagent.chatHistory', []);
                break;
        }
    }
    async _handleChatMessage(text) {
        if (!text) {
            return;
        }
        this._postStatus('Thinking…');
        try {
            const response = await fetch(`${this._backendBaseUrl}/chat`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: text })
            });
            if (!response.ok) {
                throw new Error(`Backend request failed with status ${response.status}`);
            }
            const data = (await response.json());
            this._postPersistent('assistant-message', 'assistant', data.reply ?? 'No reply from backend.');
        }
        catch (error) {
            this._postPersistent('pipeline-error', 'error', this._formatError(error, 'Unable to reach the backend chat endpoint.'));
        }
    }
    async _handlePipelineModule(module, threshold) {
        if (!module) {
            return;
        }
        this._postStatus('Starting…');
        if (module === 'generate') {
            await this._handleDockerfileGeneration();
            return;
        }
        if (module === 'test') {
            await this._handleTestGeneration(threshold);
            return;
        }
        if (module === 'all') {
            this._postStep('**Step 1 of 2** — Dockerfile');
            const generated = await this._handleDockerfileGeneration();
            if (generated) {
                this._postStep('**Step 2 of 2** — Container Structure Tests');
                await this._handleTestGeneration(threshold);
            }
            return;
        }
        try {
            const response = await fetch(`${this._backendBaseUrl}/pipeline/${encodeURIComponent(module)}`, {
                method: 'POST'
            });
            if (!response.ok) {
                throw new Error(`Backend request failed with status ${response.status}`);
            }
            const data = (await response.json());
            this._postPersistent('pipeline-success', 'success', data.detail ?? `${module} pipeline completed.`);
        }
        catch (error) {
            this._postPersistent('pipeline-error', 'error', this._formatError(error, `Unable to reach the backend pipeline endpoint for ${module}.`));
        }
    }
    async _handleDockerfileGeneration() {
        const workspaceFolders = vscode.workspace.workspaceFolders;
        if (!workspaceFolders || workspaceFolders.length === 0) {
            this._postPersistent('pipeline-error', 'error', 'No workspace folder is open. Open a project folder first.');
            return false;
        }
        const workspacePath = workspaceFolders[0].uri.fsPath;
        try {
            const response = await fetch(`${this._backendBaseUrl}/pipeline/generate`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ workspace_path: workspacePath })
            });
            if (!response.ok) {
                throw new Error(`Backend returned ${response.status}`);
            }
            if (!response.body) {
                throw new Error('No response body from backend.');
            }
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            let succeeded = false;
            while (true) {
                const { value, done } = await reader.read();
                if (done) {
                    break;
                }
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() ?? '';
                for (const line of lines) {
                    if (!line.startsWith('data: ')) {
                        continue;
                    }
                    let event;
                    try {
                        event = JSON.parse(line.slice(6));
                    }
                    catch {
                        continue;
                    }
                    if (event.step === 'done') {
                        succeeded = true;
                        this._postPersistent('pipeline-success', 'success', event.message);
                        if (event.output_path) {
                            const doc = await vscode.workspace.openTextDocument(event.output_path);
                            vscode.window.showTextDocument(doc);
                        }
                    }
                    else if (event.step === 'error') {
                        this._postPersistent('pipeline-error', 'error', event.message);
                    }
                    else {
                        this._handleProgressEvent(event.step, event.message);
                    }
                }
            }
            return succeeded;
        }
        catch (error) {
            this._postPersistent('pipeline-error', 'error', this._formatError(error, 'Dockerfile generation failed. Is the backend running?'));
            return false;
        }
    }
    async _handleTestGeneration(threshold) {
        const workspaceFolders = vscode.workspace.workspaceFolders;
        if (!workspaceFolders || workspaceFolders.length === 0) {
            this._postPersistent('pipeline-error', 'error', 'No workspace folder is open. Please open a folder that contains a Dockerfile.');
            return;
        }
        const workspacePath = workspaceFolders[0].uri.fsPath;
        const dockerfileUri = vscode.Uri.joinPath(workspaceFolders[0].uri, 'Dockerfile');
        try {
            await vscode.workspace.fs.stat(dockerfileUri);
        }
        catch {
            this._postPersistent('pipeline-error', 'error', 'No Dockerfile found in the workspace root.');
            return;
        }
        const dockerfilePath = dockerfileUri.fsPath;
        try {
            const response = await fetch(`${this._backendBaseUrl}/pipeline/test`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    dockerfile_path: dockerfilePath,
                    workspace_path: workspacePath,
                    threshold,
                    execute: true
                })
            });
            if (!response.ok) {
                throw new Error(`Backend request failed with status ${response.status}`);
            }
            if (!response.body) {
                throw new Error('No response body from backend.');
            }
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            while (true) {
                const { value, done } = await reader.read();
                if (done) {
                    break;
                }
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() ?? '';
                for (const line of lines) {
                    if (!line.startsWith('data: ')) {
                        continue;
                    }
                    let event;
                    try {
                        event = JSON.parse(line.slice(6));
                    }
                    catch {
                        continue;
                    }
                    if (event.step === 'done') {
                        const outputPath = event.output_path ?? '';
                        if (event.results) {
                            const { total, failed, passed, cases } = event.results;
                            const ok = failed === 0;
                            const summary = ok
                                ? `${passed} of ${total} container structure tests passed`
                                : `${failed} of ${total} container structure tests failed`;
                            this._postPersistent(ok ? 'pipeline-success' : 'pipeline-error', ok ? 'success' : 'error', summary, this._failureDetail(cases));
                        }
                        else {
                            this._postPersistent('pipeline-success', 'success', event.message);
                        }
                        // Execution problems are non-fatal — the spec is still usable.
                        if (event.warning) {
                            this._postStep(event.warning);
                        }
                        if (outputPath) {
                            const doc = await vscode.workspace.openTextDocument(outputPath);
                            vscode.window.showTextDocument(doc);
                        }
                    }
                    else if (event.step === 'error') {
                        this._postPersistent('pipeline-error', 'error', event.message);
                    }
                    else {
                        this._handleProgressEvent(event.step, event.message);
                    }
                }
            }
        }
        catch (error) {
            this._postPersistent('pipeline-error', 'error', this._formatError(error, 'Test generation failed. Is the backend running?'));
        }
    }
    _postStatus(label, meta) {
        this._runLabel = label;
        this._runMeta = meta ?? '';
        // startedAt lets a rebuilt webview re-arm the strip with the correct elapsed time.
        this._view?.webview.postMessage({
            type: 'status-update',
            label,
            meta,
            startedAt: this._runStartedAt || Date.now()
        });
    }
    /** A pipeline milestone: kept in the transcript, does not end the run. */
    _postStep(text, detail) {
        this._addToHistory({ variant: 'step', text }); // detail is deliberately not persisted
        this._view?.webview.postMessage({ type: 'pipeline-step', text, detail });
    }
    /**
     * Turn a raw `{step, message}` event into something worth showing.
     * Anything not matched falls back to the raw message as a status line only.
     */
    _progressView(step, message) {
        // ── Dockerfile generation ────────────────────────────────────────────
        const attempt = message.match(/Build attempt (\d+)\/(\d+)/i);
        if (attempt) {
            return { label: 'Building image', meta: `attempt ${attempt[1]} of ${attempt[2]}` };
        }
        const succeeded = message.match(/Build succeeded on attempt (\d+)/i);
        if (succeeded) {
            return {
                label: 'Build succeeded',
                meta: `on attempt ${succeeded[1]}`,
                milestone: true
            };
        }
        const located = message.match(/Error located:\s*([\s\S]+)/i);
        if (located) {
            return {
                label: 'Build failed',
                meta: this._summarizeError(located[1]),
                detail: located[1].trim(),
                milestone: true
            };
        }
        const repairing = message.match(/Generating repair candidates \((.+?)\)/i);
        if (repairing) {
            return { label: 'Writing a fix', meta: repairing[1] };
        }
        if (/No progress/i.test(message)) {
            return {
                label: 'Stopped — the same error kept recurring',
                milestone: true
            };
        }
        // Phase B already emits short, human-readable lines — pass them through
        // rather than flattening every sub-step to one label.
        if (step === 'optimizing') {
            const clean = message
                .replace(/^Phase B\s*[—–-]\s*/i, '')
                .replace(/[….]+$/, '')
                .trim();
            const cased = clean.charAt(0).toUpperCase() + clean.slice(1);
            return { label: this._truncate(cased, 52) };
        }
        // ── Fixed steps, keyed off the SSE step name ─────────────────────────
        const byStep = {
            checking: 'Checking workspace',
            context: 'Reading project files',
            generating: 'Writing the Dockerfile',
            S0: 'Building image',
            S1: 'Reading image layers',
            S2: 'Scoring files',
            S3: 'Choosing test types',
            S4: 'Collecting versions',
            S5: 'Running tests'
        };
        if (byStep[step]) {
            // Test-generation steps carry useful counts after the dash
            // ("S1 — Found 12 layers, 3400 files, …") — keep those as meta.
            const rest = message.replace(/^S[0-5]\s*[—–-]\s*/, '').trim();
            const isStats = /\d/.test(rest) && rest.includes(',');
            return {
                label: byStep[step],
                meta: isStats ? this._truncate(rest, 46) : undefined
            };
        }
        // Unknown event — show it, but keep it short and out of the transcript.
        return { label: this._truncate(message, 70), detail: message };
    }
    /** Compress a raw build error into a few words. */
    _summarizeError(raw) {
        const text = raw.trim();
        const failedCmd = text.match(/process "\/bin\/sh -c (.+?)" did not complete/i);
        if (failedCmd) {
            return `\`${this._truncate(failedCmd[1], 42)}\` failed`;
        }
        const aptMissing = text.match(/Unable to locate package (\S+)/i);
        if (aptMissing) {
            return `package not found: ${aptMissing[1]}`;
        }
        const pipMissing = text.match(/No matching distribution found for (\S+)/i);
        if (pipMissing) {
            return `package not found: ${pipMissing[1]}`;
        }
        const missingFile = text.match(/no such file or directory[:,]?\s*'?([^'\s]+)/i);
        if (missingFile) {
            return `missing file: ${missingFile[1]}`;
        }
        const notFound = text.match(/([\w.\-/]+): not found/i);
        if (notFound) {
            return `not found: ${notFound[1]}`;
        }
        // Fall back to the first meaningful line, minus the usual noise prefixes.
        const firstLine = text.split('\n')[0]
            .replace(/^(ERROR|error|E):\s*/, '')
            .replace(/^failed to solve:\s*/i, '')
            .trim();
        return this._truncate(firstLine, 60);
    }
    /** Render failing cases for the collapsible detail panel. */
    _failureDetail(cases) {
        const failing = (cases || []).filter(c => !c.passed);
        if (failing.length === 0) {
            return undefined;
        }
        return failing
            .map(c => {
            const errors = (c.errors || []).map(e => `    ${e}`).join('\n');
            return errors ? `✗ ${c.name}\n${errors}` : `✗ ${c.name}`;
        })
            .join('\n\n');
    }
    _truncate(text, max) {
        const clean = text.replace(/\s+/g, ' ').trim();
        return clean.length > max ? clean.slice(0, max - 1) + '…' : clean;
    }
    /** Route one pipeline event to the status strip and, if notable, the transcript. */
    _handleProgressEvent(step, message) {
        const view = this._progressView(step, message);
        this._postStatus(view.label, view.meta);
        if (view.milestone) {
            const text = view.meta ? `${view.label} — ${view.meta}` : view.label;
            this._postStep(text, view.detail);
        }
    }
    _formatError(error, fallbackMessage) {
        if (error instanceof Error && error.message) {
            return error.message;
        }
        return fallbackMessage;
    }
    _getHtmlContent(webview) {
        const scriptUri = webview.asWebviewUri(vscode.Uri.joinPath(this._context.extensionUri, 'src', 'webview', 'main.js'));
        const styleUri = webview.asWebviewUri(vscode.Uri.joinPath(this._context.extensionUri, 'src', 'webview', 'style.css'));
        const nonce = getNonce();
        return /* html */ `
      <!DOCTYPE html>
      <html lang="en">
      <head>
        <meta charset="UTF-8">
        <meta http-equiv="Content-Security-Policy"
          content="default-src 'none';
                   style-src ${webview.cspSource} 'unsafe-inline';
                   script-src 'nonce-${nonce}';">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <link rel="stylesheet" href="${styleUri}">
        <title>DockAgent</title>
      </head>
      <body>
        <div id="app">

          <!-- ── HEADER ── -->
          <div class="da-header">
            <div class="da-header__mark" aria-hidden="true">DA</div>
            <div class="da-header__copy">
              <div class="da-header__title">DockAgent</div>
              <div class="da-header__subtitle">Dockerfile generation and build testing</div>
            </div>
            <button class="da-icon-btn" id="clearBtn" title="Clear conversation" aria-label="Clear conversation">
              <span aria-hidden="true">×</span>
            </button>
          </div>

          <!-- ── QUICK ACTIONS ── -->
          <div class="da-actions-header" id="actionsHeader">
            <span class="da-actions-label">Actions</span>
            <button class="da-icon-btn da-actions-toggle" id="actionsToggle" title="Collapse actions" aria-label="Collapse actions" aria-expanded="true">
              <span aria-hidden="true">⌃</span>
            </button>
          </div>
          <div class="da-actions" id="actionsBody">
            <button class="da-action-btn" data-module="generate">
              <span class="da-action-btn__icon" aria-hidden="true">DF</span>
              <span class="da-action-btn__content">
                <span class="da-action-btn__label">Generate Dockerfile</span>
                <span class="da-action-btn__meta">Infer runtime, ports, and commands</span>
              </span>
            </button>
            <div class="da-test-group">
              <button class="da-action-btn" data-module="test">
                <span class="da-action-btn__icon" aria-hidden="true">CT</span>
                <span class="da-action-btn__content">
                  <span class="da-action-btn__label">Generate Tests</span>
                  <span class="da-action-btn__meta">Create Container Structure Tests</span>
                </span>
              </button>

              <!-- Threshold setting — only relevant for test generation -->
              <div class="da-setting-row">
                <div>
                  <label class="da-setting-label" for="thresholdInput">Test score threshold</label>
                  <div class="da-setting-hint">Only used by Generate Tests. Use 0 to include every generated file.</div>
                </div>
                <input class="da-setting-input" type="number" inputmode="numeric"
                       id="thresholdInput" min="0" max="20" step="1" value="0">
              </div>
            </div>

            <button class="da-action-btn" data-module="flakiness">
              <span class="da-action-btn__icon" aria-hidden="true">FX</span>
              <span class="da-action-btn__content">
                <span class="da-action-btn__label">Repair Flakiness</span>
                <span class="da-action-btn__meta">Find unstable build behavior</span>
              </span>
            </button>
            <button class="da-action-btn da-action-btn--primary" data-module="all">
              <span class="da-action-btn__icon" aria-hidden="true">▶</span>
              <span class="da-action-btn__content">
                <span class="da-action-btn__label">Run Full Pipeline</span>
                <span class="da-action-btn__meta">Generate, test, and repair</span>
              </span>
            </button>
          </div>

          <!-- ── CHAT MESSAGES ── -->
          <div class="da-messages" id="messages">
            <div class="da-message da-message--assistant">
              <div class="da-message__avatar">DA</div>
              <div class="da-message__bubble">
                <p><strong>Ready for your container workflow.</strong></p>
                <ul>
                  <li>Generate a Dockerfile for your repository</li>
                  <li>Create Container Structure Tests</li>
                  <li>Detect and repair flaky builds</li>
                </ul>
                <p>Choose an action or ask a question below.</p>
              </div>
            </div>
          </div>

          <!-- ── LIVE STATUS STRIP ── -->
          <!-- Sits directly above the composer, visible only while a run is in
               flight. The elapsed timer ticks client-side so it proves liveness
               even during long silences. -->
          <div class="da-status" id="statusStrip" hidden aria-live="polite">
            <span class="da-status__spinner" aria-hidden="true"></span>
            <span class="da-status__body">
              <span class="da-status__label" id="statusLabel">Working</span>
              <span class="da-status__meta" id="statusMeta"></span>
            </span>
            <span class="da-status__time" id="statusTime">0:00</span>
          </div>

          <!-- ── CHAT INPUT ── -->
          <div class="da-input-area">
            <div class="da-input-row">
              <textarea
                id="chatInput"
                class="da-input"
                placeholder="Ask anything about your Dockerfile…"
                rows="1"
              ></textarea>
              <button class="da-send-btn" id="sendBtn" title="Send (Enter)">
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                  <path d="M1 8l13-7-4 7 4 7-13-7z" fill="currentColor"/>
                </svg>
              </button>
            </div>
            <div class="da-input-hint">Enter to send / Shift+Enter for new line</div>
          </div>

        </div>

        <script nonce="${nonce}" src="${scriptUri}"><\/script>
      </body>
      </html>
    `;
    }
}
exports.DockAgentPanel = DockAgentPanel;
function getNonce() {
    let text = '';
    const possible = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    for (let i = 0; i < 32; i++) {
        text += possible.charAt(Math.floor(Math.random() * possible.length));
    }
    return text;
}
//# sourceMappingURL=DockAgentPanel.js.map