#!/usr/bin/env bash
# bundle-and-push.sh — Package sidecar artifacts and push to a remote VM.
#
# Bundles all compression sidecar files, playbooks, and build inputs into a
# tarball, SCPs it to the target VM, then SSHes in and runs setup-on-target.sh
# to build zli, the C extension, and stage everything for Ansible.
#
# Usage:
#   ./bundle-and-push.sh <terraform-dir> <target-vm-hostname>
#
# Example:
#   ./bundle-and-push.sh ~/work/sst-template-migration/head/SB_load_192_dest/run/194/terraform amcx0
#
# Prerequisites:
#   - SSH access to the target VM via the terraform directory's ssh.config
#   - The target VM has internet access (to clone OpenZL from GitHub)

set -euo pipefail

# ---- Argument parsing -------------------------------------------------------

if [ $# -ne 2 ]; then
    echo "Usage: $0 <terraform-dir> <target-vm-hostname>"
    echo ""
    echo "  terraform-dir     Path to terraform directory (contains ssh.config, id_rsa)"
    echo "  target-vm-hostname  Hostname of the VM (as defined in ssh.config)"
    exit 1
fi

TERRAFORM_DIR="$(cd "$1" && pwd)"
TARGET_VM="$2"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SSH_CONFIG="$TERRAFORM_DIR/ssh.config"
SSH_KEY="$TERRAFORM_DIR/id_rsa"

# ---- Validate inputs --------------------------------------------------------

for f in "$SSH_CONFIG" "$SSH_KEY"; do
    if [ ! -f "$f" ]; then
        echo "Error: $f not found"
        exit 1
    fi
done

# Check required source files exist
for f in telemetry_sidecar.py telemetry_service.py telemetry_codec.py zljsonl.py \
         _telemetry_scanner.c setup.py decompress.sh; do
    if [ ! -f "$REPO_ROOT/$f" ]; then
        echo "Error: $REPO_ROOT/$f not found"
        exit 1
    fi
done

if [ ! -f "$REPO_ROOT/models/lossless_telemetry/telemetry_csv.zl_compressor" ]; then
    echo "Error: trained model not found at $REPO_ROOT/models/lossless_telemetry/telemetry_csv.zl_compressor"
    exit 1
fi

# ---- Build tarball -----------------------------------------------------------

BUNDLE_NAME="sidecar-bundle"
WORK_DIR="$(mktemp -d)"
BUNDLE_DIR="$WORK_DIR/$BUNDLE_NAME"
mkdir -p "$BUNDLE_DIR/models/lossless_telemetry"
mkdir -p "$BUNDLE_DIR/playbooks/templates"

echo "==> Bundling sidecar artifacts..."

# Python source + C extension source + build config
for f in telemetry_sidecar.py telemetry_service.py telemetry_codec.py zljsonl.py \
         _telemetry_scanner.c setup.py decompress.sh; do
    cp "$REPO_ROOT/$f" "$BUNDLE_DIR/"
done

# Trained model + schema
cp "$REPO_ROOT/models/lossless_telemetry/telemetry_csv.zl_compressor" \
   "$BUNDLE_DIR/models/lossless_telemetry/"
cp "$REPO_ROOT/models/lossless_telemetry/telemetry_schema.json" \
   "$BUNDLE_DIR/models/lossless_telemetry/"

# Ansible playbooks + templates (copy all)
cp "$REPO_ROOT/ansible/playbooks/"*.yaml "$BUNDLE_DIR/playbooks/"
cp "$REPO_ROOT/ansible/playbooks/templates/"*.j2 "$BUNDLE_DIR/playbooks/templates/"

# Setup script (runs on the VM)
cp "$REPO_ROOT/setup-on-target.sh" "$BUNDLE_DIR/"

# Create tarball
TARBALL="$WORK_DIR/$BUNDLE_NAME.tar.gz"
tar -czf "$TARBALL" -C "$WORK_DIR" "$BUNDLE_NAME"
echo "   Bundle: $(du -h "$TARBALL" | cut -f1) compressed"

# ---- Push to target VM ------------------------------------------------------

REMOTE_DIR="/tmp"
SSH_OPTS="-F $SSH_CONFIG -o StrictHostKeyChecking=no"

echo "==> Pushing bundle to $TARGET_VM..."
scp $SSH_OPTS "$TARBALL" "centos@${TARGET_VM}:${REMOTE_DIR}/"

echo "==> Extracting on $TARGET_VM..."
ssh $SSH_OPTS "centos@${TARGET_VM}" \
    "cd $REMOTE_DIR && tar -xzf $BUNDLE_NAME.tar.gz"

echo "==> Running setup-on-target.sh on $TARGET_VM..."
ssh $SSH_OPTS -t "centos@${TARGET_VM}" \
    "cd $REMOTE_DIR/$BUNDLE_NAME && bash setup-on-target.sh"

# ---- Cleanup -----------------------------------------------------------------

rm -rf "$WORK_DIR"
echo ""
echo "==> Done. SSH into $TARGET_VM to run the playbooks:"
echo "   ssh $SSH_OPTS centos@$TARGET_VM"
