import * as vscode from 'vscode';
import { DockAgentPanel } from './DockAgentPanel';

export function activate(context: vscode.ExtensionContext) {

  // Register the webview view provider (sidebar panel).
  // retainContextWhenHidden keeps the panel's DOM alive while it is collapsed,
  // so an in-flight run — its status strip, timer and scroll position — is
  // still there when the user reopens it.
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(
      'dockagent.chatView',
      new DockAgentPanel(context),
      { webviewOptions: { retainContextWhenHidden: true } }
    )
  );

  // Register commands
  context.subscriptions.push(
    vscode.commands.registerCommand('dockagent.runAll', () => {
      vscode.commands.executeCommand('dockagent.chatView.focus');
      DockAgentPanel.currentPanel?.postMessage({ type: 'run-all' });
    }),
    vscode.commands.registerCommand('dockagent.generateDockerfile', () => {
      DockAgentPanel.currentPanel?.postMessage({ type: 'run-module', module: 'generate' });
    }),
    vscode.commands.registerCommand('dockagent.generateTests', () => {
      DockAgentPanel.currentPanel?.postMessage({ type: 'run-module', module: 'test' });
    }),
    vscode.commands.registerCommand('dockagent.detectFlakiness', () => {
      DockAgentPanel.currentPanel?.postMessage({ type: 'run-module', module: 'flakiness' });
    })
  );
}

export function deactivate() {}
