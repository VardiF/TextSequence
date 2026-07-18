# Reproducible Build Week Demo Workflow

The repository's test fixture is a tiny deterministic CFR 24fps H.264/AAC MP4:
one second of tone, one second of silence, one second of tone, half a second
of silence, and one second of tone. At the default 700ms threshold, analysis
detects only frames 24–48; removal shortens the 108-frame source to an
84-frame render. The fixture is generated under pytest's temporary directory
and is not committed.

For a local recording, use any known CFR H.264/AAC MP4 with deliberate pauses,
or generate an equivalent file with FFmpeg's `testsrc`, `sine`, and `anullsrc`
filters. Start the backend and frontend, create a project, and import the
absolute path through the UI. Use the MCP endpoint at
`http://127.0.0.1:8000/mcp` for `get_timeline`, `analyze_silence`,
`remove_silence`, and `render_preview`. Finish by exporting MP4 from the UI.

The demo should visibly show both sides of the architecture:

- Human: import, select, split or trim, preview, and export.
- Agent: inspect the current revision, analyze silence, remove silence, and
  render through the same ProjectService.
