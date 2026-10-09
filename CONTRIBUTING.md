# Development

Use Python 3.11+ and the committed `uv.lock`:

```sh
uv sync --locked --extra stata --group dev
uv run --locked --extra stata --group dev python scripts/check.py
```

Work on `feat/*`, `fix/*`, `refactor/*`, or `docs/*` branches. Keep changes and
commits small; review staged diffs and the complete history before pushing.
Never commit credentials, assignment originals or their copies, downloaded
third-party datasets/code, runtime databases, or raw model logs. Use the existing
`.gitignore`; do not add everything with an unchecked `git add .`.

Unit tests and CI stay offline. Live probes are explicit commands, require local
Stata and/or model credentials, and must be reported separately. No model calls
or Stata initialization occur merely by importing the application or running
`status`/`export` when those commands become available. Use ordinary SDKs only;
the project's own code will own the research loop.

Open a draft Gitea PR early; distinguish self-review, independent review, remote
CI, real execution, and merge. Default delivery is a reviewable PR. Do not merge
without the applicable authorization or repository policy.
