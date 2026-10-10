import * as vscode from 'vscode';

interface ChatEntry {
  variant: 'user' | 'assistant' | 'error' | 'success' | 'step';
  text: string;
}

/**
 * A raw pipeline event rendered for humans.
 *
 * `label` is short enough for the live status strip; `detail` holds the raw
 * text (build logs, stack traces) and is only ever shown on demand.
 */
interface ProgressView {
  label: string;
  meta?: string;
  badge?: string;        // module chip, kept out of the label so it cannot truncate it
  detail?: string;
  milestone?: boolean;   // milestones are kept in the chat transcript
}

interface TestCaseResult {
  name: string;
  passed: boolean;
  errors: string[];
}

/** The `done` event from POST /pipeline/flakiness. */
interface FlakinessDoneEvent {
  step: string;
  message: string;
  verdict?: string;
  detection?: {
    verdict: string;
    iterations: number;
    successes: number;
    failures: number;
    is_flaky: boolean;
    failing_instruction: string;
  };
  repair?: {
    success: boolean;
    attempts: number;
    message: string;
    demonstrations?: string[];
    repaired_path?: string;
    applied?: boolean;
  } | null;
}

/** The `done` event from POST /pipeline/test. */
interface TestDoneEvent {
  step: string;
  message: string;
  output_path?: string;
  results_path?: string | null;    // JSON record of this run, kept as evidence
  warning?: string;                // set when the spec was written but not run
  results?: {
    total: number;
    passed: number;
    failed: number;
    cases: TestCaseResult[];
    raw_output: string;
  } | null;
}

/** Short module tags prefixed onto status labels during a coordinated run. */
/** Raw error output beyond this goes untransmitted; the detail panel scrolls,
 *  but a pathological log should not bloat every webview message. */
const MAX_ERROR_DETAIL = 12000;

const STAGE_LABELS: Record<string, string> = {
  generate: 'Module 1',
  test: 'Module 2',
  flakiness: 'Module 3'
};

//: What the status strip shows instead of the full module name. Prefixing the
//: label with "Module 1 · " cost it a third of a narrow sidebar's width, which
//: is what pushed both the label and its meta into ellipses.
const STAGE_BADGES: Record<string, string> = {
  generate: 'M1',
  test: 'M2',
  flakiness: 'M3'
};

interface RunState {
  stage: string;
  feedback_rounds: number;
  reverified: boolean;
  generation?: { success: boolean; attempts: number; message: string } | null;
  test?: {
    executed: boolean;
    total: number;
    passed: number;
    failed: number;
    failing: { name: string; errors: string[] }[];
  } | null;
  flakiness?: {
    verdict: string;
    repaired: boolean;
    applied: boolean;
    repaired_path?: string | null;
  } | null;
  history: { stage: string; outcome: string; message: string }[];
}

/** Events from POST /pipeline/run — every one carries the stage that emitted it. */
interface RunEvent {
  stage: string;                 // generate | test | flakiness | agent
  step: string;
  message: string;
  output_path?: string;
  state?: RunState;
}

/**
 * Serves the pre-repair Dockerfile from memory.
 *
 * A repair now overwrites the Dockerfile in place, so there is no second file
 * on disk to diff against. The original text is held here instead, which keeps
 * the review step without leaving a `.repaired` artefact in the project.
 */
const ORIGINAL_SCHEME = 'dockagent-original';

class OriginalDockerfileProvider implements vscode.TextDocumentContentProvider {
  private readonly _onDidChange = new vscode.EventEmitter<vscode.Uri>();
  readonly onDidChange = this._onDidChange.event;
  private _snapshots = new Map<string, string>();

  provideTextDocumentContent(uri: vscode.Uri): string {
    return this._snapshots.get(uri.path) ?? '';
  }

  /** Store *text* and return the URI that serves it. */
  snapshot(label: string, text: string): vscode.Uri {
    this._snapshots.set(label, text);
    const uri = vscode.Uri.from({ scheme: ORIGINAL_SCHEME, path: label });
    this._onDidChange.fire(uri);
    return uri;
  }
}

export class DockAgentPanel implements vscode.WebviewViewProvider {

  public static currentPanel: DockAgentPanel | undefined;
  private _view?: vscode.WebviewView;
  private readonly _backendBaseUrl = 'http://127.0.0.1:8000';
  private _chatHistory: ChatEntry[] = [];
  private readonly _maxHistory = 200;

  // Authoritative run state. The webview's copy is destroyed whenever the view
  // is hidden, but the SSE loop keeps running here — so the host owns this and
  // replays it when the view comes back.
  private _runStartedAt = 0;
  private _runLabel = 'Working';
  private _runMeta = '';
  private _runBadge = '';
  private _activeController?: AbortController;

  private get _runActive(): boolean {
    return this._runStartedAt > 0;
  }

  private readonly _originals = new OriginalDockerfileProvider();

  constructor(private readonly _context: vscode.ExtensionContext) {
    DockAgentPanel.currentPanel = this;
    // Restore history saved in previous session
    this._chatHistory = _context.workspaceState.get<ChatEntry[]>('dockagent.chatHistory', []);
    _context.subscriptions.push(
      vscode.workspace.registerTextDocumentContentProvider(
        ORIGINAL_SCHEME, this._originals
      )
    );
  }

  public resolveWebviewView(
    webviewView: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken
  ) {
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

    webviewView.webview.onDidReceiveMessage(
      msg => this._handleMessage(msg),
      undefined,
      this._context.subscriptions
    );

    // Replay state once the webview has initialised. This runs on every
    // re-resolve, which is what makes a reopened panel pick up a run that is
    // still in flight rather than showing a blank UI.
    setTimeout(() => this._restoreState(), 250);
  }

  /** Push history and any in-flight run into a freshly resolved webview. */
  private _restoreState() {
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
        badge: this._runBadge,
        startedAt: this._runStartedAt,
        stopping: this._activeController?.signal.aborted ?? false
      });
    }
  }

  /** Mark a run as started; `startedAt` is absolute so elapsed time survives a reopen. */
  private _beginRun(label: string) {
    this._activeController = new AbortController();
    this._runStartedAt = Date.now();
    this._runLabel = label;
    this._runMeta = '';
    this._runBadge = '';
    this._view?.webview.postMessage({
      type: 'run-begin',
      label,
      startedAt: this._runStartedAt
    });
  }

  private _endRun() {
    this._activeController = undefined;
    this._runStartedAt = 0;
    this._runLabel = 'Working';
    this._runMeta = '';
    this._runBadge = '';
    this._view?.webview.postMessage({ type: 'run-end' });
  }

  public postMessage(message: object) {
    this._view?.webview.postMessage(message);
  }

  // Post a message AND persist it to history
  private _postPersistent(
    type: string,
    variant: ChatEntry['variant'],
    text: string,
    detail?: string
  ) {
    this._addToHistory({ variant, text });   // detail is deliberately not persisted
    this._view?.webview.postMessage({ type, text, detail });
  }

  private _addToHistory(entry: ChatEntry) {
    this._chatHistory.push(entry);
    if (this._chatHistory.length > this._maxHistory) {
      this._chatHistory.shift();
    }
    this._context.workspaceState.update('dockagent.chatHistory', this._chatHistory);
  }

  private async _handleMessage(message: any) {
    switch (message.type) {

      case 'user-message':
        if (this._runActive) { break; }
        this._addToHistory({ variant: 'user', text: String(message.text ?? '') });
        this._beginRun('Thinking');
        try {
          await this._handleChatMessage(String(message.text ?? ''));
        } finally {
          if (this._activeController?.signal.aborted) {
            this._postStep('Run stopped.');
          }
          this._endRun();
        }
        break;

      case 'run-module': {
        if (this._runActive) { break; }
        const labels: Record<string, string> = {
          generate: 'Generate Dockerfile',
          test: 'Generate Tests',
          flakiness: 'Detect & Repair Flakiness',
          all: 'Run Full Pipeline'
        };
        const label = labels[message.module] ?? String(message.module);
        this._addToHistory({ variant: 'user', text: label });
        this._beginRun('Starting');
        try {
          await this._handlePipelineModule(
            String(message.module ?? ''),
            typeof message.threshold === 'number' ? message.threshold : 0.0
          );
        } finally {
          if (this._activeController?.signal.aborted) {
            this._postStep('Run stopped.');
          }
          this._endRun();
        }
        break;
      }

      case 'open-file':
        vscode.workspace.openTextDocument(message.path)
          .then(doc => vscode.window.showTextDocument(doc));
        break;

      case 'stop-run':
        if (this._runActive && this._activeController &&
            !this._activeController.signal.aborted) {
          this._activeController.abort();
          this._postStatus('Stopping…');
        }
        break;

      case 'clear-history':
        this._chatHistory = [];
        this._context.workspaceState.update('dockagent.chatHistory', []);
        break;
    }
  }

  private async _handleChatMessage(text: string) {
    if (!text) { return; }

    this._postStatus('Thinking…');

    try {
      const response = await fetch(`${this._backendBaseUrl}/chat`, {
        method: 'POST',
        signal: this._activeController?.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: text,
          workspace_path: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath
        })
      });

      if (!response.ok) {
        throw new Error(`Backend request failed with status ${response.status}`);
      }

      const data = (await response.json()) as { reply?: string };
      this._postPersistent('assistant-message', 'assistant', data.reply ?? 'No reply from backend.');
    } catch (error) {
      if (!this._activeController?.signal.aborted) {
        this._postPersistent('pipeline-error', 'error', this._formatError(error, 'Unable to reach the backend chat endpoint.'));
      }
    }
  }

  private async _handlePipelineModule(module: string, threshold: number) {
    if (!module) { return; }

    this._postStatus('Starting…');

    if (module === 'generate') {
      await this._handleDockerfileGeneration();
      return;
    }

    if (module === 'test') {
      await this._handleTestGeneration(threshold);
      return;
    }

    if (module === 'flakiness') {
      await this._handleFlakinessRepair();
      return;
    }

    if (module === 'all') {
      await this._handleCoordinatedRun(threshold);
      return;
    }

    this._postPersistent('pipeline-error', 'error', `Unknown action: ${module}`);
  }

  /**
   * Run all three modules under backend agent coordination.
   *
   * The chaining used to live here, which meant the extension decided the
   * order and nothing could route a failure backwards. The agent owns that now,
   * so this method only renders what it reports.
   */
  private async _handleCoordinatedRun(threshold: number) {
    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (!workspaceFolders || workspaceFolders.length === 0) {
      this._postPersistent('pipeline-error', 'error',
        'No workspace folder is open. Open a project folder first.');
      return;
    }

    const workspaceUri = workspaceFolders[0].uri;

    this._postStep(
      'Running the full pipeline: **Dockerfile → container tests → flakiness**. ' +
      'Failing tests are routed back for repair, and a flakiness fix is re-verified. ' +
      'Flakiness builds run without cache, so expect this to take a while.'
    );

    try {
      const response = await fetch(`${this._backendBaseUrl}/pipeline/run`, {
        method: 'POST',
        signal: this._activeController?.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          workspace_path: workspaceUri.fsPath,
          threshold,
          // Apply a flakiness repair in place. This is also what makes
          // Feedback B reachable: the agent only re-verifies the container
          // tests when the Dockerfile it tested has actually changed.
          apply: true
        })
      });

      if (!response.ok) { throw new Error(`Backend returned ${response.status}`); }
      if (!response.body) { throw new Error('No response body from backend.'); }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { value, done } = await reader.read();
        if (done) { break; }

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';

        for (const line of lines) {
          if (!line.startsWith('data: ')) { continue; }
          let event: RunEvent;
          try {
            event = JSON.parse(line.slice(6));
          } catch { continue; }

          if (event.step === 'done') {
            await this._reportRun(event);
          } else if (event.step === 'error') {
            this._postPipelineError(event.message);
          } else if (event.stage === 'agent') {
            // The agent's routing decisions are the most interesting thing on
            // screen — they are what makes the feedback loop visible.
            this._postStep(`**Agent** — ${event.message}`);
          } else {
            this._handleProgressEvent(event.step, event.message, event.stage);
          }
        }
      }
    } catch (error) {
      if (!this._activeController?.signal.aborted) {
        this._postPersistent('pipeline-error', 'error',
          this._formatError(error, 'Full pipeline failed. Is the backend running?'));
      }
    }
  }

  /** Closing summary plus a recap of what each module found. */
  private async _reportRun(event: RunEvent) {
    const state = event.state;
    const detail = state ? this._runDetail(state) : undefined;
    const failed =
      state?.test && state.test.executed && state.test.failed > 0;

    this._postPersistent(
      failed ? 'pipeline-error' : 'pipeline-success',
      failed ? 'error' : 'success',
      event.message,
      detail
    );

    if (event.output_path) {
      try {
        const doc = await vscode.workspace.openTextDocument(event.output_path);
        await vscode.window.showTextDocument(doc);
      } catch { /* the file may not exist if generation failed */ }
    }
  }

  private _runDetail(state: RunState): string {
    const lines: string[] = [];
    if (state.generation) {
      lines.push(`Dockerfile: ${state.generation.message}`);
    }
    if (state.test) {
      lines.push(state.test.executed
        ? `Tests: ${state.test.passed}/${state.test.total} passed`
        : 'Tests: written but not executed');
      for (const t of state.test.failing ?? []) {
        lines.push(`  ✗ ${t.name}${t.errors?.[0] ? ` — ${t.errors[0]}` : ''}`);
      }
    }
    if (state.flakiness) {
      lines.push(`Flakiness: ${state.flakiness.verdict}`);
      if (state.flakiness.repaired) {
        lines.push(`  repair written to ${state.flakiness.repaired_path}`);
      }
    }
    lines.push(`Feedback rounds used: ${state.feedback_rounds}`);
    return lines.join('\n');
  }

  private async _handleDockerfileGeneration(): Promise<boolean> {
    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (!workspaceFolders || workspaceFolders.length === 0) {
      this._postPersistent(
        'pipeline-error', 'error',
        'No workspace folder is open. Open a project folder first.'
      );
      return false;
    }

    const workspacePath = workspaceFolders[0].uri.fsPath;

    try {
      const response = await fetch(`${this._backendBaseUrl}/pipeline/generate`, {
        method: 'POST',
        signal: this._activeController?.signal,
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
        if (done) { break; }

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';

        for (const line of lines) {
          if (!line.startsWith('data: ')) { continue; }
          let event: { step: string; message: string; output_path?: string };
          try {
            event = JSON.parse(line.slice(6));
          } catch { continue; }

          if (event.step === 'done') {
            succeeded = true;
            this._postPersistent('pipeline-success', 'success', event.message);
            if (event.output_path) {
              const doc = await vscode.workspace.openTextDocument(event.output_path);
              vscode.window.showTextDocument(doc);
            }
          } else if (event.step === 'error') {
            this._postPipelineError(event.message);
          } else {
            this._handleProgressEvent(event.step, event.message);
          }
        }
      }

      return succeeded;
    } catch (error) {
      if (!this._activeController?.signal.aborted) {
        this._postPersistent(
          'pipeline-error', 'error',
          this._formatError(error, 'Dockerfile generation failed. Is the backend running?')
        );
      }
      return false;
    }
  }

  private async _handleFlakinessRepair() {
    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (!workspaceFolders || workspaceFolders.length === 0) {
      this._postPersistent('pipeline-error', 'error',
        'No workspace folder is open. Open a project with a Dockerfile first.');
      return;
    }

    const workspaceUri = workspaceFolders[0].uri;
    const dockerfileUri = vscode.Uri.joinPath(workspaceUri, 'Dockerfile');
    try {
      await vscode.workspace.fs.stat(dockerfileUri);
    } catch {
      this._postPersistent('pipeline-error', 'error',
        'No Dockerfile found in the workspace root. Generate one first.');
      return;
    }

    // Captured before the run: the repair overwrites this file, and the diff
    // is shown against this text rather than against a file left on disk.
    let originalDockerfile = '';
    try {
      originalDockerfile = Buffer.from(
        await vscode.workspace.fs.readFile(dockerfileUri)
      ).toString('utf8');
    } catch {
      originalDockerfile = '';
    }

    // Flakiness only shows up without the build cache, so every build is a
    // cold one. Say so before the user is left staring at a slow run.
    this._postStep(
      'Checking for flakiness. Builds run **without cache**, so this is slow — ' +
      'up to 8 full rebuilds if a repair takes several attempts.'
    );

    try {
      const response = await fetch(`${this._backendBaseUrl}/pipeline/flakiness`, {
        method: 'POST',
        signal: this._activeController?.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          workspace_path: workspaceUri.fsPath,
          dockerfile_path: dockerfileUri.fsPath,
          repair: true,
          apply: true           // overwrite the Dockerfile; the diff is shown
                                // against the in-memory original
        })
      });

      if (!response.ok) { throw new Error(`Backend returned ${response.status}`); }
      if (!response.body) { throw new Error('No response body from backend.'); }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { value, done } = await reader.read();
        if (done) { break; }

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';

        for (const line of lines) {
          if (!line.startsWith('data: ')) { continue; }
          let event: FlakinessDoneEvent;
          try {
            event = JSON.parse(line.slice(6));
          } catch { continue; }

          if (event.step === 'done') {
            await this._reportFlakiness(event, dockerfileUri, originalDockerfile);
          } else if (event.step === 'verdict') {
            // Rendered here rather than on `done`: retrieval and repair stream
            // their own lines after this, so a verdict held back to the end
            // reads as if the builds failed after the repair was validated.
            this._postDetectionVerdict(event.detection, event.message);
          } else if (event.step === 'error') {
            this._postPipelineError(event.message);
          } else {
            this._handleProgressEvent(event.step, event.message);
          }
        }
      }
    } catch (error) {
      if (!this._activeController?.signal.aborted) {
        this._postPersistent('pipeline-error', 'error',
          this._formatError(error, 'Flakiness check failed. Is the backend running?'));
      }
    }
  }

  /** Report the detection verdict at the moment detection finishes. */
  private _postDetectionVerdict(
    detection: FlakinessDoneEvent['detection'],
    message: string
  ) {
    if (!detection) {
      this._postStep(message);
      return;
    }

    if (detection.verdict === 'stable') {
      this._postPersistent('pipeline-success', 'success',
        `No flakiness detected across ${detection.iterations} builds`, message);
      return;
    }

    const headline = detection.is_flaky
      ? `Flaky — ${detection.successes} of ${detection.iterations} builds passed with identical input`
      : `Not reproducible — all ${detection.iterations} builds failed`;

    const detail = [
      message,
      detection.failing_instruction
        ? `\nFailing instruction:\n    ${detection.failing_instruction}`
        : ''
    ].filter(Boolean).join('\n');

    this._postStep(headline, detail);
  }

  /** Render the flakiness repair outcome, and show what changed. */
  private async _reportFlakiness(
    event: FlakinessDoneEvent,
    dockerfileUri: vscode.Uri,
    originalDockerfile = ''
  ) {
    const repair = event.repair;

    // The verdict was already reported from the `verdict` event, in sequence.
    if (!repair) { return; }

    if (repair.success && repair.repaired_path) {
      const attempts = `${repair.attempts} attempt${repair.attempts === 1 ? '' : 's'}`;
      const summary = repair.applied
        ? `${repair.message} (${attempts})\n\nYour Dockerfile has been updated. ` +
          `The diff shows what changed — undo it with Git if you disagree.`
        : `${repair.message} (${attempts})\n\nReview the diff and apply it if you agree.`;
      this._postPersistent('pipeline-success', 'success', summary,
        repair.demonstrations?.length
          ? `Guided by similar repairs:\n${repair.demonstrations.map(d => `    ${d}`).join('\n')}`
          : undefined);

      if (repair.applied) {
        // Diff the snapshot taken before the run against the file as it is
        // now, so the change is reviewable even though it was written in place.
        const beforeUri = this._originals.snapshot(
          '/Dockerfile (before repair)', originalDockerfile
        );
        await vscode.commands.executeCommand(
          'vscode.diff', beforeUri, dockerfileUri,
          'Dockerfile — before ↔ after repair'
        );
      } else {
        await vscode.commands.executeCommand(
          'vscode.diff', dockerfileUri, vscode.Uri.file(repair.repaired_path),
          'Dockerfile ↔ Proposed repair'
        );
      }
    } else {
      this._postPersistent('pipeline-error', 'error', repair.message);
    }
  }

  private async _handleTestGeneration(threshold: number) {
    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (!workspaceFolders || workspaceFolders.length === 0) {
      this._postPersistent('pipeline-error', 'error', 'No workspace folder is open. Please open a folder that contains a Dockerfile.');
      return;
    }

    const workspacePath = workspaceFolders[0].uri.fsPath;

    const dockerfileUri = vscode.Uri.joinPath(workspaceFolders[0].uri, 'Dockerfile');
    try {
      await vscode.workspace.fs.stat(dockerfileUri);
    } catch {
      this._postPersistent('pipeline-error', 'error', 'No Dockerfile found in the workspace root.');
      return;
    }

    const dockerfilePath = dockerfileUri.fsPath;

    try {
      const response = await fetch(`${this._backendBaseUrl}/pipeline/test`, {
        method: 'POST',
        signal: this._activeController?.signal,
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
        if (done) { break; }

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';

        for (const line of lines) {
          if (!line.startsWith('data: ')) { continue; }
          let event: TestDoneEvent;
          try {
            event = JSON.parse(line.slice(6));
          } catch {
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
              this._postPersistent(
                ok ? 'pipeline-success' : 'pipeline-error',
                ok ? 'success' : 'error',
                summary,
                this._failureDetail(cases)
              );
            } else {
              this._postPersistent('pipeline-success', 'success', event.message);
            }

            // Execution problems are non-fatal — the spec is still usable.
            if (event.warning) {
              this._postStep(event.warning);
            }

            if (event.results_path) {
              this._postStep(`Results saved to ${event.results_path}`);
            }

            if (outputPath) {
              const doc = await vscode.workspace.openTextDocument(outputPath);
              vscode.window.showTextDocument(doc);
            }
          } else if (event.step === 'error') {
            this._postPipelineError(event.message);
          } else {
            this._handleProgressEvent(event.step, event.message);
          }
        }
      }
    } catch (error) {
      if (!this._activeController?.signal.aborted) {
        this._postPersistent('pipeline-error', 'error', this._formatError(error, 'Test generation failed. Is the backend running?'));
      }
    }
  }

  private _postStatus(label: string, meta?: string, badge?: string) {
    this._runLabel = label;
    this._runMeta = meta ?? '';
    this._runBadge = badge ?? '';
    // startedAt lets a rebuilt webview re-arm the strip with the correct elapsed time.
    this._view?.webview.postMessage({
      type: 'status-update',
      label,
      meta,
      badge,
      startedAt: this._runStartedAt || Date.now(),
      stopping: this._activeController?.signal.aborted ?? false
    });
  }

  /** A pipeline milestone: kept in the transcript, does not end the run. */
  private _postStep(text: string, detail?: string) {
    this._addToHistory({ variant: 'step', text });   // detail is deliberately not persisted
    this._view?.webview.postMessage({ type: 'pipeline-step', text, detail });
  }

  /**
   * Turn a raw `{step, message}` event into something worth showing.
   * Anything not matched falls back to the raw message as a status line only.
   */
  private _progressView(step: string, message: string): ProgressView {
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

    // ── Flakiness detection and repair ───────────────────────────────────
    const flakyBuild = message.match(/Build (\d+) of (\d+)/i);
    if (flakyBuild) {
      return {
        label: step === 'repair' ? 'Validating the repair' : 'Testing for flakiness',
        meta: `build ${flakyBuild[1]} of ${flakyBuild[2]}`
      };
    }

    const generatingRepair = message.match(/Generating repair (\d+) of (\d+)/i);
    if (generatingRepair) {
      return {
        label: 'Writing a repair',
        meta: `attempt ${generatingRepair[1]} of ${generatingRepair[2]}`
      };
    }

    const validating = message.match(/Validating repair (\d+) \((\d+) builds?\)/i);
    if (validating) {
      return { label: 'Validating the repair', meta: `${validating[2]} clean builds needed` };
    }

    const retrieved = message.match(/Retrieved (\d+) similar repairs?:\s*(.+)/i);
    if (retrieved) {
      // The labels repeat often — three hits on one fault type is the common
      // case — so joining them truncated to "Fault, Repository Depr…" said
      // less than a count does. The breakdown goes in the detail panel.
      const counts = new Map<string, number>();
      for (const label of retrieved[2].split(',').map(l => l.trim()).filter(Boolean)) {
        counts.set(label, (counts.get(label) ?? 0) + 1);
      }
      const kinds = [...counts.entries()];
      return {
        label: `Found ${retrieved[1]} similar repairs`,
        meta: kinds.length === 1
          ? kinds[0][0]
          : `${kinds.length} fault types`,
        detail: kinds.map(([name, n]) => (n > 1 ? `${name} \u00d7${n}` : name)).join('\n'),
        milestone: true
      };
    }

    if (step === 'verdict') {
      if (/^Flaky:/i.test(message)) {
        return { label: 'Flaky build detected', detail: message, milestone: true };
      }
      if (/^Failed all/i.test(message)) {
        return { label: 'Not reproducible — all builds failed', detail: message, milestone: true };
      }
      return { label: 'No flakiness detected', detail: message, milestone: true };
    }

    if (/Unable to resolve/i.test(message)) {
      return { label: 'Could not repair — the same error kept recurring', milestone: true };
    }

    if (/Repair validated across/i.test(message)) {
      // Not a milestone: the closing event reports this with the attempt
      // count a moment later, and two bubbles in a row read as a stutter.
      return { label: 'Repair validated' };
    }

    if (/still fails/i.test(message)) {
      return { label: 'That repair still fails — trying again' };
    }

    if (/without cache/i.test(message)) {
      return { label: 'Testing for flakiness', meta: 'builds run without cache' };
    }

    // Phase B already emits short, human-readable lines — pass them through
    // rather than flattening every sub-step to one label.
    if (step === 'optimizing') {
      const clean = message
        .replace(/^Phase B\s*[—–-]\s*/i, '')
        .replace(/[….]+$/, '')
        .trim();
      const cased = clean.charAt(0).toUpperCase() + clean.slice(1);
      return { label: cased };
    }

    // ── Fixed steps, keyed off the SSE step name ─────────────────────────
    const byStep: Record<string, string> = {
      checking:   'Checking workspace',
      context:    'Reading project files',
      generating: 'Writing the Dockerfile',
      detect:     'Testing for flakiness',
      retrieve:   'Finding similar repairs',
      repair:     'Repairing',
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
        meta: isStats ? rest : undefined
      };
    }

    // Unknown event — show it, but keep it short and out of the transcript.
    return { label: this._firstSentence(message), detail: message };
  }

  /** Compress a raw build error into a few words. */
  /**
   * Report a backend failure as a short headline with the raw output collapsed.
   *
   * A failed build arrives as the whole BuildKit log plus a Python traceback.
   * Dumping eighty lines into the panel buried the one line that mattered, so
   * the summary is derived here and the raw text goes in the detail panel,
   * which is already scrollable.
   */
  private _postPipelineError(raw: string) {
    const text = (raw || '').trim();
    if (!text) {
      this._postPersistent('pipeline-error', 'error',
        'The backend reported an error with no detail.');
      return;
    }

    const cause = this._errorCause(text);
    const summary = cause
      ? `${this._errorHeadline(text)}\n\n${cause}`
      : this._errorHeadline(text);

    this._postPersistent('pipeline-error', 'error', summary, this._clamp(text));
  }

  /** Bound the raw log, keeping both ends: the exception is at the top and
   *  the resolved error summary is at the bottom, so only the middle is safe
   *  to drop. */
  private _clamp(text: string): string {
    if (text.length <= MAX_ERROR_DETAIL) { return text; }

    const head = Math.floor(MAX_ERROR_DETAIL * 0.25);
    const tail = MAX_ERROR_DETAIL - head;
    const dropped = text.length - MAX_ERROR_DETAIL;
    return text.slice(0, head)
      + `\n\n\u2026 ${dropped} characters omitted \u2026\n\n`
      + text.slice(-tail);
  }

  /** One line naming what failed. */
  private _errorHeadline(text: string): string {
    const cmd = text.match(
      /process "\/bin\/sh -c (.+?)" did not complete successfully: exit code: (\d+)/i
    );
    if (cmd) {
      return `Build failed on \`${cmd[1].trim()}\` (exit ${cmd[2]})`;
    }

    const timedOut = text.match(/timed out after (\d+) (seconds|minutes)/i);
    if (timedOut) {
      const mins = timedOut[2].toLowerCase().startsWith('second')
        ? Math.round(Number(timedOut[1]) / 60)
        : Number(timedOut[1]);
      return `Build timed out after ${mins} minute${mins === 1 ? '' : 's'}`;
    }

    // Not every exception name ends in "Error" — TimeoutExpired is the one
    // that matters here, so match that suffix too.
    const exc = text.match(
      /^([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Expired|Timeout)):\s*(.+)$/m
    );
    if (exc) { return `${exc[1]} \u2014 ${this._firstSentence(exc[2])}`; }

    const first = text.split('\n').map(l => l.trim()).find(Boolean);
    return this._firstSentence(first ?? 'Something went wrong');
  }

  /**
   * Plain-language cause, when the log carries a recognisable one.
   *
   * These mirror the fault categories the flakiness corpus is labelled with,
   * which is where build failures in this project actually cluster.
   */
  private _errorCause(text: string): string | undefined {
    if (/timed out after/i.test(text)) {
      return 'The build was still running when the time limit was reached. '
        + 'Large downloads or packages compiled from source can take longer '
        + 'than the limit allows.';
    }

    if (/pull access denied|manifest unknown|repository does not exist/i.test(text)) {
      return 'The base image could not be pulled — it may have been renamed or removed.';
    }

    if (/(Failed to fetch|does not have a Release file)/i.test(text)
        && /404|Release file/i.test(text)) {
      return 'The base image\u2019s package repositories are no longer served (404). '
        + 'That distribution has most likely reached end of life.';
    }

    const apt = text.match(/Unable to locate package (\S+)/i);
    if (apt) { return `apt cannot find the package \`${apt[1]}\`.`; }

    const pip = text.match(/No matching distribution found for (\S+)/i);
    if (pip) {
      return `pip cannot find a release of \`${pip[1]}\` for this Python version.`;
    }

    if (/externally-managed-environment/i.test(text)) {
      return 'The image blocks system-wide pip installs (PEP 668). '
        + 'Install into a virtualenv instead.';
    }

    const missing = text.match(/([\w.\-/]+): not found/i);
    if (missing) { return `\`${missing[1]}\` is not present in the image.`; }

    return undefined;
  }

  private _summarizeError(raw: string): string {
    const text = raw.trim();

    const failedCmd = text.match(/process "\/bin\/sh -c (.+?)" did not complete/i);
    if (failedCmd) { return `\`${failedCmd[1].trim()}\` failed`; }

    const aptMissing = text.match(/Unable to locate package (\S+)/i);
    if (aptMissing) { return `package not found: ${aptMissing[1]}`; }

    const pipMissing = text.match(/No matching distribution found for (\S+)/i);
    if (pipMissing) { return `package not found: ${pipMissing[1]}`; }

    const missingFile = text.match(/no such file or directory[:,]?\s*'?([^'\s]+)/i);
    if (missingFile) { return `missing file: ${missingFile[1]}`; }

    const notFound = text.match(/([\w.\-/]+): not found/i);
    if (notFound) { return `not found: ${notFound[1]}`; }

    // Fall back to the first meaningful line, minus the usual noise prefixes.
    const firstLine = text.split('\n')[0]
      .replace(/^(ERROR|error|E):\s*/, '')
      .replace(/^failed to solve:\s*/i, '')
      .trim();
    return firstLine;
  }

  /** Render failing cases for the collapsible detail panel. */
  private _failureDetail(cases: TestCaseResult[]): string | undefined {
    const failing = (cases || []).filter(c => !c.passed);
    if (failing.length === 0) { return undefined; }
    return failing
      .map(c => {
        const errors = (c.errors || []).map(e => `    ${e}`).join('\n');
        return errors ? `✗ ${c.name}\n${errors}` : `✗ ${c.name}`;
      })
      .join('\n\n');
  }

  /** The opening sentence, for a message whose full text lives in the detail panel. */
  private _firstSentence(text: string): string {
    const clean = text.replace(/\s+/g, ' ').trim();
    const end = clean.search(/[.!?](\s|$)/);
    return end > 0 ? clean.slice(0, end + 1) : clean;
  }

  /** Route one pipeline event to the status strip and, if notable, the transcript. */
  private _handleProgressEvent(step: string, message: string, stage?: string) {
    const view = this._progressView(step, message);
    const badge = stage ? STAGE_BADGES[stage] : undefined;
    this._postStatus(view.label, view.meta, view.badge ?? badge);
    if (view.milestone) {
      // The transcript has room for the full module name; the strip does not.
      const prefix = stage && STAGE_LABELS[stage] ? `${STAGE_LABELS[stage]} · ` : '';
      const text = view.meta
        ? `${prefix}${view.label} — ${view.meta}`
        : `${prefix}${view.label}`;
      this._postStep(text, view.detail);
    }
  }

  private _formatError(error: unknown, fallbackMessage: string): string {
    if (error instanceof Error && error.message) {
      return error.message;
    }
    return fallbackMessage;
  }

  private _getHtmlContent(webview: vscode.Webview): string {
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._context.extensionUri, 'src', 'webview', 'main.js')
    );
    const styleUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._context.extensionUri, 'src', 'webview', 'style.css')
    );

    const nonce = getNonce();

    return /* html */`
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
            <button class="da-icon-btn da-icon-btn--danger" id="clearBtn"
                    title="Clear conversation" aria-label="Clear conversation">
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"
                   stroke="currentColor" stroke-width="1.2" stroke-linecap="round"
                   stroke-linejoin="round">
                <path d="M3 4.5 H13"/>
                <path d="M6.25 4.5 V2.75 H9.75 V4.5"/>
                <path d="M4.25 4.5 L5 13.25 H11 L11.75 4.5"/>
                <path d="M6.75 7.25 V11"/>
                <path d="M9.25 7.25 V11"/>
              </svg>
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
              <span class="da-status__line">
                <span class="da-status__badge" id="statusBadge" hidden></span>
                <span class="da-status__label" id="statusLabel">Working</span>
              </span>
              <span class="da-status__meta" id="statusMeta" hidden></span>
            </span>
            <span class="da-status__time" id="statusTime">0:00</span>
            <button class="da-status__stop" id="stopBtn" type="button" title="Stop current run">Stop</button>
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

function getNonce(): string {
  let text = '';
  const possible = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  for (let i = 0; i < 32; i++) {
    text += possible.charAt(Math.floor(Math.random() * possible.length));
  }
  return text;
}
