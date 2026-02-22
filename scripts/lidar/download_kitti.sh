#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_DIR="${DATA_DIR:-$HERE/data}"
mkdir -p "$DATA_DIR"

# KITTI raw drive 0002 (Velodyne LiDAR point clouds)
KITTI_URL="${KITTI_URL:-https://s3.eu-central-1.amazonaws.com/avg-kitti/raw_data/2011_09_26_drive_0002/2011_09_26_drive_0002_sync.zip}"
ZIP_FILE="$DATA_DIR/kitti_sample.zip"
RAW_DIR="$DATA_DIR/2011_09_26/2011_09_26_drive_0002_sync/velodyne_points/data"

if [ ! -f "$ZIP_FILE" ]; then
  echo "Downloading: $KITTI_URL"
  curl -L -o "$ZIP_FILE" "$KITTI_URL"
else
  echo "Already downloaded: $ZIP_FILE"
fi

if [ ! -d "$RAW_DIR" ]; then
  echo "Extracting to: $DATA_DIR"
  unzip -q "$ZIP_FILE" -d "$DATA_DIR"
else
  echo "Already extracted: $RAW_DIR"
fi

echo "KITTI LiDAR frames: $RAW_DIR"
echo "$(ls "$RAW_DIR"/*.bin | wc -l) frames available"
