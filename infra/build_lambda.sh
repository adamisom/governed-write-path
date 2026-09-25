#!/usr/bin/env bash
# Build the Lambda package for arm64 Python 3.12. Not run in the spike.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf infra/build && mkdir -p infra/build/pkg
uv export --no-dev --no-hashes --format requirements-txt > infra/build/requirements.txt
uv pip install --target infra/build/pkg --python-platform aarch64-manylinux2014 --python-version 3.12 \
  -r infra/build/requirements.txt
cp -r src/gwp infra/build/pkg/
(cd infra/build/pkg && zip -qr ../lambda.zip .)
echo "infra/build/lambda.zip"
