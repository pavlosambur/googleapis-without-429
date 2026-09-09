# googleapis-without-429

[![CI](https://github.com/pavlosambur/googleapis-without-429/actions/workflows/ci.yml/badge.svg)](https://github.com/pavlosambur/googleapis-without-429/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://github.com/pavlosambur/googleapis-without-429)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Drop-in transport for Google API clients that stays inside the quota instead of
recovering from `429 Too many requests`.

> **Status: work in progress.** Nothing is released yet. The first release
> covers the Google Sheets and Drive APIs through a `requests`-based session,
> which together are what [gspread](https://github.com/burnash/gspread) uses.

## Development

```bash
uv sync                # install the project and its dev dependencies
make check             # everything CI runs: lint, format, types, tests
uv run pre-commit install   # optional: run the fast checks on every commit
```

Individual steps: `make lint`, `make check-format`, `make typecheck`,
`make test`. CI calls the same targets, so a green local run means a green
pipeline.

The test suite needs no credentials and makes no network calls.

## License

MIT
