---
name: engineering-csharp
description: "**Opt-in for C#:** load when C# is the implementation language. .NET ecosystem (ASP.NET Core, Minimal APIs), build tool (dotnet CLI), ORM (Entity Framework Core, Dapper), test runner (xUnit), and idiomatic patterns for layering, DDD, and concurrency in C#."
version: 1.0.1
user-invocable: false
---

<!-- RULE-ANCHOR -->
Rules in this skill are **non-negotiable** and active for the entire session.
Read this skill completely before acting. No partial reads.
Every action must trace to: (a) the user's request, including the decisions it delegates to you, (b) code read in the repo, or (c) a rule in a loaded skill. Anything else is a hypothesis: a decision you do not own, asked of the user (Decision ownership in `engineering-governance`).
<!-- /RULE-ANCHOR -->


# C#

## Runtime and package manager

- Package manager: NuGet (`dotnet add package`)
- Version manager: `global.json` pins the SDK, optional
- Lockfile: `packages.lock.json`
- Build tool: `dotnet CLI` (`dotnet build`)

## Frameworks

| Framework | When to pick |
|---|---|
| ASP.NET Core | Default for production backend services; mature, cross-platform, with built-in DI, config, logging, and EF Core integration |
| Minimal APIs | Lightweight REST APIs on .NET 6+; use when controller ceremony is not needed |
| Orleans | Distributed actor framework; use only when the workload needs stateful actors and cluster infrastructure |

## Database access

| Layer | Options |
|---|---|
| ORM | Entity Framework Core (default), Dapper |
| Raw SQL driver | Microsoft.Data.SqlClient, Npgsql, MySqlConnector |
| Migration tool | EF Core Migrations, FluentMigrator |

## Test runner

- Primary: xUnit (alternatives: NUnit, MSTest)
- Test layout: `*.Tests` projects beside source projects; separate unit and integration projects
- Coverage: coverlet + ReportGenerator
- Mocking: Moq, NSubstitute, or hand-rolled test doubles

## Linting and formatting

- Linter: Roslyn analyzers (StyleCop, .NET built-in IDE rules)
- Formatter: `dotnet format`
- Type checker: built into `dotnet build`

## Idiomatic patterns

#### Value objects

Use a `record` for immutable values with value equality and constructor validation:

```csharp
public sealed record Email
{
    public string Value { get; }

    public Email(string value)
    {
        if (string.IsNullOrWhiteSpace(value) || !value.Contains('@'))
            throw new ArgumentException("Invalid email", nameof(value));

        Value = value;
    }
}
```

#### Entities

Keep entities behavior-rich with a private constructor plus a static factory:

```csharp
public sealed class User
{
    public Guid Id { get; private set; }
    public Email Email { get; private set; }

    private User() { }

    private User(Guid id, Email email) => (Id, Email) = (id, email);

    public static User Create(Email email) => new(Guid.NewGuid(), email);
}
```

#### Dependency injection

Use the built-in `IServiceCollection` and constructor injection. Ports are C# interfaces; register adapters in the composition root:

```csharp
public sealed class CreateUser(IUserRepository users)
{
    public async Task<User> ExecuteAsync(Guid id, CancellationToken ct)
    {
        var user = await users.FindByIdAsync(id, ct);
        return user ?? throw new UserNotFoundException(id);
    }
}
```

#### Async / concurrency

Use `Task<T>`/`ValueTask<T>` with `async`/`await`, `IAsyncEnumerable<T>` for streams, and `Task.WhenAll` for parallel work. Avoid `async void`:

```csharp
public async Task ProcessAllAsync(
    IReadOnlyCollection<Item> items,
    CancellationToken ct)
{
    await Task.WhenAll(items.Select(item => ProcessAsync(item, ct)));
}
```

## Layer boundaries

Use projects or namespaces to enforce layer boundaries: `Domain` stays free of framework and persistence dependencies, `Application` depends on domain interfaces, `Infrastructure` implements ports with EF Core or Dapper, and the presentation/composition root registers dependencies through `IServiceCollection`. Ports are C# `interface`s, value objects and entities live in `Domain`, and persistence models in `Infrastructure` map back to domain entities. Placement and dependency rules are owned by `engineering-arch-hexagonal`, `engineering-arch-modular`, and `engineering-arch-mvc`; this section only adds C# placement notes.

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
