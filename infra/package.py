"""Builds the Lambda code folder: the locked runtime dependencies as arm64 Linux wheels, plus the package."""

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
# The Lambda Python runtime already has these; leaving them out keeps the package small.
PROVIDED_BY_RUNTIME = ("boto3", "botocore", "s3transfer", "jmespath")


def build_lambda_package() -> Path:
    out = BUILD / "lambda"
    shutil.rmtree(BUILD, ignore_errors=True)
    out.mkdir(parents=True)
    requirements = BUILD / "requirements.txt"
    skip = [arg for name in PROVIDED_BY_RUNTIME for arg in ("--no-emit-package", name)]
    subprocess.run(
        ["uv", "export", "--frozen", "--no-dev", "--no-group", "infra", "--no-emit-project", "--no-hashes",
         *skip, "--output-file", str(requirements)],
        cwd=ROOT, check=True, capture_output=True,
    )  # fmt: skip
    subprocess.run(
        ["uv", "pip", "install", "--quiet", "--requirements", str(requirements), "--target", str(out),
         "--python-platform", "aarch64-manylinux2014", "--python-version", "3.13", "--only-binary", ":all:"],
        cwd=ROOT, check=True,
    )  # fmt: skip
    shutil.copytree(ROOT / "src" / "ledger_agent", out / "ledger_agent", ignore=shutil.ignore_patterns("__pycache__"))
    return out
