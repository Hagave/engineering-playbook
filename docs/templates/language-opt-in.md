<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) user's explicit request, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis — stop and confirm.
<!-- /RULE-ANCHOR -->

# Language Opt-In Template

This is the canonical structure for `engineering-{lang}` skills. Each section below is required; the language-specific worker fills the content while keeping the section headings and order.

## Required frontmatter

```yaml
# frontmatter is required - example only
name: engineering-{lang}                # lowercase, hyphenated
description: "**Opt-in for {Language}:** load when {language} is the implementation language. Ecosystem summary, framework choices, ORM/driver, test runner, linter/formatter, and idiomatic patterns for layering, DDD, and concurrency in {language}. The trigger fires when the project is {language} or when {language}-specific conventions are needed."
version: 1.0.0
user-invocable: false
```

The RULE-ANCHOR and OUTPUT-DISCIPLINE blocks are **byte-identical to every other skill** — copy verbatim from `engineering-typescript/SKILL.md` or any other skill in the catalog. Structural tests assert this.

## Required sections (in order)

### 1. Runtime and package manager

- Package manager: [tool]
- Version manager: [tool, optional]
- Lockfile: [lockfile-name]
- Build tool: [tool, when applicable]

### 2. Frameworks

| Framework | When to pick |
|---|---|
| [framework-primary] | [mainstream use case] |
| [framework-alternative] | [non-mainstream use case] |

Pick the 2-3 most popular frameworks for the language. Do not attempt exhaustive coverage.

### 3. Database access

| Layer | Options |
|---|---|
| ORM | [list, max 3] |
| Raw SQL driver | [list, max 3] |
| Migration tool | [list, max 2] |

### 4. Test runner

- Primary: [runner]
- Test layout: [conventional path]
- Coverage: [tool]
- Mocking: [tool]

### 5. Linting and formatting

- Linter: [tool]
- Formatter: [tool]
- Type checker: [tool, when applicable]

### 6. Idiomatic patterns

#### Value objects
[language-idiomatic VO example — code block, max 12 lines]

#### Entities
[language-idiomatic entity example — code block, max 12 lines]

#### Dependency injection
[DI mechanism — code block, max 8 lines]

#### Async / concurrency
[language-idiomatic async — code block, max 8 lines]

### 7. Layer boundaries

[1 short paragraph: how the language typically structures layers, with reference to engineering-arch-hexagonal / engineering-arch-modular / engineering-arch-mvc. Generic content lives there; this section only adds language-specific placement notes.]

### 8. Cross-references

- DDD patterns: engineering-ddd
- Layer rules: engineering-architecture
- Hexagonal structure: engineering-arch-hexagonal
- Modular monolith: engineering-arch-modular
- MVC: engineering-arch-mvc
- Concurrency: engineering-concurrency
- Error handling: engineering-error-handling
- Testing: engineering-testing
- Performance: engineering-performance

## Hard constraints

1. **RULE-ANCHOR and OUTPUT-DISCIPLINE are byte-identical** to the canonical blocks. Copy verbatim from any existing skill. Do not edit.
2. **Frontmatter ordering and field names** are fixed; only description content and version vary.
3. **No nesting deeper than 4 levels (####)** in this template.
4. **Code examples are language-idiomatic** — the language's own conventions, not translated from TypeScript. Value objects, entities, DI, and async must showcase the language's idioms.
5. **No new sections** beyond the 8 listed. If a section feels missing, fold it into the closest existing one.
6. **No new cross-references** beyond the 9 listed. The generic skills own the principles; this skill only shows how to apply them in this language.
7. **No tools configuration deep-dives.** Mention the tool name and its role; do not paste config files. The project's own config wins.
8. **Length cap: 220 lines per skill.** This is a soft cap; if the language genuinely needs more, expand Idiomatic patterns instead of adding new sections.

## What NOT to do

- Do not paste multi-file boilerplate. One example per concept.
- Do not recommend unstable, alpha, or experimental frameworks. Pick mature, production-proven options.
- Do not include the language's stdlib documentation. The skill is for ENGINEERING tooling choices, not language tutorials.
- Do not duplicate engineering-typescript content. Each language is its own ecosystem.

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
When ambiguous: stop, list the question, wait. Never assume.
<!-- /OUTPUT-DISCIPLINE -->
