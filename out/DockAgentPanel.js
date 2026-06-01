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
    constructor(_context) {
        this._context = _context;
        this._backendBaseUrl = 'http://127.0.0.1:8000';
        this._chatHistory = [];
        this._maxHistory = 200;
        DockAgentPanel.currentPanel = this;
        // Restore history saved in previous session
        this._chatHistory = _context.workspaceState.get('dockagent.chatHistory', []);
    }
    resolveWebviewView(webviewView, _context, _token) {
        this._view = webviewView;
        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [
                vscode.Uri.joinPath(this._context.extensionUri, 'src', 'webview')
            ]
        };
        webviewView.webview.html = this._getHtmlContent(webviewView.webview);
        webviewView.webview.onDidReceiveMessage(msg => this._handleMessage(msg), undefined, this._context.subscriptions);
        // Replay stored history after the webview has initialised
        if (this._chatHistory.length > 0) {
            setTimeout(() => {
                this._view?.webview.postMessage({
                    type: 'restore-history',
                    history: this._chatHistory
                });
            }, 250);
        }
    }
    postMessage(message) {
        this._view?.webview.postMessage(message);
    }
    // Post a message AND persist it to history
    _postPersistent(type, variant, text) {
        this._addToHistory({ variant, text });
        this._view?.webview.postMessage({ type, text });
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
                await this._handleChatMessage(String(message.text ?? ''));
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
                await this._handlePipelineModule(String(message.module ?? ''), typeof message.threshold === 'number' ? message.threshold : 0.0);
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
        this._postStatus(`Running ${module}…`);
        if (module === 'test') {
            await this._handleTestGeneration(threshold);
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
                    threshold
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
                        this._postPersistent('pipeline-success', 'success', event.message);
                        if (outputPath) {
                            const doc = await vscode.workspace.openTextDocument(outputPath);
                            vscode.window.showTextDocument(doc);
                        }
                    }
                    else if (event.step === 'error') {
                        this._postPersistent('pipeline-error', 'error', event.message);
                    }
                    else {
                        this._postStatus(event.message);
                        // Append step-completion summaries as chat messages
                        const isStepSummary = /^S[0-4] —.*(?:successfully|found|kept|identified|got version|built)/.test(event.message);
                        if (isStepSummary) {
                            this._postPersistent('assistant-message', 'assistant', event.message);
                        }
                    }
                }
            }
        }
        catch (error) {
            this._postPersistent('pipeline-error', 'error', this._formatError(error, 'Test generation failed. Is the backend running?'));
        }
    }
    _postStatus(text) {
        this._view?.webview.postMessage({ type: 'status-update', text });
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