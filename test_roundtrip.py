"""Test round-trip correctness with single and multi-worker encoding."""
import tempfile, shutil, sys
from pathlib import Path


def main():
    import telemetry_codec

    src = Path("data/openZL-telemetru/nom-telegraf/part-005.jsonl")
    if not src.exists():
        # Try alternate paths
        for alt in [
            Path("../data/openZL-telemetru/nom-telegraf/part-005.jsonl"),
            Path("data/openZL-telemetru/nom-telegraf2/part-014.jsonl"),
            Path("../data/openZL-telemetru/nom-telegraf2/part-014.jsonl"),
        ]:
            if alt.exists():
                src = alt
                break
        else:
            print("No test data found. Provide a JSONL file path as argument.")
            sys.exit(1)

    if len(sys.argv) > 1:
        src = Path(sys.argv[1])

    schema = telemetry_codec.load_schema(
        Path("models/lossless_telemetry/telemetry_schema.json"))

    # Use 50K lines for fast testing
    with open(src) as f:
        lines = [f.readline() for _ in range(50000)]
    test_file = Path(tempfile.mktemp(suffix=".jsonl"))
    test_file.write_text("".join(lines))
    orig = test_file.read_text()
    print(f"Test file: {test_file.stat().st_size:,} bytes, {len(lines)} lines")

    all_pass = True
    for nw in [1, 2, 4, 8]:
        tmpdir = Path(tempfile.mkdtemp())
        telemetry_codec.encode(test_file, tmpdir / "tsv", schema=schema, num_workers=nw)
        out = Path(tempfile.mktemp(suffix=".jsonl"))
        telemetry_codec.decode(tmpdir / "tsv", out)
        got = out.read_text()
        if orig == got:
            print(f"  {nw} worker(s): PASS")
        else:
            all_pass = False
            orig_lines = orig.splitlines()
            got_lines = got.splitlines()
            print(f"  {nw} worker(s): FAIL ({len(orig_lines)} vs {len(got_lines)} lines)")
            diffs = 0
            for i, (a, b) in enumerate(zip(orig_lines, got_lines)):
                if a != b:
                    diffs += 1
                    if diffs <= 2:
                        print(f"    Line {i}: orig={a[:150]}...")
                        print(f"    Line {i}: got ={b[:150]}...")
            if len(orig_lines) != len(got_lines):
                print(f"    Line count diff: {len(orig_lines) - len(got_lines)}")
            print(f"    Total diffs: {diffs}")
        out.unlink(missing_ok=True)
        shutil.rmtree(tmpdir, ignore_errors=True)

    test_file.unlink(missing_ok=True)
    if all_pass:
        print("\nAll tests PASSED!")
    else:
        print("\nSome tests FAILED!")
        sys.exit(1)


if __name__ == "__main__":
    main()
