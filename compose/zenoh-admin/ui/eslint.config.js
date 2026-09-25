import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import prettierConfig from 'eslint-config-prettier'

// Flat config (typescript.md: "Use the modern flat config with typescript-eslint
// and, for React projects, eslint-plugin-react-hooks"). Rules kept close to
// each plugin's own recommended set rather than hand-picked strictness —
// this repo's compact style (short-circuit returns, dense JSX) is a
// formatting choice, not a correctness one, so it's Prettier's job to leave
// alone, not ESLint's job to flag.
export default tseslint.config(
  { ignores: ['dist', 'node_modules'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      // The classic two hooks rules only — v7's "recommended" preset also
      // pulls in newer, much stricter React-Compiler-oriented checks
      // (set-state-in-effect, purity, ...) that flag long-standing, correct
      // patterns already throughout this codebase (data-fetching effects,
      // etc.) as errors. Adopting those is a real behavioral discussion for
      // this codebase, not something "add an ESLint config" should force.
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      // Every routes/*.tsx file exports its route config + component +
      // local helper types side by side by design (TanStack Router's own
      // file-route convention) — this rule assumes a one-component-per-file
      // layout and would otherwise warn on nearly every route file.
      'react-refresh/only-export-components': 'off',
      // Existing code prefixes an intentionally-unused destructured/catch
      // binding with `_` (see e.g. terminal.tsx's catch blocks) rather than
      // omitting it — recognize that convention instead of flagging it.
      '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      // A bare `catch {}` is a deliberate "one failed fetch shouldn't blank
      // the rest of the page" pattern used throughout this codebase (see
      // routes/index.tsx's independent per-widget fetches) — not dead code.
      'no-empty': ['error', { allowEmptyCatch: true }],
    },
  },
  // Must be last: disables any ESLint stylistic rule that would conflict
  // with Prettier's own formatting once Prettier is actually run.
  prettierConfig,
)
