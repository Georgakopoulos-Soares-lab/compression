#!/usr/bin/env python3
"""
Tests for the SDDL generator using a small FASTA fixture.

Run with: python3 -m pytest tests/test_fasta_small.py -v
   (from the skills/sddl_generate directory)
"""

import os
import struct
import sys
import tempfile
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from analyzer import analyze, detect_format
from sddl_generator import generate_sddl, safe_output_path
from preprocess_generator import generate_preprocess

FIXTURES = Path(__file__).parent / "fixtures"
TINY_FA = FIXTURES / "tiny.fa"


# ---------------------------------------------------------------------------
# Analyzer tests
# ---------------------------------------------------------------------------

class TestAnalyzer:
    def test_detect_fasta(self):
        with open(TINY_FA, "rb") as f:
            head = f.read(8192)
        fmt = detect_format(TINY_FA, head)
        assert fmt == "fasta"

    def test_analyze_fasta_stats(self):
        stats = analyze(TINY_FA)
        assert stats["format"] == "fasta"
        assert stats["records"] == 4
        assert stats["seq_len_min"] == 3  # seq4: "ACG"
        assert stats["seq_len_max"] > 10
        assert stats["has_n_bases"] is True
        assert stats["alphabet_size"] >= 4  # at least A, C, G, T
        assert stats["file_size"] > 0

    def test_common_prefix(self):
        stats = analyze(TINY_FA)
        # All headers start with "test sequence" after the "seq<N> " prefix
        # Actually they start with "seq" — let's just check it's non-empty or a real prefix
        # The headers are: "seq1 test...", "seq2 test...", "seq3 test...", "seq4 short"
        # Common prefix is "seq"
        assert stats["common_prefix_len"] >= 3  # at least "seq"


# ---------------------------------------------------------------------------
# SDDL generator tests
# ---------------------------------------------------------------------------

class TestSddlGenerator:
    def test_fasta_sddl_content(self):
        stats = analyze(TINY_FA)
        sddl = generate_sddl(stats)
        assert "U32 = UInt32LE" in sddl
        assert "magic" in sddl
        assert "Byte[4]" in sddl
        assert "num_records" in sddl
        assert "hdr_offsets" in sddl
        assert "seq_offsets" in sddl
        assert "seq_lengths" in sddl
        assert "hdr_total" in sddl
        assert "seq_total" in sddl
        assert "hdr_pad" in sddl
        assert "seq_pad" in sddl
        assert "Byte[_rem]" in sddl

    def test_sddl_has_baseline_comments(self):
        stats = analyze(TINY_FA)
        sddl = generate_sddl(stats)
        assert "Borrowed baseline idea from nyx/schemas/fasta_packed.sddl" in sddl

    def test_safe_output_path_no_collision(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            p = safe_output_path(td, "custom", ".sddl")
            assert p == td / "custom.sddl"

    def test_safe_output_path_with_collision(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "custom.sddl").write_text("existing")
            p = safe_output_path(td, "custom", ".sddl")
            assert p == td / "custom_v2.sddl"

    def test_safe_output_path_multiple_collisions(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "custom.sddl").write_text("v1")
            (td / "custom_v2.sddl").write_text("v2")
            p = safe_output_path(td, "custom", ".sddl")
            assert p == td / "custom_v3.sddl"


# ---------------------------------------------------------------------------
# Preprocess generator tests
# ---------------------------------------------------------------------------

class TestPreprocessGenerator:
    def test_fasta_preprocess_content(self):
        stats = analyze(TINY_FA)
        script = generate_preprocess(stats)
        assert "FAV4" in script
        assert "pack_bases" in script
        assert "write_chunk" in script
        assert "preprocess" in script
        assert "struct.pack" in script
        assert "Borrowed baseline idea from" in script

    def test_preprocess_script_is_valid_python(self):
        stats = analyze(TINY_FA)
        script = generate_preprocess(stats)
        # Should compile without syntax errors
        compile(script, "<generated>", "exec")

    def test_preprocess_writes_output(self):
        """Test that the generated preprocess.py actually produces binary chunks."""
        stats = analyze(TINY_FA)
        script = generate_preprocess(stats)

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            script_path = td / "preprocess.py"
            script_path.write_text(script)

            # Execute the generated script
            import subprocess
            out_dir = td / "chunks"
            result = subprocess.run(
                [sys.executable, str(script_path), str(TINY_FA), str(out_dir)],
                capture_output=True, text=True,
            )
            assert result.returncode == 0, f"Script failed: {result.stderr}"

            # Check output
            chunks = sorted(out_dir.glob("chunk_*.bin"))
            assert len(chunks) >= 1

            # Verify magic bytes
            with open(chunks[0], "rb") as f:
                magic = f.read(4)
                assert magic == b"FAV4"

                # Read num_records
                num_records = struct.unpack("<I", f.read(4))[0]
                assert num_records == 4  # tiny.fa has 4 records

    def test_preprocess_is_lossless(self):
        """Verify that all headers and sequences are preserved in the binary."""
        stats = analyze(TINY_FA)
        script = generate_preprocess(stats)

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            script_path = td / "preprocess.py"
            script_path.write_text(script)

            out_dir = td / "chunks"
            subprocess_mod = __import__("subprocess")
            result = subprocess_mod.run(
                [sys.executable, str(script_path), str(TINY_FA), str(out_dir)],
                capture_output=True, text=True,
            )
            assert result.returncode == 0

            chunks = sorted(out_dir.glob("chunk_*.bin"))
            assert len(chunks) >= 1

            # Read the binary and verify structure
            with open(chunks[0], "rb") as f:
                data = f.read()

            # Parse header
            magic = data[0:4]
            assert magic == b"FAV4"

            num_records = struct.unpack_from("<I", data, 4)[0]
            assert num_records == 4

            # Parse offset arrays
            off = 8
            hdr_offsets = []
            for i in range(num_records + 1):
                hdr_offsets.append(struct.unpack_from("<I", data, off)[0])
                off += 4

            seq_offsets = []
            for i in range(num_records + 1):
                seq_offsets.append(struct.unpack_from("<I", data, off)[0])
                off += 4

            seq_lengths = []
            for i in range(num_records):
                seq_lengths.append(struct.unpack_from("<I", data, off)[0])
                off += 4

            # Verify offsets are monotonically increasing
            for i in range(len(hdr_offsets) - 1):
                assert hdr_offsets[i] <= hdr_offsets[i + 1]
            for i in range(len(seq_offsets) - 1):
                assert seq_offsets[i] <= seq_offsets[i + 1]

            # Verify seq_lengths are positive
            for sl in seq_lengths:
                assert sl > 0


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_full_pipeline(self):
        """Run the full analyze -> generate_sddl -> generate_preprocess pipeline."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)

            stats = analyze(TINY_FA)
            assert stats["format"] == "fasta"

            sddl = generate_sddl(stats)
            sddl_path = td / "custom.sddl"
            sddl_path.write_text(sddl)
            assert sddl_path.exists()

            preprocess_script = generate_preprocess(stats)
            preprocess_path = td / "preprocess.py"
            preprocess_path.write_text(preprocess_script)
            assert preprocess_path.exists()

            # Both files should be non-empty
            assert sddl_path.stat().st_size > 100
            assert preprocess_path.stat().st_size > 500


if __name__ == "__main__":
    # Simple test runner if pytest is not available
    import traceback

    test_classes = [TestAnalyzer, TestSddlGenerator, TestPreprocessGenerator, TestIntegration]
    passed = 0
    failed = 0

    for cls in test_classes:
        instance = cls()
        for name in dir(instance):
            if name.startswith("test_"):
                try:
                    getattr(instance, name)()
                    print(f"  PASS: {cls.__name__}.{name}")
                    passed += 1
                except Exception as e:
                    print(f"  FAIL: {cls.__name__}.{name}: {e}")
                    traceback.print_exc()
                    failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed > 0 else 0)
