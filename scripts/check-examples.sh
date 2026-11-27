#!/usr/bin/env bash
# Check each example app, and each app the SDK's tests run, in its own environment,
# built by uv from its PEP 723 block: type-check app.py and its tests, then run
# `cliquet test`, which checks the app's definition, runs its tests and fails if an
# invariant never refused.
set -euo pipefail
cd "$(dirname "$0")/.."

# pytest isn't one of an app's dependencies: the type checker finds it, along with
# the rest of this repository's development environment, on an extra search path.
pytest_site="$(uv run --quiet python -c 'import pathlib, pytest; print(pathlib.Path(pytest.__file__).parent.parent)')"

for dir in examples/*/ packages/sdk/tests/apps/*/; do
  dir="${dir%/}"
  echo "== $dir"
  uv sync --quiet --script "$dir/app.py"
  python="$(uv python find --script "$dir/app.py")"
  uv run --quiet ty check --python "$python" --extra-search-path "$dir" \
    --extra-search-path "$pytest_site" "$dir/app.py" "$dir/tests"
  uv run --quiet cliquet test "$dir" -q
done
