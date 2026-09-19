"""Run the Hotel composition against the explicitly resolved evaluator package."""

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", required=True, type=Path)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    with tempfile.TemporaryDirectory(prefix="vibesys-hotel-module-") as directory:
        modfile = Path(directory) / "runtime.mod"
        shutil.copyfile(root / "runtime.mod", modfile)
        shutil.copyfile(root / "runtime.sum", modfile.with_suffix(".sum"))
        subprocess.run(
            [
                "go",
                "-C",
                str(root),
                "mod",
                "edit",
                f"-modfile={modfile}",
                f"-replace=vibesys/microservice-evaluator={args.package_root.resolve()}",
            ],
            check=True,
        )
        return subprocess.run(
            [
                "go",
                "-C",
                str(root),
                "run",
                f"-modfile={modfile}",
                "./cmd/hotel-correctness",
                *arguments,
            ],
            check=False,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
