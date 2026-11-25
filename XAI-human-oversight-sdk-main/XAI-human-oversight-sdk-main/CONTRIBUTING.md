# Contributing

## Development setup

Use Python 3.11 or later, then run `pip install -e ".[dev]"` from this directory.

## Checks

Run `pytest`, `ruff check .`, and `ruff format --check .` before submitting changes. Tests must mock HTTP and must not require a live service. Keep backend communication in `transport/`; do not add frontend or backend implementation here.

## Design rules

- Preserve sync and async parity for public workflows.
- Validate backend responses and fail closed on unknown or malformed authorization data.
- Never log API credentials, action parameters, or raw backend response bodies.
- Update the proposed API contract and security documentation when changing wire behavior.

