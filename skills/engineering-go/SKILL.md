---
name: engineering-go
description: "**Opt-in for Go:** load when Go is the implementation language. Go ecosystem (net/http, Gin, Echo), modules, build tool (go build), database (sqlx, GORM, pgx), test runner (stdlib testing + testify), and idiomatic patterns for layering, DDD, and concurrency in Go."
version: 1.0.1
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# Go

## Runtime and package manager

- Package manager: Go modules (`go mod`)
- Version manager: optional; use the Go toolchain already installed
- Lockfile: `go.sum`
- Build tool: `go build`

## Frameworks

| Framework | When to pick |
|---|---|
| `net/http` (stdlib) | Default for most services; add `chi` or `echo` as a router only when middleware composition is needed |
| Gin | Most popular third-party framework; use when the team wants a mature router, middleware, and validation ecosystem |
| Echo | Minimalist and fast; use when Gin feels heavier than the service requires |

## Database access

| Layer | Options |
|---|---|
| ORM | GORM, ent |
| Raw SQL driver | sqlx, pgx |
| Migration tool | golang-migrate, goose |

## Test runner

- Primary: stdlib `testing` package via `go test`
- Test layout: `_test.go` files alongside `*.go` in the same package directory
- Coverage: `go test -cover` / `go test -coverprofile`
- Mocking: idiomatic stdlib interfaces; use `testify/mock` only for complex mocks
- Prefer table-driven tests with subtests (`t.Run`).

## Linting and formatting

- Linter: `golangci-lint` (aggregates `go vet`, `staticcheck`, and configured linters)
- Formatter: `gofmt` (mandatory) plus `goimports`
- Type checker: built into `go build`

## Idiomatic patterns

### Value objects

```go
type Email struct {
	value string
}

func NewEmail(value string) (Email, error) {
	if !strings.Contains(value, "@") {
		return Email{}, errors.New("invalid email")
	}
	return Email{value: value}, nil
}

func (e Email) String() string { return e.value }
```

### Entities

```go
type User struct {
	ID    string
	Name  string
	email Email
}

func NewUser(id, name string, email Email) (*User, error) {
	if name == "" {
		return nil, errors.New("name is required")
	}
	return &User{ID: id, Name: name, email: email}, nil
}

func (u *User) Rename(name string) { u.Name = name }
```

### Dependency injection

```go
type UserRepository interface {
	FindByID(ctx context.Context, id string) (*User, error)
}

type GetUser struct {
	users UserRepository
}

func NewGetUser(users UserRepository) *GetUser {
	return &GetUser{users: users}
}
```

### Async / concurrency

```go
g, ctx := errgroup.WithContext(ctx)
for _, item := range items {
	item := item
	g.Go(func() error {
		return process(ctx, item)
	})
}
if err := g.Wait(); err != nil {
	return err
}
```

Use goroutines and channels for streaming or one-to-one communication; use `context.Context` for cancellation and deadlines.

## Layer boundaries

Map architecture layers to package boundaries. Keep dependencies moving inward: domain packages import no adapters, application packages depend on domain interfaces, and infrastructure packages provide concrete adapters. Use `internal/` to keep unexported packages from leaking outside the module. In hexagonal projects, place ports in `domain/`, adapters in `infrastructure/`, and HTTP handlers in `presentation/`; generic placement rules live in `engineering-arch-hexagonal`, `engineering-arch-modular`, and `engineering-arch-mvc`.

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
