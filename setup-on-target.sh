#!/usr/bin/env bash
# setup-on-target.sh — Build binaries and stage files for Ansible on a Linux VM.
#
# This script runs ON the target VM (pushed there by bundle-and-push.sh).
# It builds zli and _telemetry_scanner.so from source, then stages all
# artifacts so the Ansible playbooks can deploy them.
#
# Output:
#   /tmp/telemetry-compress-stage/   — sidecar files for Ansible (ems_sidecar_src_dir)
#   /tmp/playbooks/                  — Ansible playbooks ready to run

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGE_DIR="/tmp/telemetry-compress-stage"
PLAYBOOK_DIR="/tmp/playbooks"
OPENZL_REPO="https://github.com/facebook/openzl"
OPENZL_COMMIT="e40fe9f314283047147d573d113fa7d17eabf7ac"

# ---- 1) Install build prerequisites -----------------------------------------

echo "==> Installing build prerequisites..."
sudo dnf install -y gcc gcc-c++ make git python3 python3-devel 2>/dev/null || true
sudo dnf install -y gcc-toolset-13-gcc gcc-toolset-13-gcc-c++ 2>/dev/null || true

# Enable GCC toolset 13 if available (RHEL/Rocky 8 GCC 8 is too old for C++20)
GCC_TOOLSET="/opt/rh/gcc-toolset-13/enable"
if [ -f "$GCC_TOOLSET" ]; then
    # shellcheck disable=SC1090
    source "$GCC_TOOLSET"
    echo "   Using gcc-toolset-13: $(gcc --version | head -1)"
else
    echo "   Using system gcc: $(gcc --version | head -1)"
fi

# Verify prerequisites
missing=()
for cmd in gcc g++ make git python3; do
    command -v "$cmd" &>/dev/null || missing+=("$cmd")
done
if [ ${#missing[@]} -gt 0 ]; then
    echo "Error: required tools not found: ${missing[*]}"
    exit 1
fi

# ---- 2) Build zli from OpenZL source ----------------------------------------

ZLI_BUILD_DIR="/tmp/openzl-build"

if [ -x "$STAGE_DIR/zli" ]; then
    echo "==> zli already built, skipping. Delete $STAGE_DIR/zli to rebuild."
else
    echo "==> Building zli from OpenZL source..."

    if [ -d "$ZLI_BUILD_DIR/.git" ]; then
        echo "   OpenZL already cloned at $ZLI_BUILD_DIR"
    else
        rm -rf "$ZLI_BUILD_DIR"
        git clone "$OPENZL_REPO" "$ZLI_BUILD_DIR"
    fi

    cd "$ZLI_BUILD_DIR"
    git fetch --all --tags -q
    git checkout -q "$OPENZL_COMMIT"
    echo "   OpenZL HEAD: $(git rev-parse HEAD)"

    echo "   Compiling (this may take a few minutes)..."
    env -u CFLAGS -u CXXFLAGS -u CPPFLAGS -u LDFLAGS -u LDLIBS \
        make -j"$(nproc 2>/dev/null || echo 4)" MOREFLAGS="-pthread"

    if [ ! -x "$ZLI_BUILD_DIR/zli" ]; then
        echo "Error: zli binary not found after build"
        echo "Check: ls -la $ZLI_BUILD_DIR | grep zli"
        exit 1
    fi

    echo "   zli built: $(file "$ZLI_BUILD_DIR/zli")"
fi

# ---- 3) Build _telemetry_scanner.so C extension -----------------------------

echo "==> Building _telemetry_scanner.so..."
cd "$SCRIPT_DIR"
python3 setup.py build_ext --inplace 2>&1 | tail -3

SO_FILE=$(find "$SCRIPT_DIR" -name "_telemetry_scanner*.so" -print -quit)
if [ -z "$SO_FILE" ]; then
    echo "Warning: _telemetry_scanner.so not found. Compression will fall back to pure Python."
else
    echo "   Built: $SO_FILE"
fi

# ---- 4) Stage files for Ansible ----------------------------------------------

echo "==> Staging files to $STAGE_DIR..."
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR/models/lossless_telemetry"

# Python source
for f in telemetry_sidecar.py telemetry_service.py telemetry_codec.py zljsonl.py; do
    cp "$SCRIPT_DIR/$f" "$STAGE_DIR/"
done

# Decompress wrapper
cp "$SCRIPT_DIR/decompress.sh" "$STAGE_DIR/"
chmod +x "$STAGE_DIR/decompress.sh"

# Built binaries
if [ -x "$ZLI_BUILD_DIR/zli" ]; then
    cp "$ZLI_BUILD_DIR/zli" "$STAGE_DIR/"
    chmod +x "$STAGE_DIR/zli"
fi
if [ -n "$SO_FILE" ]; then
    cp "$SO_FILE" "$STAGE_DIR/_telemetry_scanner.so"
fi

# Trained model + schema
cp "$SCRIPT_DIR/models/lossless_telemetry/telemetry_csv.zl_compressor" \
   "$STAGE_DIR/models/lossless_telemetry/"
cp "$SCRIPT_DIR/models/lossless_telemetry/telemetry_schema.json" \
   "$STAGE_DIR/models/lossless_telemetry/"

echo "   Staged files:"
find "$STAGE_DIR" -type f -exec ls -lh {} \; | awk '{print "     " $NF " (" $5 ")"}'

# ---- 5) Stage playbooks ------------------------------------------------------

echo "==> Staging playbooks to $PLAYBOOK_DIR..."
rm -rf "$PLAYBOOK_DIR"
mkdir -p "$PLAYBOOK_DIR/templates"

cp "$SCRIPT_DIR/playbooks/"*.yaml "$PLAYBOOK_DIR/"
cp "$SCRIPT_DIR/playbooks/templates/"*.j2 "$PLAYBOOK_DIR/templates/"

echo "   Playbooks:"
ls "$PLAYBOOK_DIR/"*.yaml | while read -r f; do echo "     $(basename "$f")"; done

# ---- 6) Verify ---------------------------------------------------------------

echo ""
echo "============================================================"
echo "  Setup complete!"
echo "============================================================"
echo ""
echo "Staged artifacts:  $STAGE_DIR"
echo "Playbooks:         $PLAYBOOK_DIR"
echo ""

# Quick sanity check
ERRORS=0
if [ ! -x "$STAGE_DIR/zli" ]; then
    echo "WARNING: zli binary missing from staging"
    ERRORS=1
fi
if [ ! -f "$STAGE_DIR/_telemetry_scanner.so" ]; then
    echo "WARNING: _telemetry_scanner.so missing (will use slow Python fallback)"
fi
if [ ! -f "$STAGE_DIR/models/lossless_telemetry/telemetry_csv.zl_compressor" ]; then
    echo "WARNING: trained model missing"
    ERRORS=1
fi

if [ "$ERRORS" -eq 0 ]; then
    echo "All checks passed."
fi

echo ""
echo "Next steps — become root first (sudo su), then run these commands:"
echo ""
echo "  sudo su"
echo "  cd $PLAYBOOK_DIR"
echo ""
echo "============================================================"
echo "  A/B Comparison Setup"
echo "============================================================"
echo ""
echo "Step 0: Create POC Kafka topics (one per environment):"
echo ""
echo "  # On Environment A's Kafka broker (SSL off by default):"
echo "  ansible-playbook create-poc-kafka-topics.yaml \\"
echo "    -i \"<KAFKA_BROKER_ENV_A>,\" \\"
echo "    -e ems_hosts=\"<KAFKA_BROKER_ENV_A>\" \\"
echo "    -e ems_broker_addr=\"<BROKER_PRIVATE_IP>:9093\" \\"
echo "    -e ems_topic_name=\"nom-telemetry-poc-compressed\" \\"
echo "    -e ansible_user=centos --become"
echo ""
echo "  # On Environment B's Kafka broker (SSL off by default):"
echo "  ansible-playbook create-poc-kafka-topics.yaml \\"
echo "    -i \"<KAFKA_BROKER_ENV_B>,\" \\"
echo "    -e ems_hosts=\"<KAFKA_BROKER_ENV_B>\" \\"
echo "    -e ems_broker_addr=\"<BROKER_PRIVATE_IP>:9093\" \\"
echo "    -e ems_topic_name=\"nom-telemetry-poc-raw\" \\"
echo "    -e ansible_user=centos --become"
echo ""
echo "  # Zookeeper defaults to <broker_private_ip>:2182. Override with -e ems_zookeeper_addr=..."
echo "  # For SSL environments, add -e ems_ssl=true (uses admin-client.properties, no zookeeper)"
echo ""
echo "Step 1: Deploy decompressor to Data Loader VM (MUST BE FIRST):"
echo ""
echo "  ansible-playbook deploy-telemetry-decompressor.yaml \\"
echo "    -i \"<DATA_LOADER_VM>,\" \\"
echo "    -e ems_hosts=\"<DATA_LOADER_VM>\" \\"
echo "    -e ems_sidecar_src_dir=\"$STAGE_DIR\" \\"
echo "    -e ansible_user=centos --become"
echo ""
echo "--- Environment A (compressed) ---"
echo ""
echo "Step 2a: Deploy sidecar to POC VM (produces to compressed topic):"
echo ""
echo "  ansible-playbook deploy-telemetry-sidecar.yaml \\"
echo "    -i \"<ENV_A_VM>,\" \\"
echo "    -e ems_hosts=\"<ENV_A_VM>\" \\"
echo "    -e ems_sidecar_src_dir=\"$STAGE_DIR\" \\"
echo "    -e kafka_brokers=\"<broker1:9093,broker2:9093>\" \\"
echo "    -e kafka_topic=\"nom-telemetry-poc-compressed\" \\"
echo "    -e ansible_user=centos --become"
echo ""
echo "Step 3a: Switch Telegraf to use sidecar:"
echo ""
echo "  ansible-playbook configure-telegraf-sidecar.yaml \\"
echo "    -i \"<ENV_A_VM>,\" \\"
echo "    -e ems_hosts=\"<ENV_A_VM>\" \\"
echo "    -e ansible_user=centos --become"
echo ""
echo "--- Environment B (uncompressed baseline) ---"
echo ""
echo "Step 2b: Redirect Telegraf to raw topic (no sidecar):"
echo ""
echo "  ansible-playbook configure-telegraf-custom-topic.yaml \\"
echo "    -i \"<ENV_B_VM>,\" \\"
echo "    -e ems_hosts=\"<ENV_B_VM>\" \\"
echo "    -e ems_topic=\"nom-telemetry-poc-raw\" \\"
echo "    -e ansible_user=centos --become"
echo ""
echo "--- Compare ---"
echo ""
echo "After both environments run for a while, compare topic sizes:"
echo "  kafka-log-dirs --describe --bootstrap-server <broker>:9093 \\"
echo "    --command-config /tmp/kafka-admin-ssl.properties \\"
echo "    --topic-list nom-telemetry-poc-compressed,nom-telemetry-poc-raw"
echo ""
echo "--- Rollback (if needed) ---"
echo ""
echo "  # Env A: restore Telegraf Kafka output, stop sidecar"
echo "  ansible-playbook rollback-telegraf-sidecar.yaml \\"
echo "    -i \"<ENV_A_VM>,\" \\"
echo "    -e ems_hosts=\"<ENV_A_VM>\" \\"
echo "    -e ansible_user=centos --become"
echo ""
echo "  # Env B: restore original topic"
echo "  ansible-playbook configure-telegraf-custom-topic.yaml \\"
echo "    -i \"<ENV_B_VM>,\" \\"
echo "    -e ems_hosts=\"<ENV_B_VM>\" \\"
echo "    -e ems_topic=\"nom-telemetry\" \\"
echo "    -e ansible_user=centos --become"
echo ""
echo "Replace <KAFKA_BROKER>, <DATA_LOADER_VM>, <ENV_A_VM>, <ENV_B_VM>,"
echo "and <broker:port> with actual values."
