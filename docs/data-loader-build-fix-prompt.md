# Agent Prompt: Fix Data Loader Build

## Your Task

The `nom-data-loader` repo at `~/work/nom-data-loader/head` fails to build.
Your job is to make it compile and pass tests.

## Before You Start

1. Read the handoff document at `~/personal/compression/docs/data-loader-build-fix-handoff.md` — it explains the two independent change sets in the repo and likely causes of build failures.
2. Read `~/work/nom-data-loader/head/pom.xml` to understand the build configuration.

## Steps

1. **Try to build**: Run `mvn compile` (install Maven first if needed: `brew install maven`). Capture the full error output.
2. **Read the handoff document** at the path above for context on the two change sets.
3. **Categorize each error**: Is it from our telemetry decompression code (Change Set 1) or the pre-existing DEMUX/file-loader code (Change Set 2)?
4. **Fix each error**: Make the minimal change needed to resolve it. Do NOT remove the telemetry decompression integration (TelemetryDecompressor.java, the one-line change in InfluxDbLoadQueue.java).
5. **Run tests**: `mvn test` — ensure all tests pass, including our `TelemetryDecompressorTest`.
6. **Report**: List every change you made and why.

## Constraints

- Java 11 target
- Do NOT revert or remove TelemetryDecompressor.java or the InfluxDbLoadQueue.java change
- Do NOT introduce new features — only fix compilation/test errors
- Keep fixes minimal — if a DEMUX field is commented out but still referenced, either uncomment it or remove the reference, whichever is simpler
- The test at `src/test/groovy/com/nominum/data_loader/influxdb/TelemetryDecompressorTest.java` must compile and pass
