# Contributing to Weave

By submitting a pull request, you agree that your contributions are licensed under the Apache License 2.0.

## Development Setup

```bash
pip install -r requirements.txt
python -m pytest -v --tb=short
flake8 --max-line-length=100
```

## Pull Request Process

1. Fork the repository
2. Create a feature branch (`git checkout -b feat/my-feature`)
3. Commit your changes with conventional commit messages
4. Ensure tests pass (`python -m pytest`)
5. Open a pull request against `main`
