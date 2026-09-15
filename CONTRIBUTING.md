# Contributing

1. Create a focused branch from the default branch.
2. Keep business rules in `app/domain`; do not place financial decisions in prompts.
3. Add a meaningful test for behavior or risk changes.
4. Run `pytest`, `ruff check .`, and `mypy app` before opening a pull request.
5. Describe the customer-visible behavior and any security or migration impact.

