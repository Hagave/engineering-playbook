---
name: engineering-linting
description: "Linting and formatting setup: Biome (formatting + linting) and typescript-eslint (no-unsafe-* rules). Load when setting up or auditing the linting stack on a TypeScript/NestJS project."
version: 1.0.1
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Linting

Use the language's established formatter/linter stack and keep it committed to the project. The following sections are TypeScript-specific.

## TypeScript projects

Two mandatory tools working together. Neither replaces the other — they cover different rule domains.

| Tool | Responsibility |
|---|---|
| **Biome** | Formatting, import organization, and linting including type-aware rules via Scanner (v2) |
| **typescript-eslint** | `no-unsafe-*` rules — these have no Biome equivalent and will not be added |

Biome 2 Scanner enables `noFloatingPromises` and `noUnresolvedImports`. However, `no-unsafe-argument`, `no-unsafe-call`, `no-unsafe-member-access`, `no-unsafe-assignment`, and `no-unsafe-return` are absent from Biome with no roadmap. These five rules are the only reason typescript-eslint stays in the stack.

Never configure Prettier — Biome handles all formatting. Never remove typescript-eslint — the `no-unsafe-*` rules it enforces are not available in Biome.

---

## Install

```bash
<pm> add -D @biomejs/biome eslint @eslint/js typescript-eslint eslint-plugin-unused-imports globals
```

Remove any pre-existing Prettier or opinionated ESLint configs if present:

```bash
<pm> remove eslint-config-prettier prettier
rm -f .eslintrc.js .eslintrc.cjs .prettierrc .prettierignore
```

---

## Biome

Reference `biome.json`:

```json
{
  "$schema": "https://biomejs.dev/schemas/2.0.0/schema.json",
  "organizeImports": { "enabled": true },
  "formatter": {
    "enabled": true,
    "indentStyle": "space",
    "indentWidth": 2,
    "lineWidth": 100,
    "bracketSpacing": true
  },
  "linter": {
    "enabled": true,
    "rules": {
      "recommended": true,
      "correctness": {
        "noUnusedVariables": "error",
        "noUnusedImports": "error",
        "noFloatingPromises": "error"
      },
      "suspicious": {
        "noExplicitAny": "error",
        "noConsole": "error"
      },
      "style": {
        "useExplicitType": "error"
      }
    }
  },
  "javascript": {
    "formatter": {
      "quoteStyle": "single",
      "trailingCommas": "all",
      "semicolons": "always"
    }
  },
  "files": {
    "ignore": ["dist", "node_modules"]
  },
  "overrides": [
    {
      "include": ["**/*.spec.ts", "**/*.test.ts", "test/**/*.ts"],
      "linter": {
        "rules": {
          "suspicious": {
            "noExplicitAny": "off",
            "noConsole": "off"
          }
        }
      }
    }
  ]
}
```

---

## typescript-eslint

Reference `eslint.config.ts`:

```typescript
// @ts-check

import eslint from '@eslint/js';
import unusedImports from 'eslint-plugin-unused-imports';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  {
    ignores: ['dist', 'node_modules'],
  },
  eslint.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,

  {
    files: ['**/*.ts'],
    languageOptions: {
      parser: tseslint.parser,
      globals: { ...globals.node },
      sourceType: 'module',
      parserOptions: {
        project: ['./tsconfig.json'],
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: {
      'unused-imports': unusedImports,
    },
    rules: {
      // These five rules have no Biome equivalent — sole reason ESLint remains in the stack.
      '@typescript-eslint/no-unsafe-argument': 'error',
      '@typescript-eslint/no-unsafe-call': 'error',
      '@typescript-eslint/no-unsafe-member-access': 'error',
      '@typescript-eslint/no-unsafe-assignment': 'error',
      '@typescript-eslint/no-unsafe-return': 'error',

      // Covered by Biome too — kept here for CI strictness on type-checked paths.
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/no-floating-promises': 'error',
      'no-console': 'error',
      '@typescript-eslint/explicit-function-return-type': [
        'error',
        {
          allowExpressions: false,
          allowTypedFunctionExpressions: false,
          allowHigherOrderFunctions: false,
          allowDirectConstAssertionInArrowFunctions: false,
        },
      ],
      'unused-imports/no-unused-imports': 'error',
      '@typescript-eslint/no-unused-vars': 'off',
      'unused-imports/no-unused-vars': [
        'warn',
        {
          vars: 'all',
          varsIgnorePattern: '^_',
          args: 'after-used',
          argsIgnorePattern: '^_',
        },
      ],
    },
  },

  {
    files: ['**/*.spec.ts', '**/*.test.ts', 'test/**/*.ts'],
    languageOptions: {
      globals: { ...globals.node, vi: 'readonly' },
    },
    rules: {
      '@typescript-eslint/no-unsafe-call': 'off',
      '@typescript-eslint/no-unsafe-member-access': 'off',
      '@typescript-eslint/no-unsafe-assignment': 'off',
      '@typescript-eslint/no-unsafe-argument': 'off',
      '@typescript-eslint/no-unsafe-return': 'off',
      '@typescript-eslint/no-explicit-any': 'off',
      'no-console': 'off',
    },
  },
);
```

`recommendedTypeChecked` requires `parserOptions.project` pointing to `tsconfig.json`. Without it, type-aware rules silently degrade — unsafe patterns will not be reported.

---

## Scripts

Add to `package.json`:

```json
{
  "scripts": {
    "lint": "biome check --write src && eslint --fix src",
    "lint:check": "biome check src && eslint src"
  }
}
```

`lint` — local use, auto-fixes what it can. `lint:check` — CI only, reports errors without writing.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
