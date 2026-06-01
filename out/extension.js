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
exports.deactivate = exports.activate = void 0;
const vscode = __importStar(require("vscode"));
const DockAgentPanel_1 = require("./DockAgentPanel");
function activate(context) {
    // Register the webview view provider (sidebar panel)
    context.subscriptions.push(vscode.window.registerWebviewViewProvider('dockagent.chatView', new DockAgentPanel_1.DockAgentPanel(context)));
    // Register commands
    context.subscriptions.push(vscode.commands.registerCommand('dockagent.runAll', () => {
        vscode.commands.executeCommand('dockagent.chatView.focus');
        DockAgentPanel_1.DockAgentPanel.currentPanel?.postMessage({ type: 'run-all' });
    }), vscode.commands.registerCommand('dockagent.generateDockerfile', () => {
        DockAgentPanel_1.DockAgentPanel.currentPanel?.postMessage({ type: 'run-module', module: 'generate' });
    }), vscode.commands.registerCommand('dockagent.generateTests', () => {
        DockAgentPanel_1.DockAgentPanel.currentPanel?.postMessage({ type: 'run-module', module: 'test' });
    }), vscode.commands.registerCommand('dockagent.detectFlakiness', () => {
        DockAgentPanel_1.DockAgentPanel.currentPanel?.postMessage({ type: 'run-module', module: 'flakiness' });
    }));
}
exports.activate = activate;
function deactivate() { }
exports.deactivate = deactivate;
//# sourceMappingURL=extension.js.map