# carbon-api

Backend for Carbon, the plagiarism checker. Python 3.12, managed with [uv](https://docs.astral.sh/uv/).

## Layout

```
src/carbon/
  api/        FastAPI app (API Lambda)
  worker/     SQS worker (Worker Lambda)
  core/       detection cascade — pure Python, no AWS imports
  ingest/     input extraction
  discovery/  candidate source discovery
  layout/     page layout / screenshots
  infra/      AWS adapters (DynamoDB, S3, SQS)
tests/
```

## Commands

```bash
uv sync                          # install deps (incl. dev group)
uv run pytest                    # run tests
uv run ruff check .              # lint
uv run ruff format .             # format
uv run pre-commit install        # enable git pre-commit hook (ruff check + format)
```
