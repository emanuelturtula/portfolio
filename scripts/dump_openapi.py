"""Write the backend's OpenAPI schema to a file.

The frontend's TypeScript API types are generated from this schema, and CI regenerates them
on every pull request and fails if the result differs from what is committed. That turns a
backend contract change that nobody propagated to the frontend into a failing check on the
pull request that caused it, rather than a runtime surprise weeks later.

The schema itself is not committed: it is a build artifact, and the generated types are the
thing worth reviewing in a diff.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "openapi.json",
        help="where to write the schema (default: openapi.json at the repository root)",
    )
    args = parser.parse_args(argv)

    sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))
    from portfolio.main import create_app

    schema = create_app().openapi()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys so the output is stable across runs; otherwise the drift check would be
    # comparing dictionary ordering rather than the contract.
    args.output.write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
