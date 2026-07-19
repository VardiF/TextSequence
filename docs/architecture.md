# TextSequence Architecture

TextSequence is a local-first editor whose persisted project JSON is the
canonical source of truth. Timeline positions are integer frames at a rational
frame rate; source-out positions are exclusive.

```mermaid
flowchart TD
    Human[Human editor] --> React[React GUI]
    Agent[External MCP client] --> MCP[MCP adapter]
    React --> Service[ProjectService]
    MCP --> Service
    Service --> Domain[Timeline domain operations]
    Service --> Store[Atomic ProjectStore]
    Store --> JSON[Canonical project JSON]
```

## Boundaries

- **Canonical Project State:** `Project`, `Asset`, `Track`, and `Clip` models
  are serialized to validated schema-v2 JSON with stable opaque IDs. The
  canonical shape is `Project.timeline.tracks`; top-level REST `tracks` is only
  a compatibility alias.
- **Timeline Markers:** typed absolute-frame point and range markers are owned
  by `Project.timeline.markers`, sorted by start, effective end, and ID. They
  use generic production references only; marker mutations share the same
  domain/service/commit path as clip edits.
- **Timeline Domain Operations:** framework-independent split, delete, move,
  trim, and silence-removal operations validate frame bounds and collisions.
- **ProjectService:** loads authoritative state, clones it, applies a domain
  transformation, checks expected revisions, allocates the next integer
  revision and immutable `revision_id`, and commits one authoritative result.
- **Revision Store:** new projects use `projects/{id}/head.json` plus immutable
  full snapshots in `revisions/`. Legacy flat v1 files are migrated in memory
  and promoted only on the first successful mutation; the original is retained
  as `legacy-v1.json`. Each revision carries a deterministic UTF-8 integrity
  digest over its immutable chain and audit metadata and canonical snapshot; validation
  runs before reachable-parent chain walking. This is tamper detection, not a
  cryptographic signature against an attacker who can rewrite both record and
  digest.
- **REST Adapter:** provides browser-facing project, media, editing, render,
  and silence endpoints without duplicating domain rules.
- **MCP Adapter:** exposes the same service through Streamable HTTP. It returns
  safe projections and never exposes source paths through timeline inspection.
  v0.3.0 adds stateless `prepare_transaction` and `commit_transaction` tools;
  v0.2.3 added the typed `query_timeline` and `diff_revisions` tools; eight
  read-only Resources and REST reads reuse the same safe read boundaries.
- **Read Surface:** project, timeline, asset, clip, marker, and revision
  projections are transport-neutral. Revision reads walk only the chain
  reachable from HEAD and never promote legacy flat projects or expose raw
  snapshots.
- **Revision Diff:** resolves both explicit revision IDs from one validated
  HEAD-reachable history load, then calls a pure explicit-field comparator. It
  never compares raw snapshot dictionaries, mutates persistence, or emits
  source locations.
- **Transactions:** preparation parses a strict seven-operation union, resolves
  stable IDs and earlier `result_ref` bindings, allocates deterministic IDs,
  and runs the domain operations on a deep copy. It returns the same pure
  `diff_projects` dry-run surface used by commit. Commit reloads the base under
  the existing per-project lock, re-executes the prepared operations, checks
  the base revision and revision ID, and writes exactly one `transaction`
  revision. There is no transaction database, reservation, rebase, or schema
  migration; a SHA-256 transaction hash is an integrity check rather than
  authentication.
- **FFmpeg Render Plan:** compiles canonical clips and gaps into a deterministic
  local FFmpeg command for preview or export.

Marker state remains in schema v2 and participates in immutable snapshots,
revision integrity digests, HEAD validation, parent-chain validation, and
restart/reload. A v0.2.0 binary cannot open a marker-bearing v0.2.1 project,
because v0.2.0 deliberately rejected nonempty marker collections; empty-marker
v0.2.0 projects and v0.1.x migrations remain compatible.
- **Managed Browser Media:** multipart uploads are written atomically under the
  ignored local `media/{project_id}` root, probed, and then imported through the
  same revision-checked service path. Failed probes or stale commits remove the
  managed copy where possible; path imports remain an advanced external-
  reference fallback.
- **Silence Analyzer:** runs local `ffprobe`/FFmpeg `silencedetect`, converts
  timestamps to integer frames, and separates read-only analysis from the
  revision-checked batch mutation.
- **React Polling:** polls the open project revision and adopts external MCP
  edits unless a local trim or move gesture is active.
- **Optional Built-in Agent:** uses the OpenAI Agents SDK as one optional MCP
  client; it is not required for core functionality.

```mermaid
sequenceDiagram
    participant H as Human / React
    participant A as External agent
    participant S as ProjectService
    participant P as Canonical JSON
    participant F as Local FFmpeg
    H->>S: Import or edit with expected revision
    A->>S: Inspect or mutate through MCP
    S->>P: Load HEAD, validate, commit immutable snapshot and HEAD
    S->>F: Compile and render requested revision
    S-->>H: Authoritative project / render result
    S-->>A: Safe projection / mutation result
```

The key design rule is convergence: GUI and MCP calls share the same service,
domain operations, revision checks, and persisted source of truth.
