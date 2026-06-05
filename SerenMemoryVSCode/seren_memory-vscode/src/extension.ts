import * as vscode from "vscode";
import { SerenConfig, promptSetToken } from "./config";
import { SerenClient } from "./client";
import {
  SearchTool,
  WriteTool,
  BriefTool,
  ConsolidateTool,
  PreserveVerbatimTool,
  PromoteNowTool,
  ForgetLongTool,
  CompleteIntentTool,
  ListDraftsTool,
  DraftChainTool,
  ApproveDraftTool,
  RejectDraftTool,
  SelectDraftTool,
} from "./tools";

let statusBar: vscode.StatusBarItem;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const config = new SerenConfig(context.secrets);
  const client = new SerenClient(config);

  // ── status bar ─────────────────────────────────────────────────────────────
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBar.command = "serenMemory.checkHealth";
  statusBar.text = "$(database) Seren";
  statusBar.tooltip = "Seren Memory - click to check service health";
  statusBar.show();
  context.subscriptions.push(statusBar);

  // ── commands ───────────────────────────────────────────────────────────────
  context.subscriptions.push(
    vscode.commands.registerCommand("serenMemory.setToken", () =>
      promptSetToken(config)
    ),

    vscode.commands.registerCommand("serenMemory.checkHealth", async () => {
      const alive = await client.ping();
      setStatusBar(alive);
      vscode.window.showInformationMessage(
        alive
          ? "Seren Memory: service is reachable ✓"
          : "Seren Memory: service is not reachable ✗"
      );
    }),

    vscode.commands.registerCommand("serenMemory.startService", async () => {
      const cmd = config.startCommand;
      const terminal = vscode.window.createTerminal({
        name: "Seren Memory",
        hideFromUser: false,
      });
      terminal.show();
      terminal.sendText(cmd);
      context.subscriptions.push(terminal);

      // poll for up to 15s then re-check
      await waitForService(client, 15);
      const alive = await client.ping();
      setStatusBar(alive);
      if (alive) {
        vscode.window.showInformationMessage("Seren Memory: service started ✓");
      } else {
        vscode.window.showWarningMessage(
          "Seren Memory: service may still be starting - check the terminal."
        );
      }
    })
  );

  // ── register LM tools ──────────────────────────────────────────────────────
  context.subscriptions.push(
    vscode.lm.registerTool("seren_memory_search", new SearchTool(client)),
    vscode.lm.registerTool("seren_memory_write", new WriteTool(client)),
    vscode.lm.registerTool("seren_memory_brief", new BriefTool(client)),
    vscode.lm.registerTool("seren_memory_consolidate", new ConsolidateTool(client)),
    vscode.lm.registerTool("seren_memory_preserve_verbatim", new PreserveVerbatimTool(client)),
    vscode.lm.registerTool("seren_memory_promote_now", new PromoteNowTool(client)),
    vscode.lm.registerTool("seren_memory_forget_long", new ForgetLongTool(client)),
    vscode.lm.registerTool("seren_memory_complete_intent", new CompleteIntentTool(client)),
    vscode.lm.registerTool("seren_memory_list_drafts", new ListDraftsTool(client)),
    vscode.lm.registerTool("seren_memory_draft_chain", new DraftChainTool(client)),
    vscode.lm.registerTool("seren_memory_approve_draft", new ApproveDraftTool(client)),
    vscode.lm.registerTool("seren_memory_reject_draft", new RejectDraftTool(client)),
    vscode.lm.registerTool("seren_memory_select_draft", new SelectDraftTool(client))
  );

  // ── startup health check ───────────────────────────────────────────────────
  const alive = await client.ping();
  setStatusBar(alive);

  if (!alive && !config.suppressStartPrompt) {
    const choice = await vscode.window.showWarningMessage(
      "Seren Memory: service is not reachable. Would you like to start it?",
      "Start Service",
      "Set Endpoint",
      "Don't Ask Again",
      "Dismiss"
    );
    if (choice === "Start Service") {
      vscode.commands.executeCommand("serenMemory.startService");
    } else if (choice === "Set Endpoint") {
      vscode.commands.executeCommand(
        "workbench.action.openSettings",
        "serenMemory.endpoint"
      );
    } else if (choice === "Don't Ask Again") {
      await config.setSuppressStartPrompt(true);
      vscode.window.showInformationMessage(
        "Seren Memory: startup prompt suppressed. " +
        "Toggle 'serenMemory.suppressStartPrompt' in settings to re-enable."
      );
    }
  }
}

export function deactivate(): void {
  statusBar?.dispose();
}

// ── helpers ────────────────────────────────────────────────────────────────

function setStatusBar(alive: boolean): void {
  if (alive) {
    statusBar.text = "$(database) Seren ✓";
    statusBar.backgroundColor = undefined;
    statusBar.tooltip = "Seren Memory - service reachable";
  } else {
    statusBar.text = "$(database) Seren ✗";
    statusBar.backgroundColor = new vscode.ThemeColor(
      "statusBarItem.warningBackground"
    );
    statusBar.tooltip = "Seren Memory - service not reachable. Click to check again.";
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForService(client: SerenClient, maxSeconds: number): Promise<void> {
  const deadline = Date.now() + maxSeconds * 1000;
  while (Date.now() < deadline) {
    await sleep(1000);
    if (await client.ping()) {
      return;
    }
  }
}
