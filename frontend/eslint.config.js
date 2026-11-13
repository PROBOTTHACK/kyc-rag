import js from "@eslint/js";
import { defineConfig, globalIgnores } from "eslint/config";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default defineConfig([
  // Generated from docs/api/openapi.json (`npm run gen:api`); never edited by hand.
  globalIgnores(["dist", "src/api/schema.d.ts"]),
  {
    files: ["src/**/*.{ts,tsx}"],
    extends: [js.configs.recommended, tseslint.configs.recommended],
    plugins: { "react-hooks": reactHooks },
    rules: { ...reactHooks.configs.flat.recommended.rules },
    languageOptions: { globals: globals.browser },
  },
  {
    files: ["*.config.{js,ts}"],
    extends: [js.configs.recommended, tseslint.configs.recommended],
    languageOptions: { globals: globals.node },
  },
]);
