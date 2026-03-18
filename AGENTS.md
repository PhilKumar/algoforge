# Repo Workflow

- `main` is the base branch for shared product behavior.
- Any bug fix or engine improvement that is applied on `main` and also matters to the multi-tenant system must be ported to `feature/multi-tenant` unless the user explicitly says not to.
- Prefer `main` first, then sync the same change to `feature/multi-tenant` by cherry-pick, merge, or manual port depending on conflicts.
- When working on `feature/multi-tenant`, preserve parity with `main` for shared backtest, paper, live, indicator, and UI behavior wherever possible.
