"""Opt-in local smoke path; never run as part of the normal test suite."""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--selected-clip-id")
    parser.add_argument("--playhead-frame", type=int, default=0)
    args = parser.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is required for the live agent smoke path.", file=sys.stderr)
        return 2
    session_id = "editor_live_smoke"
    project = httpx.get(f"http://127.0.0.1:8000/api/projects/{args.project_id}", timeout=15).json()
    response = httpx.post("http://127.0.0.1:8000/api/agent/chat", json={
        "editor_session_id": session_id,
        "message": args.message,
        "editor_context": {
            "editor_session_id": session_id,
            "project_id": args.project_id,
            "observed_revision": project["revision"],
            "selected_clip_id": args.selected_clip_id,
            "playhead_frame": args.playhead_frame,
            "visible_track_id": project.get("tracks", [{}])[0].get("id"),
        },
    }, timeout=120)
    print(json.dumps(response.json(), indent=2))
    return 0 if response.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
