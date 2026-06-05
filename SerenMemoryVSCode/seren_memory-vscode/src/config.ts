import * as vscode from "vscode";

const SECRET_KEY = "serenMemory.bearerToken";

export class SerenConfig {
  constructor(private readonly secrets: vscode.SecretStorage) {}

  get endpoint(): string {
    const raw = vscode.workspace
      .getConfiguration("serenMemory")
      .get<string>("endpoint", "http://localhost:7420");
    return raw.replace(/\/$/, "");
  }

  get startCommand(): string {
    return vscode.workspace
      .getConfiguration("serenMemory")
      .get<string>("startCommand", "python -m seren_memory");
  }

  /** When true, suppress the "service not reachable - start it?" prompt
   *  on extension activation. User-toggleable via the don't-ask-again
   *  button on that prompt, or directly in settings. */
  get suppressStartPrompt(): boolean {
    return vscode.workspace
      .getConfiguration("serenMemory")
      .get<boolean>("suppressStartPrompt", false);
  }

  async setSuppressStartPrompt(value: boolean): Promise<void> {
    await vscode.workspace
      .getConfiguration("serenMemory")
      .update("suppressStartPrompt", value, vscode.ConfigurationTarget.Global);
  }

  async getToken(): Promise<string | undefined> {
    return this.secrets.get(SECRET_KEY);
  }

  async setToken(token: string): Promise<void> {
    await this.secrets.store(SECRET_KEY, token);
  }

  async deleteToken(): Promise<void> {
    await this.secrets.delete(SECRET_KEY);
  }

  async getHeaders(): Promise<Record<string, string>> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    const token = await this.getToken();
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }
    return headers;
  }
}

/** Prompt the user to enter and persist a bearer token. */
export async function promptSetToken(config: SerenConfig): Promise<void> {
  const token = await vscode.window.showInputBox({
    title: "Seren Memory: Set Bearer Token",
    prompt: "Enter the bearer token for your SerenMemory service (leave blank to clear).",
    password: true,
    ignoreFocusOut: true,
  });
  if (token === undefined) {
    return; // cancelled
  }
  if (token === "") {
    await config.deleteToken();
    vscode.window.showInformationMessage("Seren Memory: bearer token cleared.");
  } else {
    await config.setToken(token);
    vscode.window.showInformationMessage("Seren Memory: bearer token saved.");
  }
}
