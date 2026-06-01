import * as vscode from 'vscode';
export declare class DockAgentPanel implements vscode.WebviewViewProvider {
    private readonly _context;
    static currentPanel: DockAgentPanel | undefined;
    private _view?;
    constructor(_context: vscode.ExtensionContext);
    resolveWebviewView(webviewView: vscode.WebviewView, _context: vscode.WebviewViewResolveContext, _token: vscode.CancellationToken): void;
    postMessage(message: object): void;
    private _handleMessage;
    private _getHtmlContent;
}
