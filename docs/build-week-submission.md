# Build Week Submission Copy

## Project name

TextSequence

## Tagline

A lightweight, open-source, MCP-native NLE where humans and AI agents edit the
same timeline.

## Description

TextSequence is an experimental local-first video editor built around a
canonical integer-frame timeline. A human can import, split, trim, move, delete,
preview, and export video in a lightweight browser UI. External MCP clients can
inspect and mutate that exact same timeline through revision-safe tools.

## Problem

Modern professional editors can be inaccessible on older or lower-spec
hardware, while agent integrations often operate beside an editor instead of
sharing its authoritative state. That makes automation opaque and difficult to
trust.

## Solution

TextSequence keeps media and project state local, uses deterministic FFmpeg
rendering, and exposes the timeline through MCP. Silence analysis is local and
frame-based: an agent can request analysis, review the ranges, and ask the
editor to apply one revision-checked batch edit.

## OpenAI ecosystem / MCP

The product exposes a real Streamable HTTP MCP server with 11 tools. It can be
connected to compatible external clients, including Codex CLI registration.
An optional built-in OpenAI assistant is available when configured, but core
editing and MCP workflows require no `OPENAI_API_KEY`.

## Technical novelty

The GUI, REST API, MCP adapter, and optional assistant all converge on the same
framework-independent ProjectService and domain operations. Agents decide what
to request; TextSequence validates IDs, frames, collisions, revisions, and
persistence deterministically.

## Works today

- Local CFR H.264/AAC media import and streaming
- Integer-frame V1 timeline with split, trim, move, and delete
- Render Preview and MP4 export
- 11 MCP tools with revision-safe mutations
- Deterministic local silence analysis and removal
- Human and external MCP co-editing through canonical JSON

## Known limitations

TextSequence is single-source-asset and V1-only, does not provide real-time
composited timeline playback, and renders synchronously. It does not include
transcription, semantic video understanding, best-take selection, transitions,
or effects. The local MCP endpoint is unauthenticated and intended for
localhost use.

## Future direction

Possible future work includes transcript-aware editing, multi-track timelines,
local-model integrations, richer MCP editing primitives, B-roll workflows, and
proxy support. No dates are promised.
