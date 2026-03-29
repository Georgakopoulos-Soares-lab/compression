# Data Loader Build Fix — Handoff Document

## Problem

The `nom-data-loader` repo at `~/work/nom-data-loader/head` fails to build.
The build system is Maven (`pom.xml`), Java 11, with Groovy-based tests compiled
via the groovy-eclipse-compiler.

## Two Independent Change Sets

There are **two unrelated sets of uncommitted changes** in this repo. Either or
both could be causing build failures.

### Change Set 1: Telemetry Decompression (ours)

**Purpose**: Add a decompression wrapper so the Data Loader can handle
ZLJSONL-compressed telemetry messages from our compression sidecar.

**Files (3 total)**:

| File | Action | Description |
|------|--------|-------------|
| `src/main/java/com/nominum/data_loader/influxdb/TelemetryDecompressor.java` | **NEW** | Static utility class. `maybeDecompress(byte[])` checks for 8-byte magic header, decompresses via external subprocess if present, passes through unchanged if not. |
| `src/main/java/com/nominum/data_loader/influxdb/InfluxDbLoadQueue.java` | **MODIFIED** | One line added at the original line 65 in `loadUnchecked()`. Changed `new Chunk(record.value(), ...)` to first pass through `TelemetryDecompressor.maybeDecompress(record.value())`. |
| `src/test/groovy/com/nominum/data_loader/influxdb/TelemetryDecompressorTest.java` | **NEW** | JUnit 4 unit tests for magic byte detection and pass-through behavior. |

**Exact diff for InfluxDbLoadQueue.java** (only our change):
```java
// BEFORE (line 65):
Chunk chunk = new Chunk(record.value(), record.partition(), record.offset());

// AFTER:
byte[] value = TelemetryDecompressor.maybeDecompress(record.value());
Chunk chunk = new Chunk(value, record.partition(), record.offset());
```

**Potential issues in our code**:

1. **`TelemetryDecompressorTest.java` is in `src/test/groovy/.../influxdb/`** —
   Existing tests (e.g., `MetricParserTest.java`) are in `src/test/groovy/com/nominum/data_loader/`
   (no `influxdb` subdirectory). If the Groovy compiler or Maven surefire plugin
   is configured to scan specific directories, our test may not compile or may
   cause classpath issues. Check Maven's test source configuration.

2. **Java API compatibility** — `TelemetryDecompressor.java` uses:
   - `Arrays.equals(byte[], int, int, byte[], int, int)` — Java 9+ API
   - `process.getInputStream().readAllBytes()` — `InputStream.readAllBytes()` is Java 9+
   - `Files.createTempFile`, `Files.write`, `Files.readAllBytes` — Java 7+
   - The project targets Java 11 (`<release>11</release>` in pom.xml), so these
     should be fine. But verify the actual compiler settings.

3. **No import needed** — `TelemetryDecompressor` is in the same package
   (`com.nominum.data_loader.influxdb`) as `InfluxDbLoadQueue`, so no import
   statement was added. This is correct Java behavior.

### Change Set 2: DEMUX / File Loader Feature (pre-existing, NOT ours)

**Purpose**: A file-loader feature that writes Kafka records to local files,
with an incomplete "demux" (per-service routing) capability that has been
partially commented out.

**Files (many)**:

| File | Action |
|------|--------|
| `.gitignore` | Modified — added `.worktrees` |
| `SConscript` | Modified — added `file-loader.schema` |
| `schema/file-loader.schema` | Modified — commented out DEMUX fields |
| `DataLoaderDaemon.java` | Modified — registers `FileLoaderConfig` handler |
| `DataLoaderConfig.java` | Modified — adds `fileLoader()` accessor |
| `FileLoaderConfig.java` | Modified — commented out `perServiceFile` field |
| `FileLoaderConfigHandler.java` | **NEW** |
| `FileLoadContext.java` | **NEW** |
| `FileLoadQueue.java` | Modified — commented out DEMUX routing, simplified constructor |
| `ServiceFileWriter.java` | **NEW** |
| `CountingOutputStream.java` | **NEW** |
| `FileLoaderStatistics.java` | Modified — commented out DEMUX stat fields |
| `FileLoaderStatisticsHandle.java` | **NEW** |
| `CountingOutputStreamTest.java` | **NEW** |
| `FileLoadQueueSingleFileTest.java` | **NEW** |
| `ServiceFileWriterTest.java` | **NEW** |

**Why this might cause build errors**:
- Many fields and methods are commented out with `/* DEMUX: ... */` and `// DEMUX:` markers
- Code that references these commented-out fields/methods will fail to compile
- The `SConscript` and `schema/file-loader.schema` changes may affect the build if
  the schema compiler runs as part of the Maven build
- The `FileLoadQueue` constructor signature changed (removed `perServiceFile` param),
  which may break tests that call the old constructor
- `FileLoaderStatistics` has commented-out methods that may be called from other code

## Repository Details

- **Path**: `~/work/nom-data-loader/head`
- **Build**: Maven — `pom.xml` at root
- **Java version**: 11 (`<release>11</release>` in pom.xml)
- **Test framework**: JUnit 4 + AssertJ, compiled via groovy-eclipse-compiler
- **Test source**: `src/test/groovy/` (note: Groovy directory, but contains `.java` files)
- **No Maven wrapper** (`mvnw`) — requires system `mvn` command
- **No `mvn` on macOS** — you may need to install it or use a workaround

## Key Files to Read

1. `pom.xml` — compiler settings, test plugin config, source directories
2. The build error output — the specific errors will tell you which change set is broken
3. `src/main/java/com/nominum/data_loader/influxdb/TelemetryDecompressor.java` — our new file
4. `src/main/java/com/nominum/data_loader/influxdb/InfluxDbLoadQueue.java` — our one-line change
5. `src/test/groovy/com/nominum/data_loader/influxdb/TelemetryDecompressorTest.java` — our test

## What to Fix

**If the errors are in our code (Change Set 1)**: Fix them directly.

**If the errors are in the DEMUX code (Change Set 2)**: These are pre-existing
changes unrelated to our work. Identify the specific compilation errors and fix
the DEMUX code to compile. Common patterns:
- Commented-out fields still referenced elsewhere → either uncomment the field or
  remove/comment out the reference
- Constructor signature mismatch → update callers to match the new constructor
- Missing methods in statistics class → uncomment the method or remove the caller

**Do NOT**:
- Remove or revert the telemetry decompression changes (Change Set 1)
- Make architectural changes to the DEMUX feature — just make it compile
- Add features or refactor beyond what's needed to fix the build
