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

## Configuration

Settings live in `carbon/settings.py` and are read from `CARBON_*` environment variables or a
`.env` file. Copy `.env.example` to `.env` to start; every key has a safe local default. Inject
settings with `Depends(get_settings)`.

## API contract

Request, job and result models live in `carbon/api/schemas.py`. After changing them, regenerate
the OpenAPI document (a test fails while it is stale) and the TypeScript types for the frontend:

```bash
uv run python scripts/export_openapi.py                          # → openapi.json
npx openapi-typescript@7.4.4 openapi.json -o clients/carbon-api.d.ts
```

`tests/fixtures/example_result.json` is a complete example result, matching the frontend mock data.
