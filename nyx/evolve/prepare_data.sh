#!/usr/bin/env bash
# prepare_data.sh — One-time data preparation for OpenEvolve hyperparameter search.
#
# Creates:
#   evolve_data/train_50MiB/   — preprocessed FAV4 chunks from a 50 MiB FASTA sample
#   evolve_data/train_100MiB/  — preprocessed FAV4 chunks from a 100 MiB FASTA sample
#   evolve_data/train_200MiB/  — preprocessed FAV4 chunks from a 200 MiB FASTA sample
#   evolve_data/test_chunks/   — preprocessed FAV4 chunks from a held-out 200 MiB sample
#
# Prerequisites:
#   - nyx is built ('nyx build')
#   - Python 3 with nyx venv active
#
# Usage:
#   bash nyx/evolve/prepare_data.sh
#
# The genome is downloaded automatically if not present.
# All of evolve_data/ is gitignored.
set -euo pipefail

# ── Paths ────────────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_DIR="${REPO_ROOT}/data"
EVOLVE_DATA="${REPO_ROOT}/evolve_data"

# Genome
GENOME_GZ="${DATA_DIR}/GCF_000001635.27_GRCm39_genomic.fna.gz"
GENOME="${DATA_DIR}/GCF_000001635.27_GRCm39_genomic.fna"
GENOME_URL="https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/001/635/GCF_000001635.27_GRCm39/GCF_000001635.27_GRCm39_genomic.fna.gz"

# Binaries (resolved via nyx's path conventions)
NYX_ROOT="${REPO_ROOT}/nyx"
PREPROCESSOR="${NYX_ROOT}/bin/genomic_preprocessor"
MAKE_SAMPLE="${NYX_ROOT}/scripts/make_train_sample.py"
SCHEMA="${NYX_ROOT}/schemas/fasta_packed.sddl"

# Training sample sizes (MiB)
TRAIN_SIZES=(50 100 200)
# Test sample: 200 MiB from unseen genome regions
TEST_SIZE_MIB=200
# Records to skip before extracting test data (MiB).
# Must be >= largest training sample to avoid overlap.
TEST_SKIP_MIB=250

# ── Preflight checks ────────────────────────────────────────────────────────
echo "=== OpenEvolve Data Preparation ==="
echo "Repository root: ${REPO_ROOT}"

if [ ! -x "${PREPROCESSOR}" ]; then
    echo "ERROR: genomic_preprocessor not found at ${PREPROCESSOR}"
    echo "       Run 'nyx build' first."
    exit 1
fi

if [ ! -f "${MAKE_SAMPLE}" ]; then
    echo "ERROR: make_train_sample.py not found at ${MAKE_SAMPLE}"
    exit 1
fi

if [ ! -f "${SCHEMA}" ]; then
    echo "ERROR: SDDL schema not found at ${SCHEMA}"
    exit 1
fi

# ── Step 1: Download genome ─────────────────────────────────────────────────
mkdir -p "${DATA_DIR}"

if [ -f "${GENOME}" ]; then
    echo "[1/5] Genome already decompressed: ${GENOME}"
elif [ -f "${GENOME_GZ}" ]; then
    echo "[1/5] Decompressing genome..."
    gunzip -k "${GENOME_GZ}"
    echo "      Done: ${GENOME}"
else
    echo "[1/5] Downloading genome..."
    curl -fSL -o "${GENOME_GZ}" "${GENOME_URL}"
    echo "      Decompressing..."
    gunzip -k "${GENOME_GZ}"
    echo "      Done: ${GENOME}"
fi

# ── Step 2: Create training FASTA samples ────────────────────────────────────
mkdir -p "${EVOLVE_DATA}"

for SIZE in "${TRAIN_SIZES[@]}"; do
    SAMPLE="${EVOLVE_DATA}/train_${SIZE}MiB.fasta"
    if [ -f "${SAMPLE}" ]; then
        echo "[2/5] Training sample already exists: ${SAMPLE}"
    else
        echo "[2/5] Creating ${SIZE} MiB training sample..."
        python3 "${MAKE_SAMPLE}" \
            --in "${GENOME}" \
            --out "${SAMPLE}" \
            --target-mib "${SIZE}"
    fi
done

# ── Step 3: Create held-out test FASTA sample ────────────────────────────────
TEST_FASTA="${EVOLVE_DATA}/test_${TEST_SIZE_MIB}MiB.fasta"
if [ -f "${TEST_FASTA}" ]; then
    echo "[3/5] Test sample already exists: ${TEST_FASTA}"
else
    echo "[3/5] Creating ${TEST_SIZE_MIB} MiB held-out test sample (skipping first ${TEST_SKIP_MIB} MiB)..."
    python3 - "${GENOME}" "${TEST_FASTA}" "${TEST_SKIP_MIB}" "${TEST_SIZE_MIB}" <<'PYEOF'
"""Extract a FASTA sample from records AFTER the first skip_mib MiB.

This ensures the test set does not overlap with any training sample.
"""
import sys
from pathlib import Path

genome_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
skip_bytes = int(sys.argv[3]) * 1024 * 1024
target_bytes = int(sys.argv[4]) * 1024 * 1024

output_path.parent.mkdir(parents=True, exist_ok=True)

skipped = 0
written = 0
records_skipped = 0
records_written = 0
header = None
seq_lines = []

def record_bytes(h, seqs):
    return len((h + "".join(seqs)).encode("utf-8", errors="surrogateescape"))

with genome_path.open("rt", encoding="utf-8", errors="surrogateescape") as f, \
     output_path.open("wt", encoding="utf-8", errors="surrogateescape") as w:
    for line in f:
        if line.startswith(">"):
            # Process previous record
            if header is not None:
                rb = record_bytes(header, seq_lines)
                if skipped < skip_bytes:
                    skipped += rb
                    records_skipped += 1
                elif written + rb <= target_bytes or written == 0:
                    w.write(header + "".join(seq_lines))
                    written += rb
                    records_written += 1
                else:
                    break  # We've collected enough test data
            header = line
            seq_lines = []
        else:
            if header is not None:
                seq_lines.append(line)

    # Process final record
    if header is not None:
        rb = record_bytes(header, seq_lines)
        if skipped < skip_bytes:
            pass  # Still in skip zone
        elif written + rb <= target_bytes or written == 0:
            w.write(header + "".join(seq_lines))
            written += rb
            records_written += 1

print(f"Skipped {records_skipped} records ({skipped / 1024 / 1024:.1f} MiB)")
print(f"Wrote {records_written} test records ({written / 1024 / 1024:.1f} MiB)")
print(f"Output: {output_path}")
PYEOF
fi

# ── Step 4: Preprocess training samples into FAV4 chunks ─────────────────────
for SIZE in "${TRAIN_SIZES[@]}"; do
    SAMPLE="${EVOLVE_DATA}/train_${SIZE}MiB.fasta"
    CHUNKS_DIR="${EVOLVE_DATA}/train_${SIZE}MiB"

    if [ -d "${CHUNKS_DIR}" ] && ls "${CHUNKS_DIR}"/chunk_*.fasta_packed.bin &>/dev/null; then
        echo "[4/5] Chunks already exist: ${CHUNKS_DIR}/"
    else
        echo "[4/5] Preprocessing ${SIZE} MiB training sample -> ${CHUNKS_DIR}/"
        mkdir -p "${CHUNKS_DIR}"
        "${PREPROCESSOR}" "${SAMPLE}" "${CHUNKS_DIR}" 1 fasta_packed
        echo "      $(ls "${CHUNKS_DIR}"/chunk_*.fasta_packed.bin 2>/dev/null | wc -l | tr -d ' ') chunk(s) created"
    fi
done

# ── Step 5: Preprocess FULL genome into FAV4 chunks (for evaluation) ─────────
# The real nyx pipeline trains on a small sample but compresses the ENTIRE genome.
# We preprocess the full genome once so the evaluator can compress all chunks.
FULL_CHUNKS="${EVOLVE_DATA}/full_genome_chunks"
FULL_THREADS=16
if [ -d "${FULL_CHUNKS}" ] && ls "${FULL_CHUNKS}"/chunk_*.fasta_packed.bin &>/dev/null; then
    echo "[5/5] Full genome chunks already exist: ${FULL_CHUNKS}/"
else
    echo "[5/5] Preprocessing full genome -> ${FULL_CHUNKS}/ (${FULL_THREADS} threads)..."
    mkdir -p "${FULL_CHUNKS}"
    "${PREPROCESSOR}" "${GENOME}" "${FULL_CHUNKS}" "${FULL_THREADS}" fasta_packed
    echo "      $(ls "${FULL_CHUNKS}"/chunk_*.fasta_packed.bin 2>/dev/null | wc -l | tr -d ' ') chunk(s) created"
fi

# Write original genome size so the evaluator can compute text-to-compressed ratio
# (matching nyx compress output). Uses wc -c for cross-platform compatibility.
wc -c < "${GENOME}" | tr -d ' ' > "${FULL_CHUNKS}/original_genome_bytes.txt"
echo "  Original genome size: $(cat "${FULL_CHUNKS}/original_genome_bytes.txt") bytes"

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "=== Data preparation complete ==="
echo ""
echo "Training data:"
for SIZE in "${TRAIN_SIZES[@]}"; do
    DIR="${EVOLVE_DATA}/train_${SIZE}MiB"
    COUNT=$(ls "${DIR}"/chunk_*.fasta_packed.bin 2>/dev/null | wc -l | tr -d ' ')
    BYTES=$(du -sh "${DIR}" 2>/dev/null | cut -f1)
    echo "  ${SIZE} MiB: ${COUNT} chunk(s), ${BYTES} on disk"
done
echo ""
echo "Full genome evaluation data:"
COUNT=$(ls "${FULL_CHUNKS}"/chunk_*.fasta_packed.bin 2>/dev/null | wc -l | tr -d ' ')
BYTES=$(du -sh "${FULL_CHUNKS}" 2>/dev/null | cut -f1)
echo "  ${COUNT} chunk(s), ${BYTES} on disk"
echo ""
echo "Ready for OpenEvolve. Run: bash nyx/evolve/run.sh"
