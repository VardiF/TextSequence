# Contributing to TextSequence

TextSequence is feature-frozen for the v0.1.2 Build Week release. Small fixes,
documentation improvements, focused tests, and reliability work are welcome.

Before opening a change:

1. Read `AGENTS.md` and preserve the canonical integer-frame timeline rules.
2. Keep media external; do not commit media, projects, runtime output, or
   credentials.
3. Run `make test`, `npm run build` from `frontend/`, and `git diff --check`.
4. Explain any user-visible behavior change and include a focused regression
   test when practical.

Please keep pull requests narrow and avoid introducing cloud services,
databases, background workers, or new product features without prior scope
approval.
