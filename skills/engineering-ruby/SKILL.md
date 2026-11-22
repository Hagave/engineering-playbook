---
name: engineering-ruby
description: "**Opt-in for Ruby:** load when Ruby is the implementation language. Ruby ecosystem (Rails, Sinatra, Hanami), ORM (ActiveRecord, Sequel), test runner (RSpec, Minitest), and idiomatic patterns for layering, DDD, and concurrency in Ruby."
version: 1.0.1
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Ruby

## Runtime and package manager

- Package manager: Bundler
- Version manager: rbenv or asdf (optional)
- Lockfile: `Gemfile.lock`
- Build tool: Rake (when applicable)

## Frameworks

| Framework | When to pick |
|---|---|
| Ruby on Rails | Default for production backend Ruby; full-stack, opinionated, ActiveRecord included |
| Sinatra | Lightweight DSL microframework for APIs and small services |
| Hanami | Modular framework with dry-rb conventions for hexagonal-style apps |

## Database access

| Layer | Options |
|---|---|
| ORM | ActiveRecord (Rails default), Sequel |
| Raw SQL driver | pg, mysql2, sqlite3 |
| Migration tool | Rails migrations (`rails db:migrate`), Sequel migrations (`rake db:migrate`) |

## Test runner

- Primary: RSpec (Minitest when following Rails defaults)
- Test layout: `spec/` (RSpec) or `test/` (Minitest)
- Coverage: SimpleCov
- Mocking: RSpec mocks / `rspec-mocks`; WebMock for HTTP stubbing

## Linting and formatting

- Linter: RuboCop
- Formatter: RuboCop (`rubocop -A`) or Standard
- Type checker: Sorbet (optional) or RBS (built-in, optional)

## Idiomatic patterns

#### Value objects

Use `Data.define` for immutable values with value equality:

```ruby
# frozen_string_literal: true

Money = Data.define(:amount, :currency) do
  def formatted
    format("%.2f %s", amount, currency)
  end
end
```

#### Entities

Keep entities behavior-rich with a private constructor and a semantic factory:

```ruby
class User
  attr_reader :id, :email
  def self.create(id:, email:)
    raise ArgumentError, "invalid email" unless email.include?("@")
    new(id:, email:)
  end
  private_class_method :new
  def initialize(id:, email:)
    @id = id
    @email = email
  end
end
```

#### Dependency injection

Inject dependencies through the constructor using duck typing; no DI container is required:

```ruby
class CreateOrder
  def initialize(repository:, notifier:)
    @repository = repository
    @notifier = notifier
  end
  def call(order)
    @notifier.deliver(@repository.save(order))
  end
end
```

#### Async / concurrency

Use `Concurrent::Promise` for in-process parallel work; use ActiveJob for background work:

```ruby
promises = ids.map { |id| Concurrent::Promise.new { fetch(id) } }
results = promises.map(&:execute).map(&:value)

OrderProcessedJob.perform_later(order.id)
```

## Layer boundaries

Ruby uses conventional folders over enforced interfaces. Keep domain objects and use cases framework-free under `app/domain` or `lib/`, keep Rails or Sinatra controllers as thin presentation, and isolate persistence by wrapping ActiveRecord or Sequel in repository objects. Load `engineering-arch-hexagonal`, `engineering-arch-modular`, or `engineering-arch-mvc` for the canonical tree and dependency rules; this section only adds Ruby placement notes.

## Cross-references

- DDD patterns: engineering-ddd
- Layer rules: engineering-architecture
- Hexagonal structure: engineering-arch-hexagonal
- Modular monolith: engineering-arch-modular
- MVC: engineering-arch-mvc
- Concurrency: engineering-concurrency
- Error handling: engineering-error-handling
- Testing: engineering-testing
- Performance: engineering-performance

<!-- OUTPUT-DISCIPLINE -->
No preamble. No ceremony. No restating context already confirmed.
Implement exactly what was specified — nothing more, nothing less.
A decision you do not own: ask, never assume. One the request delegates to you: decide and record it.
<!-- /OUTPUT-DISCIPLINE -->
