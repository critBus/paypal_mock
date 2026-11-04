"""Initialize or synchronize this checkout's optional CodeGraph index."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def update_index(
    project_root: Path, *, executable: str = "codegraph", strict: bool = False
) -> int:
    """Keep Git non-blocking while allowing explicit readiness checks to fail."""
    root = project_root.resolve()
    if not (root / "app.py").is_file() or not (root / "pyproject.toml").is_file():
        sys.stderr.write(f"Not a mock-paypal-api checkout: {root}\n")
        return 1
    command = shutil.which(executable)
    if command is None:
        sys.stderr.write(
            "CodeGraph is unavailable on PATH; use direct source reads until installed.\n"
        )
        return 1 if strict else 0
    action = "sync" if (root / ".codegraph/codegraph.db").is_file() else "init"
    arguments = [command, action, str(root)]
    if action == "init":
        arguments.append("--yes")
    try:
        result = subprocess.run(arguments, cwd=root, check=False)  # noqa: S603
    except OSError as error:
        sys.stderr.write(f"CodeGraph could not start: {error}\n")
        return 1 if strict else 0
    if result.returncode:
        sys.stderr.write(
            "CodeGraph update failed; use direct reads until the index is healthy.\n"
        )
    return result.returncode if strict else 0


def main() -> int:
    """Resolve the repository from this module, including when called outside its root."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--executable", default="codegraph")
    arguments = parser.parse_args()
    return update_index(
        Path(__file__).resolve().parents[1],
        executable=arguments.executable,
        strict=arguments.strict,
    )


if __name__ == "__main__":
    raise SystemExit(main())
