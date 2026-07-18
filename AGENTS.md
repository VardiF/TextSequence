# Project Invariants

- The persisted project JSON is the canonical source of truth for timeline state.
- Canonical timeline positions use integer frames. Frame rates are rational `{numerator, denominator}` values; floating-point seconds exist only at media/UI boundaries.
- GUI actions, API endpoints, and future MCP tools must call the same framework-independent Python timeline operations. Do not duplicate editing rules in React, FastAPI routes, or MCP handlers.
- Keep media probing, domain models and operations, persistence, API transport, and frontend presentation as separate boundaries.
- Every project, asset, track, and clip has a stable opaque ID. Clip source-out positions are exclusive.
- Keep the application local-first and lightweight. Do not add databases, cloud services, background workers, desktop shells, or heavyweight state frameworks without a demonstrated requirement.
- Treat imported media as external source files; never modify it. Persist project files atomically and validate data at load and operation boundaries.
- Prefer the smallest end-to-end change that preserves these invariants, and add focused tests for domain behavior and serialization.
