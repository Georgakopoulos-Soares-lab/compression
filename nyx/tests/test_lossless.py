"""Tests for the lossless FASTA compression pipeline.

Tests are organized in four tiers:
  1. TestCodecRoundTrip  — fasta_codec encode/decode only (no OpenZL needed)
  2. TestStreamInvariants — validate internal consistency of encoded streams
  3. TestZlfastaContainer — .zlfasta container create/extract (pure Python)
  4. TestFullPipeline    — full compress-lossless/decompress-lossless (requires zli)
"""

import hashlib
import os
import shutil
import struct
import tempfile
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURE_NAMES = [
    "acgt_only.fasta",
    "with_ns.fasta",
    "iupac.fasta",
    "softmasked.fasta",
    "irregular_wrap.fasta",
    "crlf.fasta",
    "no_trailing_newline.fasta",
    "gaps.fasta",
    "long_n_runs.fasta",
    "scattered_ns.fasta",
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _zli_available() -> bool:
    """Check if zli binary is available."""
    try:
        from nyx.utils.paths import find_zli
        find_zli()
        return True
    except (FileNotFoundError, ImportError):
        return False


def _fasta_codec_available() -> bool:
    """Check if fasta_codec binary is available."""
    try:
        from nyx.utils.paths import find_fasta_codec
        find_fasta_codec()
        return True
    except (FileNotFoundError, ImportError):
        return False


requires_fasta_codec = pytest.mark.skipif(
    not _fasta_codec_available(),
    reason="fasta_codec binary not found (run 'nyx build' first)",
)

requires_zli = pytest.mark.skipif(
    not _zli_available(),
    reason="zli binary not found (run 'nyx build' first)",
)


# =============================================================================
# Stream invariant helpers (pure Python — reads meta.bin and stream files)
# =============================================================================


def _decode_varint(data: bytes, offset: int):
    """Decode a single unsigned LEB128 varint. Returns (value, new_offset)."""
    result = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if (byte & 0x80) == 0:
            return result, offset
        shift += 7
    raise ValueError("Truncated varint")


def _popcount_bits(data: bytes, nbits: int) -> int:
    """Count number of 1-bits in the first nbits of packed data (MSB-first)."""
    count = 0
    for i in range(nbits):
        if (data[i // 8] >> (7 - (i % 8))) & 1:
            count += 1
    return count


def validate_streams(streams_dir: Path) -> list:
    """Validate internal consistency of encoded stream files.

    Returns a list of error strings. Empty list means all invariants pass.
    """
    errors = []
    meta_path = streams_dir / "meta.bin"
    if not meta_path.exists():
        return ["meta.bin not found"]

    meta = meta_path.read_bytes()
    # Global header: magic(4) + version(4) + num_records(4) +
    #   newline_style(1) + has_trailing_nl(1) + reserved(6) = 20 bytes
    GLOBAL_HEADER_SIZE = 20
    if len(meta) < GLOBAL_HEADER_SIZE:
        return ["meta.bin too small for header"]

    # Parse global header
    magic = struct.unpack_from("<I", meta, 0)[0]
    if magic != 0x4346584E:  # "NXFC"
        errors.append(f"Bad magic: 0x{magic:08X} (expected 0x4346584E)")
        return errors

    version = struct.unpack_from("<I", meta, 4)[0]
    num_records = struct.unpack_from("<I", meta, 8)[0]
    newline_style = meta[12]
    has_trailing_nl = meta[13]

    if version != 1:
        errors.append(f"Unsupported version: {version}")
    if newline_style not in (0, 1):
        errors.append(f"Invalid newline_style: {newline_style}")
    if has_trailing_nl not in (0, 1):
        errors.append(f"Invalid has_trailing_newline: {has_trailing_nl}")

    # Parse per-record metadata (36 bytes each, starting after global header)
    RECORD_META_SIZE = 36
    expected_meta_size = GLOBAL_HEADER_SIZE + num_records * RECORD_META_SIZE
    if len(meta) < expected_meta_size:
        errors.append(
            f"meta.bin too small: {len(meta)} bytes, "
            f"expected >= {expected_meta_size} for {num_records} records"
        )
        return errors

    # Read stream files
    streams = {}
    for name in ["headers.bin", "nmask.bin", "acgtmask.bin", "bases2.bin",
                  "exceptions.bin", "case.bin", "wrapping.bin"]:
        p = streams_dir / name
        if p.exists():
            streams[name] = p.read_bytes()
        else:
            streams[name] = b""

    # Track stream cursor positions
    cursors = {name: 0 for name in streams}

    for r in range(num_records):
        off = GLOBAL_HEADER_SIZE + r * RECORD_META_SIZE
        seq_len = struct.unpack_from("<I", meta, off)[0]
        header_len = struct.unpack_from("<I", meta, off + 4)[0]
        nmask_bytes = struct.unpack_from("<I", meta, off + 8)[0]
        acgtmask_bytes = struct.unpack_from("<I", meta, off + 12)[0]
        bases2_bytes = struct.unpack_from("<I", meta, off + 16)[0]
        exceptions_bytes = struct.unpack_from("<I", meta, off + 20)[0]
        case_bytes = struct.unpack_from("<I", meta, off + 24)[0]
        wrap_bytes = struct.unpack_from("<I", meta, off + 28)[0]
        case_mode = meta[off + 32]

        L = seq_len
        pfx = f"record[{r}]"

        # --- Header length check ---
        if cursors["headers.bin"] + header_len > len(streams["headers.bin"]):
            errors.append(f"{pfx}: header overflows headers.bin")
        cursors["headers.bin"] += header_len

        # --- N-mask size: ceil(L / 8) bytes ---
        expected_nmask_bytes = (L + 7) // 8
        if nmask_bytes != expected_nmask_bytes:
            errors.append(
                f"{pfx}: nmask_bytes={nmask_bytes}, "
                f"expected ceil({L}/8)={expected_nmask_bytes}"
            )

        # --- Compute L' from N-mask ---
        nm_start = cursors["nmask.bin"]
        if nm_start + nmask_bytes <= len(streams["nmask.bin"]):
            nmask_data = streams["nmask.bin"][nm_start:nm_start + nmask_bytes]
            count_n = _popcount_bits(nmask_data, L)
            Lp = L - count_n
        else:
            errors.append(f"{pfx}: nmask overflows nmask.bin")
            Lp = None
        cursors["nmask.bin"] += nmask_bytes

        # --- ACGT-mask size: ceil(L' / 8) bytes ---
        if Lp is not None:
            expected_am_bytes = (Lp + 7) // 8
            if acgtmask_bytes != expected_am_bytes:
                errors.append(
                    f"{pfx}: acgtmask_bytes={acgtmask_bytes}, "
                    f"expected ceil({Lp}/8)={expected_am_bytes}"
                )

        # --- Count ACGT from ACGT-mask ---
        am_start = cursors["acgtmask.bin"]
        count_acgt = None
        if Lp is not None and am_start + acgtmask_bytes <= len(streams["acgtmask.bin"]):
            am_data = streams["acgtmask.bin"][am_start:am_start + acgtmask_bytes]
            count_acgt = _popcount_bits(am_data, Lp)
        cursors["acgtmask.bin"] += acgtmask_bytes

        # --- bases2 size: ceil(count_acgt / 4) bytes ---
        if count_acgt is not None:
            expected_b2_bytes = (count_acgt + 3) // 4
            if bases2_bytes != expected_b2_bytes:
                errors.append(
                    f"{pfx}: bases2_bytes={bases2_bytes}, "
                    f"expected ceil({count_acgt}/4)={expected_b2_bytes}"
                )
        cursors["bases2.bin"] += bases2_bytes

        # --- Exceptions: count should equal (L' - count_acgt) ---
        if count_acgt is not None and Lp is not None:
            expected_exc_count = Lp - count_acgt
            ex_start = cursors["exceptions.bin"]
            ex_data = streams["exceptions.bin"][ex_start:ex_start + exceptions_bytes]
            # Count exceptions by parsing delta-varint + symbol pairs
            actual_exc_count = 0
            ex_off = 0
            while ex_off < len(ex_data):
                try:
                    _val, ex_off = _decode_varint(ex_data, ex_off)
                except ValueError:
                    errors.append(f"{pfx}: truncated varint in exceptions")
                    break
                if ex_off < len(ex_data):
                    ex_off += 1  # skip symbol byte
                    actual_exc_count += 1
                else:
                    errors.append(f"{pfx}: exception missing symbol byte")
                    break
            if actual_exc_count != expected_exc_count:
                errors.append(
                    f"{pfx}: exceptions count={actual_exc_count}, "
                    f"expected {expected_exc_count} (L'={Lp}, acgt={count_acgt})"
                )
        cursors["exceptions.bin"] += exceptions_bytes

        # --- Case mode validation ---
        if case_mode == 0:  # CASE_NONE
            if case_bytes != 0:
                errors.append(
                    f"{pfx}: case_mode=NONE but case_bytes={case_bytes}"
                )
        elif case_mode == 1:  # CASE_MASK
            expected_case_bytes = (L + 7) // 8
            if case_bytes != expected_case_bytes:
                errors.append(
                    f"{pfx}: case_mode=MASK, case_bytes={case_bytes}, "
                    f"expected ceil({L}/8)={expected_case_bytes}"
                )
        elif case_mode == 2:  # CASE_SPARSE
            cs_start = cursors["case.bin"]
            cs_data = streams["case.bin"][cs_start:cs_start + case_bytes]
            if len(cs_data) > 0:
                try:
                    count, cs_off = _decode_varint(cs_data, 0)
                    # Verify positions are monotonically increasing and within [0, L)
                    pos = 0
                    for k in range(count):
                        if cs_off >= len(cs_data):
                            errors.append(
                                f"{pfx}: sparse case truncated at entry {k}/{count}"
                            )
                            break
                        delta, cs_off = _decode_varint(cs_data, cs_off)
                        pos += delta
                        if pos >= L:
                            errors.append(
                                f"{pfx}: sparse case position {pos} >= L={L}"
                            )
                except ValueError:
                    errors.append(f"{pfx}: truncated varint in case data")
            else:
                errors.append(f"{pfx}: case_mode=SPARSE but case_bytes=0")
        else:
            errors.append(f"{pfx}: unknown case_mode={case_mode}")
        cursors["case.bin"] += case_bytes

        # --- Wrapping validation ---
        wr_start = cursors["wrapping.bin"]
        wr_data = streams["wrapping.bin"][wr_start:wr_start + wrap_bytes]
        if len(wr_data) > 0:
            wrap_mode = wr_data[0]
            if wrap_mode == 0x01:  # COMPACT
                if len(wr_data) < 9:
                    errors.append(f"{pfx}: WRAP_COMPACT data too short")
                else:
                    width = struct.unpack_from("<I", wr_data, 1)[0]
                    last_len = struct.unpack_from("<I", wr_data, 5)[0]
                    # Reconstruct total to verify it matches L
                    if width == 0:
                        total = last_len
                    elif last_len == width:
                        total = (L // width) * width  # should == L
                    else:
                        full_lines = (L - last_len) // width if L > width else 0
                        total = full_lines * width + last_len
                    if total != L:
                        errors.append(
                            f"{pfx}: WRAP_COMPACT total={total} != L={L} "
                            f"(width={width}, last_len={last_len})"
                        )
            elif wrap_mode == 0x02:  # EXPLICIT
                if len(wr_data) < 5:
                    errors.append(f"{pfx}: WRAP_EXPLICIT data too short")
                else:
                    num_lines = struct.unpack_from("<I", wr_data, 1)[0]
                    expected_wr_size = 1 + 4 + num_lines * 4
                    if len(wr_data) < expected_wr_size:
                        errors.append(
                            f"{pfx}: WRAP_EXPLICIT truncated "
                            f"({len(wr_data)} < {expected_wr_size})"
                        )
                    else:
                        total = 0
                        for k in range(num_lines):
                            ll = struct.unpack_from("<I", wr_data, 5 + k * 4)[0]
                            total += ll
                        if total != L:
                            errors.append(
                                f"{pfx}: WRAP_EXPLICIT sum={total} != L={L}"
                            )
            else:
                errors.append(f"{pfx}: unknown wrap_mode=0x{wrap_mode:02X}")
        elif L > 0:
            errors.append(f"{pfx}: non-empty sequence but wrap_bytes=0")
        cursors["wrapping.bin"] += wrap_bytes

    # --- Verify all stream data was consumed exactly ---
    for name, cursor in cursors.items():
        actual_len = len(streams[name])
        if cursor != actual_len:
            errors.append(
                f"{name}: consumed {cursor} bytes but file has {actual_len}"
            )

    return errors


# =============================================================================
# Tier 1: fasta_codec encode → decode roundtrip
# =============================================================================


@requires_fasta_codec
class TestCodecRoundTrip:
    """Test that fasta_codec encode → decode produces byte-identical output."""

    @pytest.fixture(params=FIXTURE_NAMES)
    def fasta_fixture(self, request):
        return FIXTURES_DIR / request.param

    def test_roundtrip(self, fasta_fixture, tmp_path):
        from nyx.core.codec import encode, decode

        streams_dir = tmp_path / "streams"
        output = tmp_path / "reconstructed.fasta"

        encode(fasta_fixture, streams_dir)
        decode(streams_dir, output)

        assert _sha256(fasta_fixture) == _sha256(output), (
            f"Roundtrip failed for {fasta_fixture.name}: "
            f"files differ"
        )

    def test_stream_files_created(self, fasta_fixture, tmp_path):
        from nyx.core.codec import encode

        streams_dir = tmp_path / "streams"
        stream_files = encode(fasta_fixture, streams_dir)

        expected_names = {
            "meta.bin", "headers.bin", "nmask.bin", "acgtmask.bin",
            "bases2.bin", "exceptions.bin", "case.bin", "wrapping.bin",
        }
        actual_names = {f.name for f in stream_files}
        assert actual_names == expected_names


@requires_fasta_codec
class TestEdgeCases:
    """Test edge cases for the codec."""

    def _roundtrip(self, content: bytes, tmp_path: Path) -> bool:
        """Write content, encode, decode, compare."""
        from nyx.core.codec import encode, decode

        input_file = tmp_path / "input.fasta"
        with open(input_file, "wb") as f:
            f.write(content)

        streams_dir = tmp_path / "streams"
        output = tmp_path / "output.fasta"

        encode(input_file, streams_dir)
        decode(streams_dir, output)

        return _sha256(input_file) == _sha256(output)

    def test_empty_sequence(self, tmp_path):
        content = b">empty_seq\n>next_seq\nACGT\n"
        assert self._roundtrip(content, tmp_path)

    def test_single_base(self, tmp_path):
        content = b">single\nA\n"
        assert self._roundtrip(content, tmp_path)

    def test_very_long_header(self, tmp_path):
        header = b">seq " + b"x" * 5000 + b"\n"
        content = header + b"ACGTACGT\n"
        assert self._roundtrip(content, tmp_path)

    def test_multi_record(self, tmp_path):
        records = b""
        for i in range(100):
            records += f">record_{i}\n".encode()
            records += b"ACGTACGTNNNN\n"
        assert self._roundtrip(records, tmp_path)

    def test_all_n_sequence(self, tmp_path):
        content = b">all_n\nNNNNNNNNNN\n"
        assert self._roundtrip(content, tmp_path)

    def test_all_lowercase(self, tmp_path):
        content = b">lower\nacgtacgtacgt\n"
        assert self._roundtrip(content, tmp_path)

    def test_mixed_iupac_and_case(self, tmp_path):
        content = b">mixed\nAcGtRySwKmBdHvNn\n"
        assert self._roundtrip(content, tmp_path)

    def test_gap_characters(self, tmp_path):
        content = b">gaps\nACGT--ACGT..ACGT\n"
        assert self._roundtrip(content, tmp_path)

    def test_lowercase_iupac(self, tmp_path):
        content = b">lower_iupac\nrysw\n"
        assert self._roundtrip(content, tmp_path)

    def test_lowercase_n(self, tmp_path):
        content = b">lower_n\nACGTnnnnACGT\n"
        assert self._roundtrip(content, tmp_path)

    def test_crlf_no_trailing(self, tmp_path):
        content = b">seq1\r\nACGT\r\nTGCA"
        assert self._roundtrip(content, tmp_path)

    def test_single_char_lines(self, tmp_path):
        content = b">seq1\nA\nC\nG\nT\n"
        assert self._roundtrip(content, tmp_path)

    def test_header_with_special_chars(self, tmp_path):
        content = b">seq|1:2-3 (desc) [tag] {info}\nACGT\n"
        assert self._roundtrip(content, tmp_path)


# =============================================================================
# Tier 2: Stream invariant validation
# =============================================================================


@requires_fasta_codec
class TestStreamInvariants:
    """Validate internal consistency of encoded stream files."""

    @pytest.fixture(params=FIXTURE_NAMES)
    def fasta_fixture(self, request):
        return FIXTURES_DIR / request.param

    def test_invariants(self, fasta_fixture, tmp_path):
        from nyx.core.codec import encode

        streams_dir = tmp_path / "streams"
        encode(fasta_fixture, streams_dir)

        errors = validate_streams(streams_dir)
        assert errors == [], (
            f"Stream invariant failures for {fasta_fixture.name}:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    def test_invariants_edge_cases(self, tmp_path):
        """Validate invariants for edge-case content."""
        from nyx.core.codec import encode

        cases = {
            "all_n": b">seq\nNNNNNNNNNN\n",
            "empty_seq": b">empty\n>next\nACGT\n",
            "mixed": b">m\nAcGtRySwKmBdHvNn\n",
            "single": b">s\nA\n",
            "gaps": b">g\nACGT--..ACGT\n",
        }

        for name, content in cases.items():
            d = tmp_path / name
            d.mkdir()
            input_file = d / "input.fasta"
            input_file.write_bytes(content)

            streams_dir = d / "streams"
            encode(input_file, streams_dir)

            errors = validate_streams(streams_dir)
            assert errors == [], (
                f"Stream invariant failures for edge case '{name}':\n"
                + "\n".join(f"  - {e}" for e in errors)
            )


# =============================================================================
# Tier 3: .zlfasta container roundtrip
# =============================================================================


class TestZlfastaContainer:
    """Test the .zlfasta container format."""

    def test_create_and_extract(self, tmp_path):
        from nyx.core.zlfasta import create_zlfasta, extract_zlfasta

        entries = {
            "meta.bin": (b"metadata_content", 16),
            "data.bin": (b"compressed_blob_here", 100),
        }

        container_path = tmp_path / "test.zlfasta"
        create_zlfasta(container_path, entries)

        extract_dir = tmp_path / "extracted"
        result = extract_zlfasta(container_path, extract_dir)

        assert set(result.keys()) == {"meta.bin", "data.bin"}
        assert (extract_dir / "meta.bin").read_bytes() == b"metadata_content"
        assert (extract_dir / "data.bin").read_bytes() == b"compressed_blob_here"

    def test_crc32_validation(self, tmp_path):
        from nyx.core.zlfasta import create_zlfasta, extract_zlfasta, ZlfastaError

        entries = {"test.bin": (b"hello", 5)}
        container_path = tmp_path / "test.zlfasta"
        create_zlfasta(container_path, entries)

        # Corrupt the data (flip a byte before the CRC)
        data = bytearray(container_path.read_bytes())
        data[20] ^= 0xFF
        container_path.write_bytes(bytes(data))

        with pytest.raises(ZlfastaError, match="CRC32 mismatch"):
            extract_zlfasta(container_path, tmp_path / "bad")

    def test_bad_magic(self, tmp_path):
        from nyx.core.zlfasta import extract_zlfasta, ZlfastaError

        container_path = tmp_path / "bad.zlfasta"
        container_path.write_bytes(b"BADMAGIC" + b"\x00" * 20)

        with pytest.raises(ZlfastaError, match="Bad magic"):
            extract_zlfasta(container_path, tmp_path / "out")

    def test_empty_entries(self, tmp_path):
        from nyx.core.zlfasta import create_zlfasta, extract_zlfasta

        entries = {"empty.bin": (b"", 0)}
        container_path = tmp_path / "empty.zlfasta"
        create_zlfasta(container_path, entries)

        result = extract_zlfasta(container_path, tmp_path / "out")
        assert (tmp_path / "out" / "empty.bin").read_bytes() == b""


# =============================================================================
# Tier 4: Full pipeline (requires zli)
# =============================================================================


@requires_fasta_codec
@requires_zli
class TestFullPipeline:
    """Test the full compress-lossless → decompress-lossless pipeline."""

    @pytest.fixture(params=FIXTURE_NAMES)
    def fasta_fixture(self, request):
        return FIXTURES_DIR / request.param

    def test_roundtrip(self, fasta_fixture, tmp_path):
        from click.testing import CliRunner
        from nyx.cli import main

        runner = CliRunner()
        compressed = tmp_path / "compressed.zlfasta"
        decompressed = tmp_path / "decompressed.fasta"

        # Compress
        result = runner.invoke(
            main,
            ["compress-lossless", str(fasta_fixture), "-o", str(compressed)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, f"Compress failed:\n{result.output}"
        assert compressed.exists()

        # Decompress
        result = runner.invoke(
            main,
            ["decompress-lossless", str(compressed), "-o", str(decompressed), "-f"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, f"Decompress failed:\n{result.output}"
        assert decompressed.exists()

        # Verify byte-identical
        assert _sha256(fasta_fixture) == _sha256(decompressed), (
            f"Full pipeline roundtrip failed for {fasta_fixture.name}"
        )

    def test_output_default_name(self, tmp_path):
        """Test that default output name is <input>.zlfasta."""
        from click.testing import CliRunner
        from nyx.cli import main

        fixture = FIXTURES_DIR / "acgt_only.fasta"
        # Copy fixture to tmp_path so default output goes there
        input_copy = tmp_path / "test.fasta"
        shutil.copy2(fixture, input_copy)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["compress-lossless", str(input_copy)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert (tmp_path / "test.fasta.zlfasta").exists()

    def test_force_overwrite(self, tmp_path):
        """Test -f flag allows overwriting."""
        from click.testing import CliRunner
        from nyx.cli import main

        fixture = FIXTURES_DIR / "acgt_only.fasta"
        output = tmp_path / "out.zlfasta"
        output.write_bytes(b"existing")

        runner = CliRunner()
        # Without -f: should fail
        result = runner.invoke(
            main,
            ["compress-lossless", str(fixture), "-o", str(output)],
        )
        assert result.exit_code != 0

        # With -f: should succeed
        result = runner.invoke(
            main,
            ["compress-lossless", str(fixture), "-o", str(output), "-f"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
