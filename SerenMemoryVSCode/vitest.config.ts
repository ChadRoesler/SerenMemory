import { defineConfig } from "vitest/config";
import path from "path";

export default defineConfig({
  test: {
    environment: "node",
  },
  resolve: {
    // Stub out the `vscode` module so SerenClient (which imports config.ts,
    // which imports vscode) can be tested without a VS Code host process.
    alias: {
      vscode: path.resolve(__dirname, "test/mocks/vscode.ts"),
    },
  },
});
