# Contributing to Weave

Thank you for your interest in contributing to Weave! By submitting a pull request, you agree that your contributions are licensed under the [Apache License 2.0](LICENSE).

## Development Setup

### Prerequisites

- Python 3.11+
- Git

### Install Dependencies

```bash
git clone https://github.com/yaogang1991/weave.git
cd weave
pip install -r requirements.txt
```

### Verify Installation

```bash
python -m pytest -v --tb=short
flake8 --max-line-length=100
```

## Development Workflow

### Running Tests

```bash
# Run all tests
python -m pytest -v --tb=short

# Run with coverage
python -m pytest --cov=. --cov-report=term-missing

# Run only unit tests (exclude integration tests that call LLM APIs)
python -m pytest -v -m "not integration"

# Run integration tests (requires ANTHROPIC_API_KEY)
python -m pytest -v -m integration
```

### Linting

```bash
flake8 --max-line-length=100
```

### Project Structure

Weave uses a layered architecture. See [ARCHITECTURE.md](ARCHITECTURE.md) for the full component map.

Key conventions:
- **Type annotations**: Python 3.10+ syntax (`str | None`, `list[dict]`)
- **Data models**: Pydantic `BaseModel` in `core/*_models.py`, re-exported via `core/models.py`
- **No circular imports**: Layer by responsibility (`core/` -> `agent/` -> `orchestrator/` -> `tools/`)
- **State is externalized**: All runtime state in `./data/` (JSONL events, artifacts)

## Pull Request Process

1. **Fork** the repository
2. **Create a feature branch** from `main`:
   ```bash
   git checkout -b feat/my-feature
   ```
3. **Make your changes** with clear, conventional commit messages:
   - `feat: add new agent type`
   - `fix: handle empty DAG in executor`
   - `refactor: extract node execution from dag_engine`
   - `docs: update configuration reference`
4. **Ensure tests pass**:
   ```bash
   python -m pytest -v --tb=short
   flake8 --max-line-length=100
   ```
5. **Open a pull request** against `main` with:
   - A clear summary of changes
   - Test plan (manual or automated verification steps)
   - Reference to any related issues

## Adding New Functionality

### Adding a Tool

1. Register in `tools/registry.py`
2. Add risk level in `guardrails/policy.py` `RISK_MAP`
3. Add tests in `tests/`

### Adding an Agent Type

1. Add to `core/agent_registry.py` `_register_defaults()`
2. Update prompt in `agent/prompts.py`
3. Update orchestrator prompt in `orchestrator/prompts/planning.md`

### Adding a CLI Command

1. Add handler in appropriate `cli/` subdirectory
2. Register subparser in `main.py`

### Data Model Changes

1. Add to the appropriate `core/*_models.py` file
2. Re-export from `core/models.py`

## Reporting Issues

- Use [GitHub Issues](https://github.com/yaogang1991/weave/issues) to report bugs or request features
- Include reproduction steps, expected behavior, and actual behavior
- For bugs, include the relevant session log from `./data/events/`

## Code of Conduct

Be respectful and constructive. We're all here to build great software together.

## License

By contributing, you agree that your contributions will be licensed under the [Apache License 2.0](LICENSE).
