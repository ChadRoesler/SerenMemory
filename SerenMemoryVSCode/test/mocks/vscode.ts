/**
 * Minimal vscode API stub for vitest.
 *
 * Only implements the surface that SerenConfig / SerenClient touch so
 * the unit tests can import those modules without a VS Code host process.
 * Add stubs here as needed when new vscode APIs are used in src/.
 */

export const workspace = {
  getConfiguration: (_section?: string) => ({
    get: <T>(key: string, defaultValue: T): T => defaultValue,
    update: async () => {},
  }),
};

export class SecretStorage {
  private store: Record<string, string> = {};
  async get(key: string): Promise<string | undefined> { return this.store[key]; }
  async store(key: string, value: string): Promise<void> { this.store[key] = value; }
  async delete(key: string): Promise<void> { delete this.store[key]; }
}

export const ConfigurationTarget = { Global: 1, Workspace: 2, WorkspaceFolder: 3 };
