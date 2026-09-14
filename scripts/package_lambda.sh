#!/usr/bin/env bash
# ==============================================================================
# scripts/package_lambda.sh
# Packages WFM Sell-Timing Advisor and trimmed dependencies into build/function.zip
# for AWS Lambda deployment.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUILD_DIR="${ROOT_DIR}/build"
PACKAGE_DIR="${BUILD_DIR}/package"
ZIP_FILE="${BUILD_DIR}/function.zip"

echo "=== 1. Preparing clean build staging directory ==="
rm -rf "${PACKAGE_DIR}" "${ZIP_FILE}"
mkdir -p "${PACKAGE_DIR}"

echo "=== 2. Installing production dependencies for Linux x86_64 ==="
# Filter out pytest and dev packages
TMP_REQS="${BUILD_DIR}/prod_requirements.txt"
grep -v -E "pytest" "${ROOT_DIR}/requirements.txt" > "${TMP_REQS}"

# Use pip with binary platform targeting for AWS Lambda Linux x86_64
if pip --version &>/dev/null; then
    PIP_CMD="pip"
else
    PIP_CMD="python3 -m pip"
fi

${PIP_CMD} install \
    --platform manylinux2014_x86_64 \
    --target "${PACKAGE_DIR}" \
    --implementation cp \
    --python-version 3.12 \
    --only-binary=:all: \
    --upgrade \
    -r "${TMP_REQS}"

echo "=== 3. Copying application source files ==="
cp "${ROOT_DIR}/lambda_handler.py" "${PACKAGE_DIR}/"
cp "${ROOT_DIR}/main.py" "${PACKAGE_DIR}/"
cp "${ROOT_DIR}/run_pipeline.py" "${PACKAGE_DIR}/"

cp -R "${ROOT_DIR}/nodes" "${PACKAGE_DIR}/"
cp -R "${ROOT_DIR}/ingest" "${PACKAGE_DIR}/"
cp -R "${ROOT_DIR}/config" "${PACKAGE_DIR}/"

# Include schema.sql for fresh initialization
mkdir -p "${PACKAGE_DIR}/db"
cp "${ROOT_DIR}/db/schema.sql" "${PACKAGE_DIR}/db/"

echo "=== 4. Stripping non-essential files to fit under 250MB limit ==="
# Remove boto3/botocore/s3transfer as they are pre-installed in AWS Lambda runtime
rm -rf "${PACKAGE_DIR}"/boto3* "${PACKAGE_DIR}"/botocore* "${PACKAGE_DIR}"/s3transfer* 2>/dev/null || true
# Strip test suites from scipy and numpy (preserving numpy/_core/tests)
find "${PACKAGE_DIR}/scipy" -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true
rm -rf "${PACKAGE_DIR}/numpy/tests" 2>/dev/null || true
# Strip type stubs, documentation, dist-info, and bytecode
find "${PACKAGE_DIR}" -type d -name "*.dist-info" -exec rm -rf {} + 2>/dev/null || true
find "${PACKAGE_DIR}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "${PACKAGE_DIR}" -name "*.pyi" -delete 2>/dev/null || true
find "${PACKAGE_DIR}" -name "*.pyc" -delete 2>/dev/null || true
find "${PACKAGE_DIR}" -name "*.pyo" -delete 2>/dev/null || true
find "${PACKAGE_DIR}" -name "*.md" -delete 2>/dev/null || true
find "${PACKAGE_DIR}" -name "*.rst" -delete 2>/dev/null || true

echo "=== 5. Compressing into ${ZIP_FILE} ==="
cd "${PACKAGE_DIR}"
zip -r9 -q "${ZIP_FILE}" .

ZIP_SIZE_MB=$(du -m "${ZIP_FILE}" | cut -f1)
echo "=== Packaging complete! ==="
echo "Artifact: ${ZIP_FILE}"
echo "Size: ${ZIP_SIZE_MB} MB"

if [ "${ZIP_SIZE_MB}" -gt 50 ]; then
    echo "Notice: Package size (${ZIP_SIZE_MB}MB) exceeds 50MB direct CLI upload limit."
    echo "Deploy script will stage package through S3 bucket."
fi
