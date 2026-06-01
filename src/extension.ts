import * as vscode from 'vscode';
import { DockAgentPanel } from './DockAgentPanel';

export function activate(context: vscode.ExtensionContext) {

  // Register the webview view provider (sidebar panel)
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(
      'dockagent.chatView',
      new DockAgentPanel(context)
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
