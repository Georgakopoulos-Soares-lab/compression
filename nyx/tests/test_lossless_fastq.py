"""Tests for the lossless FASTQ compression pipeline.

Tests are organized in four tiers:
  1. TestCodecRoundTrip  — fastq_codec encode/decode only (no OpenZL needed)
  2. TestStreamInvariants — validate internal consistency of encoded streams
  3. TestZlfastqContainer — .zlfastq container create/extract (pure Python)
  4. TestFullPipeline    — full compress/decompress pipeline (requires zli)
"""

import hashlib
import shutil
import struct
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURE_NAMES = [
    "minimal.fastq",
    "multi_record.fastq",
    "wrapped_reads.fastq",
    "mixed_case.fastq",
    "iupac_ambiguity.fastq",
    "varying_quality.fastq",
    "long_reads.fastq",
    "plus_comment.fastq",
    "edge_cases.fastq",
    "crlf.fastq",
    "no_trailing_newline.fastq",
    "illumina_reads.fastq",
    "illumina_multi_instrument.fastq",
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _zli_available() -> bool:
    try:
        from nyx.utils.paths import find_zli
        find_zli()
        return True
    except (FileNotFoundError, ImportError):
        return False


def _fastq_codec_available() -> bool:
    try:
        from nyx.utils.paths import find_fastq_codec
        find_fastq_codec()
        return True
    except (FileNotFoundError, ImportError):
        return False


requires_fastq_codec = pytest.mark.skipif(
    not _fastq_codec_available(),
    reason="fastq_codec binary not found (run 'nyx build' first)",
)

requires_zli = pytest.mark.skipif(
    not _zli_available(),
    reason="zli binary not found (run 'nyx build' first)",
)


# =============================================================================
# Stream invariant helpers (pure Python)
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


def validate_fastq_streams(streams_dir: Path) -> list:
    """Validate internal consistency of encoded FASTQ stream files.

    Returns a list of error strings. Empty list means all invariants pass.
    """
    errors = []
    meta_path = streams_dir / "meta.bin"
    if not meta_path.exists():
        return ["meta.bin not found"]

    meta = meta_path.read_bytes()
    if len(meta) < 20:
        return ["meta.bin too small for header"]

    magic = struct.unpack_from("<I", meta, 0)[0]
    if magic != 0x4E584651:  # "NXFQ"
        errors.append(f"Bad magic: 0x{magic:08X} (expected 0x4E584651)")
        return errors

    version = struct.unpack_from("<I", meta, 4)[0]
    num_records = struct.unpack_from("<I", meta, 8)[0]
    newline_style = meta[12]
    has_trailing_nl = meta[13]

    if version not in (1, 2, 3):
        errors.append(f"Unsupported version: {version}")
    if newline_style not in (0, 1):
        errors.append(f"Invalid newline_style: {newline_style}")
    if has_trailing_nl not in (0, 1):
        errors.append(f"Invalid has_trailing_newline: {has_trailing_nl}")

    # Compute global header size based on version
    meta_mode = 0  # META_FULL
    wrap_mode = 0  # WRAPMODE_PER_RECORD (default for v1/v2)
    if version in (2, 3):
        quality_layout = meta[14]
        mode_byte = meta[15]
        # v3: byte [15] packs (meta_mode << 4) | header_mode
        header_mode = mode_byte & 0x0F
        meta_mode = (mode_byte >> 4) & 0x0F
        fixed_seq_len = struct.unpack_from("<I", meta, 16)[0]
        if quality_layout not in (0, 1, 2, 3):
            errors.append(f"Invalid quality_layout: {quality_layout}")

        if header_mode == 0:  # LCP mode
            prefix_len = struct.unpack_from("<I", meta, 20)[0]
            GLOBAL_HEADER_SIZE = 24 + prefix_len
        elif header_mode == 1:  # Illumina mode
            illumina_block_size = struct.unpack_from("<I", meta, 20)[0]
            GLOBAL_HEADER_SIZE = 24 + illumina_block_size
        else:
            errors.append(f"Invalid header_mode: {header_mode}")
            GLOBAL_HEADER_SIZE = 24

        # Parse wrap_mode (v3+)
        if version >= 3 and len(meta) > GLOBAL_HEADER_SIZE:
            wrap_mode = meta[GLOBAL_HEADER_SIZE]
            GLOBAL_HEADER_SIZE += 1  # wrap_mode byte
            if wrap_mode == 1:  # WRAPMODE_CONSTANT
                # Skip constant wrapping data: u32 sw_len + sw_data + u32 qw_len + qw_data
                if GLOBAL_HEADER_SIZE + 4 <= len(meta):
                    sw_len = struct.unpack_from("<I", meta, GLOBAL_HEADER_SIZE)[0]
                    GLOBAL_HEADER_SIZE += 4 + sw_len
                if GLOBAL_HEADER_SIZE + 4 <= len(meta):
                    qw_len = struct.unpack_from("<I", meta, GLOBAL_HEADER_SIZE)[0]
                    GLOBAL_HEADER_SIZE += 4 + qw_len
    else:
        GLOBAL_HEADER_SIZE = 20
        quality_layout = None

    RECORD_META_SIZE = 48
    if meta_mode == 1:  # META_COMPACT
        expected_meta_size = GLOBAL_HEADER_SIZE + RECORD_META_SIZE + num_records * 4
    else:
        expected_meta_size = GLOBAL_HEADER_SIZE + num_records * RECORD_META_SIZE
    if len(meta) < expected_meta_size:
        errors.append(
            f"meta.bin too small: {len(meta)} bytes, "
            f"expected >= {expected_meta_size} for {num_records} records "
            f"(meta_mode={meta_mode})"
        )
        return errors

    # Read stream files
    stream_names = [
        "headers.bin", "plus.bin", "nmask.bin", "acgtmask.bin",
        "bases2.bin", "exceptions.bin", "case.bin", "seq_wrap.bin",
        "qual_wrap.bin",
    ]
    # quality.bin or quality_pos_*.bin depending on layout
    has_per_position = (version in (2, 3) and quality_layout in (2, 3))
    if not has_per_position:
        stream_names.append("quality.bin")

    streams = {}
    for name in stream_names:
        p = streams_dir / name
        if p.exists():
            streams[name] = p.read_bytes()
        else:
            streams[name] = b""

    # Validate per-position quality files for layout=2
    if has_per_position and fixed_seq_len > 0:
        import re
        pos_pattern = re.compile(r"^quality_pos_(\d{4})\.bin$")
        pos_files = sorted(
            f for f in streams_dir.iterdir()
            if pos_pattern.match(f.name)
        )
        if len(pos_files) != fixed_seq_len:
            errors.append(
                f"Expected {fixed_seq_len} quality_pos_*.bin files, found {len(pos_files)}"
            )
        for pf in pos_files:
            pf_size = pf.stat().st_size
            if pf_size != num_records:
                errors.append(
                    f"{pf.name}: size={pf_size}, expected={num_records} records"
                )

    cursors = {name: 0 for name in streams}

    # Pre-parse per-record metas (handle compact mode)
    if meta_mode == 1 and num_records > 0:  # META_COMPACT
        tmpl_off = GLOBAL_HEADER_SIZE
        tmpl_fields = struct.unpack_from("<11I2B2B", meta, tmpl_off)
        hlen_base = GLOBAL_HEADER_SIZE + RECORD_META_SIZE
        record_metas = []
        for r in range(num_records):
            hl = struct.unpack_from("<I", meta, hlen_base + r * 4)[0]
            record_metas.append((*tmpl_fields[:1], hl, *tmpl_fields[2:]))
    else:
        record_metas = []
        for r in range(num_records):
            off = GLOBAL_HEADER_SIZE + r * RECORD_META_SIZE
            fields = struct.unpack_from("<11I2B2B", meta, off)
            record_metas.append(fields)

    for r in range(num_records):
        fields = record_metas[r]
        seq_len = fields[0]
        header_len = fields[1]
        plus_len = fields[2]
        nmask_bytes = fields[3]
        acgtmask_bytes = fields[4]
        bases2_bytes = fields[5]
        exceptions_bytes = fields[6]
        case_bytes = fields[7]
        seq_wrap_bytes = fields[8]
        qual_wrap_bytes = fields[9]
        quality_bytes = fields[10]
        case_mode = fields[11]
        quality_mode = fields[12]

        L = seq_len
        pfx = f"record[{r}]"

        # Header bounds
        if cursors["headers.bin"] + header_len > len(streams["headers.bin"]):
            errors.append(f"{pfx}: header overflows headers.bin")
        cursors["headers.bin"] += header_len

        # Plus bounds
        if cursors["plus.bin"] + plus_len > len(streams["plus.bin"]):
            errors.append(f"{pfx}: plus overflows plus.bin")
        cursors["plus.bin"] += plus_len

        # N-mask size
        expected_nmask = (L + 7) // 8
        if nmask_bytes != expected_nmask:
            errors.append(
                f"{pfx}: nmask_bytes={nmask_bytes}, expected={expected_nmask}"
            )

        # Compute L' from N-mask
        nm_start = cursors["nmask.bin"]
        Lp = None
        if nm_start + nmask_bytes <= len(streams["nmask.bin"]):
            nmask_data = streams["nmask.bin"][nm_start:nm_start + nmask_bytes]
            count_n = _popcount_bits(nmask_data, L)
            Lp = L - count_n
        else:
            errors.append(f"{pfx}: nmask overflows nmask.bin")
        cursors["nmask.bin"] += nmask_bytes

        # ACGT-mask size
        if Lp is not None:
            expected_am = (Lp + 7) // 8
            if acgtmask_bytes != expected_am:
                errors.append(
                    f"{pfx}: acgtmask_bytes={acgtmask_bytes}, expected={expected_am}"
                )

        # Count ACGT
        am_start = cursors["acgtmask.bin"]
        count_acgt = None
        if Lp is not None and am_start + acgtmask_bytes <= len(streams["acgtmask.bin"]):
            am_data = streams["acgtmask.bin"][am_start:am_start + acgtmask_bytes]
            count_acgt = _popcount_bits(am_data, Lp)
        cursors["acgtmask.bin"] += acgtmask_bytes

        # bases2 size
        if count_acgt is not None:
            expected_b2 = (count_acgt + 3) // 4
            if bases2_bytes != expected_b2:
                errors.append(
                    f"{pfx}: bases2_bytes={bases2_bytes}, expected={expected_b2}"
                )
        cursors["bases2.bin"] += bases2_bytes

        # Exceptions count
        if count_acgt is not None and Lp is not None:
            expected_exc = Lp - count_acgt
            ex_start = cursors["exceptions.bin"]
            ex_data = streams["exceptions.bin"][ex_start:ex_start + exceptions_bytes]
            actual_exc = 0
            ex_off = 0
            while ex_off < len(ex_data):
                try:
                    _val, ex_off = _decode_varint(ex_data, ex_off)
                except ValueError:
                    errors.append(f"{pfx}: truncated varint in exceptions")
                    break
                if ex_off < len(ex_data):
                    ex_off += 1
                    actual_exc += 1
                else:
                    errors.append(f"{pfx}: exception missing symbol byte")
                    break
            if actual_exc != expected_exc:
                errors.append(
                    f"{pfx}: exceptions count={actual_exc}, expected={expected_exc}"
                )
        cursors["exceptions.bin"] += exceptions_bytes

        # Case mode
        if case_mode == 0:
            if case_bytes != 0:
                errors.append(f"{pfx}: case_mode=NONE but case_bytes={case_bytes}")
        elif case_mode == 1:
            expected_cs = (L + 7) // 8
            if case_bytes != expected_cs:
                errors.append(
                    f"{pfx}: case_mode=MASK, case_bytes={case_bytes}, expected={expected_cs}"
                )
        elif case_mode == 2:
            if case_bytes == 0:
                errors.append(f"{pfx}: case_mode=SPARSE but case_bytes=0")
        elif case_mode > 2:
            errors.append(f"{pfx}: unknown case_mode={case_mode}")
        cursors["case.bin"] += case_bytes

        # Wrapping validation helper
        def _validate_wrapping(wrap_name, wrap_bytes_val, label):
            wr_start = cursors[wrap_name]
            wr_data = streams[wrap_name][wr_start:wr_start + wrap_bytes_val]
            if len(wr_data) > 0:
                wrap_mode = wr_data[0]
                if wrap_mode == 0x01:  # COMPACT
                    if len(wr_data) < 9:
                        errors.append(f"{pfx}: {label} COMPACT data too short")
                    else:
                        width = struct.unpack_from("<I", wr_data, 1)[0]
                        last_len = struct.unpack_from("<I", wr_data, 5)[0]
                        if width == 0:
                            total = last_len
                        elif last_len == width:
                            total = (L // width) * width
                        else:
                            full_lines = (L - last_len) // width if L > width else 0
                            total = full_lines * width + last_len
                        if total != L:
                            errors.append(
                                f"{pfx}: {label} COMPACT total={total} != L={L}"
                            )
                elif wrap_mode == 0x02:  # EXPLICIT
                    if len(wr_data) < 5:
                        errors.append(f"{pfx}: {label} EXPLICIT data too short")
                    else:
                        num_lines = struct.unpack_from("<I", wr_data, 1)[0]
                        total = 0
                        for k in range(num_lines):
                            if 5 + (k + 1) * 4 <= len(wr_data):
                                ll = struct.unpack_from("<I", wr_data, 5 + k * 4)[0]
                                total += ll
                        if total != L:
                            errors.append(
                                f"{pfx}: {label} EXPLICIT sum={total} != L={L}"
                            )
                else:
                    errors.append(f"{pfx}: {label} unknown wrap_mode=0x{wrap_mode:02X}")
            elif L > 0:
                errors.append(f"{pfx}: non-empty seq but {label} wrap_bytes=0")
            cursors[wrap_name] += wrap_bytes_val

        if wrap_mode == 1:  # WRAPMODE_CONSTANT
            if seq_wrap_bytes != 0:
                errors.append(f"{pfx}: constant wrap but seq_wrap_bytes={seq_wrap_bytes}")
            if qual_wrap_bytes != 0:
                errors.append(f"{pfx}: constant wrap but qual_wrap_bytes={qual_wrap_bytes}")
        else:
            _validate_wrapping("seq_wrap.bin", seq_wrap_bytes, "seq")
            _validate_wrapping("qual_wrap.bin", qual_wrap_bytes, "qual")

        # Quality bytes: per-record layout stores L bytes per record;
        # columnar/per-position layout stores quality separately (validated after loop)
        if version in (2, 3) and quality_layout in (1, 2, 3):
            # Columnar/per-position: quality_bytes in meta should still be L
            if quality_bytes != L:
                errors.append(f"{pfx}: quality_bytes={quality_bytes} != seq_len={L}")
            # Don't advance cursor — quality is validated globally
        else:
            if quality_bytes != L:
                errors.append(f"{pfx}: quality_bytes={quality_bytes} != seq_len={L}")
            cursors["quality.bin"] += quality_bytes

        # Quality mode
        if version in (2, 3):
            if quality_mode != 0:
                errors.append(f"{pfx}: v2/v3 expects quality_mode=0 (raw), got {quality_mode}")
        elif quality_mode not in (0, 1):
            errors.append(f"{pfx}: unknown quality_mode={quality_mode}")

    # Columnar quality: total size should be num_records * fixed_seq_len
    if version in (2, 3) and quality_layout == 1:
        expected_qual_size = num_records * fixed_seq_len
        actual_qual_size = len(streams["quality.bin"])
        if actual_qual_size != expected_qual_size:
            errors.append(
                f"quality.bin columnar size={actual_qual_size}, "
                f"expected={expected_qual_size} (N={num_records} × L={fixed_seq_len})"
            )
        cursors["quality.bin"] = actual_qual_size  # mark as fully consumed
    # Per-position quality (layout=2): already validated above (file count + sizes)

    # Verify all streams consumed exactly
    for name, cursor in cursors.items():
        actual_len = len(streams[name])
        if cursor != actual_len:
            errors.append(
                f"{name}: consumed {cursor} bytes but file has {actual_len}"
            )

    return errors


# =============================================================================
# Tier 1: fastq_codec encode -> decode roundtrip
# =============================================================================


@requires_fastq_codec
class TestCodecRoundTrip:
    """Test that fastq_codec encode -> decode produces byte-identical output."""

    @pytest.fixture(params=FIXTURE_NAMES)
    def fastq_fixture(self, request):
        return FIXTURES_DIR / request.param

    def test_roundtrip(self, fastq_fixture, tmp_path):
        from nyx.core.fastq_codec import encode, decode

        streams_dir = tmp_path / "streams"
        output = tmp_path / "reconstructed.fastq"

        encode(fastq_fixture, streams_dir)
        decode(streams_dir, output)

        assert _sha256(fastq_fixture) == _sha256(output), (
            f"Roundtrip failed for {fastq_fixture.name}: files differ"
        )

    def test_stream_files_created(self, fastq_fixture, tmp_path):
        import re
        from nyx.core.fastq_codec import encode

        streams_dir = tmp_path / "streams"
        stream_files = encode(fastq_fixture, streams_dir)

        base_names = {
            "meta.bin", "headers.bin", "plus.bin", "nmask.bin",
            "acgtmask.bin", "bases2.bin", "exceptions.bin", "case.bin",
            "seq_wrap.bin", "qual_wrap.bin",
        }
        actual_names = {f.name for f in stream_files}

        # Accept either quality.bin (per-record/columnar) or quality_pos_*.bin (per-position)
        pos_pattern = re.compile(r"^quality_pos_\d{4}\.bin$")
        has_quality_bin = "quality.bin" in actual_names
        has_quality_pos = any(pos_pattern.match(n) for n in actual_names)

        assert has_quality_bin or has_quality_pos, (
            f"Neither quality.bin nor quality_pos_*.bin found in {actual_names}"
        )

        # Check base streams are present
        non_quality = actual_names - {n for n in actual_names
                                       if n == "quality.bin" or pos_pattern.match(n)}
        assert non_quality == base_names, (
            f"Missing base streams: {base_names - non_quality}"
        )


@requires_fastq_codec
class TestEdgeCases:
    """Test edge cases for the FASTQ codec."""

    def _roundtrip(self, content: bytes, tmp_path: Path) -> bool:
        from nyx.core.fastq_codec import encode, decode

        input_file = tmp_path / "input.fastq"
        with open(input_file, "wb") as f:
            f.write(content)

        streams_dir = tmp_path / "streams"
        output = tmp_path / "output.fastq"

        encode(input_file, streams_dir)
        decode(streams_dir, output)

        return _sha256(input_file) == _sha256(output)

    def test_single_base_read(self, tmp_path):
        content = b"@r1\nA\n+\nI\n"
        assert self._roundtrip(content, tmp_path)

    def test_all_n_sequence(self, tmp_path):
        content = b"@r1\nNNNN\n+\nIIII\n"
        assert self._roundtrip(content, tmp_path)

    def test_all_lowercase(self, tmp_path):
        content = b"@r1\nacgtacgt\n+\nIIIIIIII\n"
        assert self._roundtrip(content, tmp_path)

    def test_iupac_codes(self, tmp_path):
        content = b"@r1\nACGTRYWSMKHBVDN\n+\nIIIIIIIIIIIIIII\n"
        assert self._roundtrip(content, tmp_path)

    def test_plus_with_comment(self, tmp_path):
        content = b"@read1 desc\nACGT\n+read1 desc\nIIII\n"
        assert self._roundtrip(content, tmp_path)

    def test_multi_line_seq_and_qual(self, tmp_path):
        content = b"@r1\nACGT\nACGT\n+\nIIII\nFFFF\n"
        assert self._roundtrip(content, tmp_path)

    def test_low_quality(self, tmp_path):
        content = b"@r1\nACGT\n+\n!!!!\n"
        assert self._roundtrip(content, tmp_path)

    def test_high_quality(self, tmp_path):
        content = b"@r1\nACGT\n+\n~~~~\n"
        assert self._roundtrip(content, tmp_path)

    def test_quality_gradient(self, tmp_path):
        content = b"@r1\nACGTACGTACGT\n+\n!#%')+/5;AIS\n"
        assert self._roundtrip(content, tmp_path)

    def test_many_records(self, tmp_path):
        records = b""
        for i in range(50):
            records += f"@read_{i}\n".encode()
            records += b"ACGTACGT\n"
            records += b"+\n"
            records += b"IIIIIIII\n"
        assert self._roundtrip(records, tmp_path)


# =============================================================================
# Tier 2: Stream invariant validation
# =============================================================================


@requires_fastq_codec
class TestStreamInvariants:
    """Validate internal consistency of encoded FASTQ stream files."""

    @pytest.fixture(params=FIXTURE_NAMES)
    def fastq_fixture(self, request):
        return FIXTURES_DIR / request.param

    def test_invariants(self, fastq_fixture, tmp_path):
        from nyx.core.fastq_codec import encode

        streams_dir = tmp_path / "streams"
        encode(fastq_fixture, streams_dir)

        errors = validate_fastq_streams(streams_dir)
        assert errors == [], (
            f"Stream invariant failures for {fastq_fixture.name}:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    def test_invariants_edge_cases(self, tmp_path):
        from nyx.core.fastq_codec import encode

        cases = {
            "single_base": b"@r\nA\n+\nI\n",
            "all_n": b"@r\nNNNN\n+\nIIII\n",
            "iupac": b"@r\nACGTRYWS\n+\nIIIIIIII\n",
            "plus_comment": b"@r desc\nACGT\n+r desc\nIIII\n",
        }

        for name, content in cases.items():
            d = tmp_path / name
            d.mkdir()
            input_file = d / "input.fastq"
            input_file.write_bytes(content)

            streams_dir = d / "streams"
            encode(input_file, streams_dir)

            errors = validate_fastq_streams(streams_dir)
            assert errors == [], (
                f"Stream invariant failures for edge case '{name}':\n"
                + "\n".join(f"  - {e}" for e in errors)
            )


# =============================================================================
# Tier 3: .zlfastq container roundtrip
# =============================================================================


class TestZlfastqContainer:
    """Test the .zlfastq container format."""

    def test_create_and_extract(self, tmp_path):
        from nyx.core.zlfastq import create_zlfastq, extract_zlfastq

        entries = {
            "meta.bin": (b"metadata_content", 16),
            "data.bin": (b"compressed_blob_here", 100),
        }

        container_path = tmp_path / "test.zlfastq"
        create_zlfastq(container_path, entries)

        extract_dir = tmp_path / "extracted"
        result = extract_zlfastq(container_path, extract_dir)

        assert set(result.keys()) == {"meta.bin", "data.bin"}
        assert (extract_dir / "meta.bin").read_bytes() == b"metadata_content"
        assert (extract_dir / "data.bin").read_bytes() == b"compressed_blob_here"

    def test_crc32_validation(self, tmp_path):
        from nyx.core.zlfastq import create_zlfastq, extract_zlfastq, ZlfastqError

        entries = {"test.bin": (b"hello", 5)}
        container_path = tmp_path / "test.zlfastq"
        create_zlfastq(container_path, entries)

        data = bytearray(container_path.read_bytes())
        data[20] ^= 0xFF
        container_path.write_bytes(bytes(data))

        with pytest.raises(ZlfastqError, match="CRC32 mismatch"):
            extract_zlfastq(container_path, tmp_path / "bad")

    def test_bad_magic(self, tmp_path):
        from nyx.core.zlfastq import extract_zlfastq, ZlfastqError

        container_path = tmp_path / "bad.zlfastq"
        container_path.write_bytes(b"BADMAGIC" + b"\x00" * 20)

        with pytest.raises(ZlfastqError, match="Bad magic"):
            extract_zlfastq(container_path, tmp_path / "out")

    def test_empty_entries(self, tmp_path):
        from nyx.core.zlfastq import create_zlfastq, extract_zlfastq

        entries = {"empty.bin": (b"", 0)}
        container_path = tmp_path / "empty.zlfastq"
        create_zlfastq(container_path, entries)

        result = extract_zlfastq(container_path, tmp_path / "out")
        assert (tmp_path / "out" / "empty.bin").read_bytes() == b""


# =============================================================================
# Tier 4: Full pipeline (requires zli)
# =============================================================================


@requires_fastq_codec
@requires_zli
class TestFullPipeline:
    """Test the full compress-lossless-fastq -> decompress-lossless-fastq pipeline."""

    @pytest.fixture(params=FIXTURE_NAMES)
    def fastq_fixture(self, request):
        return FIXTURES_DIR / request.param

    def test_roundtrip(self, fastq_fixture, tmp_path):
        from click.testing import CliRunner
        from nyx.cli import main

        runner = CliRunner()
        compressed = tmp_path / "compressed.zlfastq"
        decompressed = tmp_path / "decompressed.fastq"

        result = runner.invoke(
            main,
            ["compress", str(fastq_fixture), "-o", str(compressed)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, f"Compress failed:\n{result.output}"
        assert compressed.exists()

        result = runner.invoke(
            main,
            ["decompress", str(compressed), "-o", str(decompressed), "-f"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, f"Decompress failed:\n{result.output}"
        assert decompressed.exists()

        assert _sha256(fastq_fixture) == _sha256(decompressed), (
            f"Full pipeline roundtrip failed for {fastq_fixture.name}"
        )

    def test_output_default_name(self, tmp_path):
        from click.testing import CliRunner
        from nyx.cli import main

        fixture = FIXTURES_DIR / "minimal.fastq"
        input_copy = tmp_path / "test.fastq"
        shutil.copy2(fixture, input_copy)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["compress", str(input_copy)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert (tmp_path / "test.fastq.zlfastq").exists()

    def test_force_overwrite(self, tmp_path):
        from click.testing import CliRunner
        from nyx.cli import main

        fixture = FIXTURES_DIR / "minimal.fastq"
        output = tmp_path / "out.zlfastq"
        output.write_bytes(b"existing")

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["compress", str(fixture), "-o", str(output)],
        )
        assert result.exit_code != 0

        result = runner.invoke(
            main,
            ["compress", str(fixture), "-o", str(output), "-f"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0


# =============================================================================
# Tier 5: Packed NQF codec roundtrip
# =============================================================================


@requires_fastq_codec
class TestPackedCodecRoundTrip:
    """Test that fastq_codec encode-packed -> decode-packed produces byte-identical output."""

    @pytest.fixture(params=FIXTURE_NAMES)
    def fastq_fixture(self, request):
        return FIXTURES_DIR / request.param

    def test_roundtrip(self, fastq_fixture, tmp_path):
        from nyx.core.fastq_codec import encode_packed, decode_packed

        packed_dir = tmp_path / "packed"
        output = tmp_path / "reconstructed.fastq"

        encode_packed(fastq_fixture, packed_dir)
        decode_packed(packed_dir, output)

        assert _sha256(fastq_fixture) == _sha256(output), (
            f"Packed roundtrip failed for {fastq_fixture.name}: files differ"
        )

    def test_chunk_files_created(self, fastq_fixture, tmp_path):
        from nyx.core.fastq_codec import encode_packed

        packed_dir = tmp_path / "packed"
        chunk_files = encode_packed(fastq_fixture, packed_dir)

        assert len(chunk_files) >= 1
        for cf in chunk_files:
            assert cf.name.startswith("chunk_")
            assert cf.name.endswith(".bin")
            assert cf.stat().st_size > 0


@requires_fastq_codec
class TestNQFVariantSelection:
    """Test that NQF variant auto-detection selects the right format."""

    def _get_magic(self, chunk_path):
        with open(chunk_path, "rb") as f:
            return f.read(4)

    def test_illumina_fixed_is_nqf1(self, tmp_path):
        from nyx.core.fastq_codec import encode_packed

        packed_dir = tmp_path / "packed"
        chunks = encode_packed(FIXTURES_DIR / "illumina_reads.fastq", packed_dir)
        assert self._get_magic(chunks[0]) == b"NQF1"

    def test_illumina_multi_instrument_is_nqf1(self, tmp_path):
        from nyx.core.fastq_codec import encode_packed

        packed_dir = tmp_path / "packed"
        chunks = encode_packed(
            FIXTURES_DIR / "illumina_multi_instrument.fastq", packed_dir)
        assert self._get_magic(chunks[0]) == b"NQF1"

    def test_generic_is_nqf3(self, tmp_path):
        from nyx.core.fastq_codec import encode_packed

        packed_dir = tmp_path / "packed"
        chunks = encode_packed(FIXTURES_DIR / "multi_record.fastq", packed_dir)
        assert self._get_magic(chunks[0]) == b"NQF3"

    def test_minimal_is_nqf3(self, tmp_path):
        from nyx.core.fastq_codec import encode_packed

        packed_dir = tmp_path / "packed"
        chunks = encode_packed(FIXTURES_DIR / "minimal.fastq", packed_dir)
        assert self._get_magic(chunks[0]) == b"NQF3"


@requires_fastq_codec
class TestPackedInvariants:
    """Validate internal consistency of packed NQF binary files."""

    @pytest.fixture(params=FIXTURE_NAMES)
    def fastq_fixture(self, request):
        return FIXTURES_DIR / request.param

    def test_header_invariants(self, fastq_fixture, tmp_path):
        from nyx.core.fastq_codec import encode_packed

        packed_dir = tmp_path / "packed"
        chunks = encode_packed(fastq_fixture, packed_dir)

        for chunk_path in chunks:
            data = chunk_path.read_bytes()
            assert len(data) >= 80, "Chunk too small for header"

            magic = data[0:4]
            assert magic in (b"NQF1", b"NQF2", b"NQF3"), f"Bad magic: {magic!r}"

            version = struct.unpack_from("<I", data, 4)[0]
            assert version in (1, 2), f"Bad version: {version}"

            num_records = struct.unpack_from("<I", data, 8)[0]
            assert num_records > 0, "Zero records"

            newline_style = data[12]
            assert newline_style in (0, 1), f"Bad newline_style: {newline_style}"

            has_trailing = data[13]
            assert has_trailing in (0, 1), f"Bad has_trailing: {has_trailing}"

            stream_flags = data[14]
            assert stream_flags < 8, f"Bad stream_flags: {stream_flags}"

            # Verify totals add up
            info_block_size = struct.unpack_from("<I", data, 20)[0]
            total_hdr = struct.unpack_from("<I", data, 24)[0]
            total_plus = struct.unpack_from("<I", data, 28)[0]
            total_nmask = struct.unpack_from("<I", data, 32)[0]
            total_acgt = struct.unpack_from("<I", data, 36)[0]
            total_bases = struct.unpack_from("<I", data, 40)[0]
            total_exc = struct.unpack_from("<I", data, 44)[0]
            total_case = struct.unpack_from("<I", data, 48)[0]
            total_seq_wr = struct.unpack_from("<I", data, 52)[0]
            total_qual_wr = struct.unpack_from("<I", data, 56)[0]
            total_quality = struct.unpack_from("<I", data, 60)[0]

            # Expected size: header + info + metadata + all payloads
            # v2 uses compact 8-byte metadata for NQF1, v1 uses 44-byte
            meta_per_record = 8 if (version == 2 and magic == b"NQF1") else 44
            expected_size = (80 + info_block_size + num_records * meta_per_record
                            + total_hdr + total_plus + total_nmask + total_acgt
                            + total_bases + total_exc + total_case
                            + total_seq_wr + total_qual_wr + total_quality)
            assert len(data) == expected_size, (
                f"Size mismatch: actual={len(data)}, expected={expected_size}"
            )


@requires_fastq_codec
class TestPackedMultiChunk:
    """Test packed codec with multiple chunks."""

    def test_multi_chunk_roundtrip(self, tmp_path):
        from nyx.core.fastq_codec import encode_packed, decode_packed

        fixture = FIXTURES_DIR / "illumina_reads.fastq"
        packed_dir = tmp_path / "packed"
        output = tmp_path / "reconstructed.fastq"

        chunks = encode_packed(fixture, packed_dir, num_chunks=3)
        assert len(chunks) == 3

        decode_packed(packed_dir, output)
        assert _sha256(fixture) == _sha256(output)

    def test_multi_chunk_generic(self, tmp_path):
        from nyx.core.fastq_codec import encode_packed, decode_packed

        fixture = FIXTURES_DIR / "varying_quality.fastq"
        packed_dir = tmp_path / "packed"
        output = tmp_path / "reconstructed.fastq"

        chunks = encode_packed(fixture, packed_dir, num_chunks=2)
        assert len(chunks) == 2

        decode_packed(packed_dir, output)
        assert _sha256(fixture) == _sha256(output)


# =============================================================================
# Tier 7: CSV codec roundtrip
# =============================================================================


@requires_fastq_codec
class TestCSVCodecRoundTrip:
    """Test that fastq_codec encode-csv -> decode-csv produces byte-identical output."""

    @pytest.fixture(params=FIXTURE_NAMES)
    def fastq_fixture(self, request):
        return FIXTURES_DIR / request.param

    def test_roundtrip(self, fastq_fixture, tmp_path):
        from nyx.core.fastq_codec import encode_csv, decode_csv

        csv_dir = tmp_path / "csv"
        output = tmp_path / "reconstructed.fastq"

        encode_csv(fastq_fixture, csv_dir)
        decode_csv(csv_dir, output)

        assert _sha256(fastq_fixture) == _sha256(output), (
            f"CSV roundtrip failed for {fastq_fixture.name}: files differ"
        )

    def test_output_files_created(self, fastq_fixture, tmp_path):
        from nyx.core.fastq_codec import encode_csv

        csv_dir = tmp_path / "csv"
        files = encode_csv(fastq_fixture, csv_dir)

        names = {f.name for f in files}
        assert "meta.bin" in names, "meta.bin not produced"
        assert any(n.startswith("part_") and n.endswith(".tsv") for n in names), (
            "No part_*.tsv files produced"
        )


@requires_fastq_codec
class TestCSVMultiPart:
    """Test CSV codec with multiple TSV parts."""

    def test_multi_part_illumina(self, tmp_path):
        from nyx.core.fastq_codec import encode_csv, decode_csv

        fixture = FIXTURES_DIR / "illumina_reads.fastq"
        csv_dir = tmp_path / "csv"
        output = tmp_path / "reconstructed.fastq"

        files = encode_csv(fixture, csv_dir, num_parts=3)
        tsv_parts = [f for f in files if f.name.startswith("part_")]
        assert len(tsv_parts) == 3

        decode_csv(csv_dir, output)
        assert _sha256(fixture) == _sha256(output)

    def test_multi_part_generic(self, tmp_path):
        from nyx.core.fastq_codec import encode_csv, decode_csv

        fixture = FIXTURES_DIR / "varying_quality.fastq"
        csv_dir = tmp_path / "csv"
        output = tmp_path / "reconstructed.fastq"

        files = encode_csv(fixture, csv_dir, num_parts=2)
        tsv_parts = [f for f in files if f.name.startswith("part_")]
        assert len(tsv_parts) == 2

        decode_csv(csv_dir, output)
        assert _sha256(fixture) == _sha256(output)


@requires_fastq_codec
class TestCSVIlluminaDetection:
    """Test that Illumina fixtures produce 8-column TSV and generic produce 3-column."""

    def test_illumina_8_columns(self, tmp_path):
        from nyx.core.fastq_codec import encode_csv

        csv_dir = tmp_path / "csv"
        encode_csv(FIXTURES_DIR / "illumina_reads.fastq", csv_dir)

        tsv = next(csv_dir.glob("part_*.tsv"))
        first_line = tsv.read_text().split("\n")[0]
        cols = first_line.split("\t")
        assert len(cols) >= 8, f"Expected >= 8 columns for Illumina, got {len(cols)}"

    def test_generic_3_columns(self, tmp_path):
        from nyx.core.fastq_codec import encode_csv

        csv_dir = tmp_path / "csv"
        encode_csv(FIXTURES_DIR / "multi_record.fastq", csv_dir)

        tsv = next(csv_dir.glob("part_*.tsv"))
        first_line = tsv.read_text().split("\n")[0]
        cols = first_line.split("\t")
        assert len(cols) == 3, f"Expected 3 columns for generic, got {len(cols)}"


# =============================================================================
# Tier 8: Full pipeline with CSV format (default, requires zli)
# =============================================================================


@requires_fastq_codec
@requires_zli
class TestFullPipelineCSV:
    """Test full compress -> decompress pipeline with CSV format (default)."""

    @pytest.fixture(params=FIXTURE_NAMES)
    def fastq_fixture(self, request):
        return FIXTURES_DIR / request.param

    def test_roundtrip(self, fastq_fixture, tmp_path):
        from click.testing import CliRunner
        from nyx.cli import main

        runner = CliRunner()
        compressed = tmp_path / "compressed.zlfastq"
        decompressed = tmp_path / "decompressed.fastq"

        result = runner.invoke(
            main,
            ["compress", str(fastq_fixture),
             "-o", str(compressed)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, f"Compress failed:\n{result.output}"
        assert compressed.exists()

        result = runner.invoke(
            main,
            ["decompress", str(compressed),
             "-o", str(decompressed), "-f"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, f"Decompress failed:\n{result.output}"
        assert decompressed.exists()

        assert _sha256(fastq_fixture) == _sha256(decompressed), (
            f"Full pipeline CSV roundtrip failed for {fastq_fixture.name}"
        )


