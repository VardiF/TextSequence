# TextSequence MCP clients

TextSequence is an MCP-native local NLE. It provides the editor, canonical
JSON project store, and MCP server. An MCP-capable client supplies the natural
language or automation layer.

## Endpoint

- Transport: Streamable HTTP
- Default URL: `http://127.0.0.1:8000/mcp`
- Intended use: localhost development/MVP; the endpoint is unauthenticated.

Start TextSequence with `make backend`, then connect the client to the URL.
The MCP endpoint remains available without `OPENAI_API_KEY`.

## Available tools

The current server exposes exactly these 15 tools:

### `list_projects()`

Returns safe project summaries ordered deterministically:

`project_id`, `name`, `revision`, `revision_id`, `timeline_id`, rational `fps`,
and `clip_count`.

### `get_timeline(project_id)`

Returns the safe timeline projection: project metadata, tracks, stable clip
IDs, deterministic clip ordinals, asset IDs and display names, integer
`source_in_frame`, exclusive `source_out_frame`, duration, timeline start/end,
deterministic gap ordinals, sorted markers, `content_end_frame`, and
`display_end_frame`. It does not return source filesystem paths.

### `get_editor_context(editor_session_id)`

Returns the most recent context submitted by an open TextSequence browser tab:

`active_project_id`, observed and current revisions, selected clip ID and
validity, playhead frame, visible track ID, and capture time. It is for
resolving “this”, “selected clip”, and “here”. An independent MCP client
should use explicit `get_timeline` references instead.

Possible errors include `EDITOR_CONTEXT_MISSING`, `PROJECT_NOT_FOUND`, and
`INVALID_SELECTION`.

### `add_marker(project_id, expected_revision, start_frame, name, end_frame=null, description="", type="generic", production={...})`

Adds a server-generated point or exclusive range marker. The marker is
absolute timeline metadata and creates exactly one revision.

### `update_marker(project_id, marker_id, expected_revision, changes)`

Applies a partial marker update. Omitted fields remain unchanged; supplying
`end_frame: null` converts a range to a point. The marker ID cannot change,
and an empty update returns `NO_CHANGES`.

### `delete_marker(project_id, marker_id, expected_revision)`

Deletes one marker and returns `deleted_marker_id` plus the authoritative
timeline projection. Missing markers return `MARKER_NOT_FOUND`.

### `analyze_silence(project_id, minimum_silence_ms=700, noise_threshold_db=-35)`

Runs local FFmpeg `silencedetect` analysis without changing the project. It
returns deterministic integer-frame silence ranges and a summary. Analysis
requires a present asset with an audio stream.

### `remove_silence(project_id, expected_revision, minimum_silence_ms=700, noise_threshold_db=-35, keep_padding_ms=0)`

Re-analyzes current media, intersects detected source ranges with current
trimmed/moved clips, and applies one revision-checked batch mutation. Affected
content is compacted without ripple trimming. If the project changed during
analysis, it returns `STALE_REVISION` and makes no edit.

### `split_clip(project_id, clip_id, timeline_frame, expected_revision)`

Splits a clip strictly inside its timeline range. The revision must match the
current canonical project revision.

### `delete_clip(project_id, clip_id, expected_revision)`

Deletes a stable clip ID using revision protection.

### `move_clip(project_id, clip_id, expected_revision, destination)`

`destination` is either:

- `{ "kind": "timeline_frame", "timeline_start_frame": N }`
- `{ "kind": "gap", "gap_ordinal": N, "alignment": "start" }`

The server validates non-overlap and resolves gap destinations deterministically.

### `trim_clip(project_id, clip_id, expected_revision, edge, frames_to_remove)`

Relative trim only. `edge` is `start` or `end`; `frames_to_remove` must be a
positive integer. No ripple trimming is performed.

### `render_preview(project_id, expected_revision)`

Renders the current revision through the existing local FFmpeg renderer and
returns safe render metadata and an application URL.

### `export_project(project_id, expected_revision)`

Exports the current revision through the same renderer and returns safe render
metadata and an application URL.

### `query_timeline(project_id, query)`

Returns safe clip and marker projections matching the typed query. Use
`entity_types` with one or both of `clip` and `marker`, and at least one
substantive predicate such as `frame`, `frame_range`, `asset_id`, `marker_type`,
`shot_id`, `dialogue_line_id`, or `external_ref`. `frame` and `frame_range` are
mutually exclusive. Clips match a containing frame or overlapping half-open
range; markers use their point or half-open range semantics. Results are
deterministically ordered and contain no source filesystem paths.

## Read-only Resources

The server exposes exactly eight read-only JSON Resources:

- `textsequence://projects`
- `textsequence://projects/{project_id}`
- `textsequence://projects/{project_id}/timeline`
- `textsequence://projects/{project_id}/assets/{asset_id}`
- `textsequence://projects/{project_id}/clips/{clip_id}`
- `textsequence://projects/{project_id}/markers/{marker_id}`
- `textsequence://projects/{project_id}/revisions`
- `textsequence://projects/{project_id}/revisions/{revision_id}`

Each returns a safe JSON envelope with a resource type, canonical URI, state
metadata, and data projection. Revision resources expose only snapshots
reachable from current HEAD; legacy flat projects report unavailable history
until a normal mutation promotes them. Resource URIs must be exact and may not
contain query strings, fragments, trailing slashes, traversal segments, or
encoded separators.

The equivalent REST read routes are `GET /api/projects/{project_id}/timeline`,
`POST /api/projects/{project_id}/timeline/query`,
`GET /api/projects/{project_id}/revisions`, and
`GET /api/projects/{project_id}/revisions/{revision_id}`.

## Recommended client workflow

Follow **INSPECT → RESOLVE → MUTATE**:

1. Call `list_projects` when the project ID is unknown.
2. Call `get_timeline`.
3. Resolve stable IDs, deterministic ordinals/gaps, and the current revision.
4. Call the mutation with `expected_revision`.
5. Re-inspect after a `STALE_REVISION` response and retry only after resolving
   the original request against current state.

The REST GUI and MCP tools use the same `ProjectService`, domain operations,
per-project lock, and atomic project persistence. React observes MCP edits by
polling the canonical revision.

## Silence-removal workflow

Call `analyze_silence` first when presenting a proposed edit, then call
`remove_silence` with the revision observed from `get_timeline`. Both tools use
local FFmpeg only; no media is uploaded and no semantic transcription or
speech understanding is implied. Defaults are a 700 ms minimum, -35 dB
threshold, and no padding. Optional padding is retained around each detected
range where possible.

## Codex CLI setup

The installed Codex CLI supports external Streamable HTTP MCP server
registrations. A non-destructive manual registration command is:

```sh
codex mcp add textsequence --url http://127.0.0.1:8000/mcp
```

This changes the user's Codex configuration, so it is intentionally not run by
the TextSequence setup. Remove it later with:

```sh
codex mcp remove textsequence
```

Direct Codex → TextSequence tool calls were not executed in this environment;
the installed CLI's command/help surface proves the supported URL registration
syntax, but no claim is made here about a completed Codex model session.
