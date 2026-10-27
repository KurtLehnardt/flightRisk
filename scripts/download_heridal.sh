#!/usr/bin/env bash
# Download the HERIDAL SAR aerial dataset (Zenodo record 5662351, CC-BY-3.0,
# 8.3 GB, no login) and unpack it into datasets/heridal/.
#
# Usage: scripts/download_heridal.sh
# Environment: DATASETS_DIR overrides the target parent directory (default: datasets).
set -euo pipefail

URL="https://zenodo.org/records/5662351/files/heridal_keras_retinanet_voc.zip?download=1"
ZIP_NAME="heridal_keras_retinanet_voc.zip"
DEST="${DATASETS_DIR:-datasets}/heridal"
ZIP_PATH="${DEST}/${ZIP_NAME}"

mkdir -p "${DEST}"

if [ ! -s "${ZIP_PATH}" ]; then
  echo "Downloading HERIDAL (about 8.3 GB) to ${ZIP_PATH}"
  curl --fail --location --continue-at - --output "${ZIP_PATH}" "${URL}"
else
  echo "Found existing ${ZIP_PATH}, skipping download"
fi

if [ ! -f "${ZIP_PATH}" ]; then
  echo "Error: ${ZIP_PATH} does not exist after download" >&2
  exit 1
fi

echo "Unpacking ${ZIP_PATH}"
unzip -q -o "${ZIP_PATH}" -d "${DEST}"
echo "HERIDAL ready in ${DEST}"
