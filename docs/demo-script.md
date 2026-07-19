# TextSequence Build Week Demo Script

Target length: 60–90 seconds. Use the reproducible tone/silence fixture
described in `docs/demo-workflow.md`.

**0–10 seconds — pitch**

“TextSequence is a lightweight, open-source, MCP-native non-linear editor.
The human keeps the visual timeline, while agents get a safe tool surface for
inspection and deterministic edits.”

**10–25 seconds — import and manual edit**

“I create a project, import this local CFR MP4, and select the first clip. I’ll
make one small manual split so the canonical timeline has an observable human
edit.” Split the clip and show the selected state and playhead.

**25–40 seconds — MCP connection**

“The Agent Connections panel shows the local Streamable HTTP MCP endpoint and
14 available tools. An external MCP client calls `get_timeline`; it sees the
same project revision and clip IDs shown in the editor.”

**40–60 seconds — analyze and remove silence**

“Now the client calls `analyze_silence`. TextSequence runs FFmpeg locally and
returns integer-frame ranges; it does not invent timestamps. I review the
detected pause, then call `remove_silence` with the current revision.”

**60–75 seconds — deterministic result**

“The timeline becomes multiple compact clips, the revision advances once, and
the UI replaces its state with the authoritative project returned by the
service. This is the same timeline the human was editing.”

**75–90 seconds — render and close**

“I render a preview, play the shorter result, and export an MP4. TextSequence
turns video editing into an MCP-accessible tool surface without taking control
away from the human editor.”
