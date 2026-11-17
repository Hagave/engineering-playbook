# Parsing untrusted data — deserialization, XXE, prototype pollution, ReDoS, parameter pollution

CWE-502, CWE-611, CWE-776, CWE-1321, CWE-1333, CWE-235, CWE-400

**Load when** the code parses data from outside the process (bodies, queries, files, messages, cookies), merges objects from input, or applies a regex to input.

## Invariant

Parsing yields plain data, bounded in size, depth and time. It never yields code, never yields objects with behavior, and never changes shared prototypes.

## Sinks

- Native deserializers on untrusted bytes:
  - JavaScript `node-serialize` / `serialize-javascript` `eval` mode;
  - Python `pickle.loads`, `yaml.load` without a safe loader;
  - Java `ObjectInputStream`, `XMLDecoder`;
  - Ruby `Marshal.load`, `YAML.load`;
  - PHP `unserialize`;
  - .NET `BinaryFormatter`.
- XML parsers with DTDs or external entities enabled (libxml `noent`, Java `DocumentBuilderFactory` defaults, `lxml` `resolve_entities=True`, SAML and SVG processing).
- Prototype pollution:
  - deep merge or clone of input into an existing object (custom `merge`, outdated `lodash.merge` / `defaultsDeep`, `Object.assign` deep variants);
  - `target[key] = value` where `key` comes from input (path setters like `set(obj, 'a.b', v)`).
- Regexes built from input (`new RegExp(input)`), or patterns with nested quantifiers (`(a+)+`, `(.*)*`, `(\w+\s?)+$`) applied to unbounded input.
- Query parsers that turn `?a=1&a=2` into an array or `?a[b]=1` into an object, where code expects a scalar.
- Bodies without size or nesting limits.

## Control

- Data formats only: JSON plus schema validation, or `yaml.safe_load` / `SafeLoader`. Never native object deserialization of untrusted input. If a format must carry types, the types come from an allowlist.
- XML: disable DTD processing and external entities entirely. Prefer JSON.
- Objects from input:
  - build with `Object.create(null)` or `Map`;
  - reject the keys `__proto__`, `constructor` and `prototype`;
  - validate against a schema that rejects unknown keys;
  - use maintained merge libraries.
- Regex: never compile input. Escape it if it must be matched literally. Keep patterns linear (no nested quantifiers over the same characters), cap input length before matching, or use a linear-time engine (RE2).
- Scalars stay scalars: the schema rejects an array or object where a string or number is expected.
- Body size limit, JSON depth limit, and a timeout on parsing workers.

## Negative proof

| Hostile input | Expected |
|---|---|
| `{"__proto__": {"isAdmin": true}}` merged into settings | then `({}).isAdmin === undefined`; request rejected or key dropped |
| `{"constructor": {"prototype": {"polluted": 1}}}` | same check, `({}).polluted === undefined` |
| `<?xml version="1.0"?><!DOCTYPE d [<!ENTITY e SYSTEM "file:///etc/passwd">]><d>&e;</d>` | rejected, or entity not expanded (no file content in output) |
| nested entity expansion ("billion laughs") | rejected quickly, memory flat |
| `'a'.repeat(50000) + '!'` against each validation regex | returns within a fixed budget (e.g. 50 ms) |
| `?role=user&role=admin` | 400, or the documented single value, never an array reaching the code |
| `?filter[$ne]=x` | 400 |
| 10 MB body on an endpoint expecting 1 KB | 413 before parsing |
| JSON nested 10,000 levels | 400 |
| a pickled / serialized object in a cookie or message | refused by format, never loaded |

## Dynamic proof

Not provable in the lab: the passive baseline sends no hostile documents and times no responses. The negative tests above are the proof.

## Review signal

- A deserializer from the list, or a parser constructed with default options in a language whose defaults allow DTDs.
- A recursive merge, or a bracket assignment whose key comes from input.
- `new RegExp` with a variable, or a nested quantifier in a validation regex.
