// DNS TSV compression benchmark: OpenZL (trained) vs zstd-9 vs Snappy
// All compressors run in-process — no subprocess overhead.
// Tracks wall time, CPU time (user+sys via getrusage), ratio, and throughput.
// Supports parallel compression/decompression via chunking.
//
// Usage: benchmark <data_dir> <compressor.zl_compressor>
//          [--runs N] [--sizes 1,2,5,10,20,50] [--threads N] [--csv out.csv]

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <sys/resource.h>  // getrusage

// OpenZL
#include "openzl/cpp/CCtx.hpp"
#include "openzl/cpp/DCtx.hpp"
#include "openzl/cpp/Compressor.hpp"
#include "openzl/zl_compress.h"
#include "openzl/zl_decompress.h"
#include "openzl/zl_version.h"
#include "openzl/zl_errors.h"
#include "custom_parsers/dependency_registration.h"

// zstd
#include <zstd.h>

// snappy
#include <snappy.h>

namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;

// ---------------------------------------------------------------
// CPU time (process-wide user+sys)
// ---------------------------------------------------------------

static double get_cpu_time() {
    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    return (ru.ru_utime.tv_sec + ru.ru_utime.tv_usec / 1e6)
         + (ru.ru_stime.tv_sec + ru.ru_stime.tv_usec / 1e6);
}

// ---------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------

static std::string read_file(const std::string& path) {
    std::ifstream f(path, std::ios::binary | std::ios::ate);
    if (!f) { std::cerr << "Cannot open: " << path << "\n"; std::exit(1); }
    auto sz = f.tellg();
    f.seekg(0);
    std::string buf(sz, '\0');
    f.read(buf.data(), sz);
    return buf;
}

static std::string human_size(size_t n) {
    char buf[64];
    if (n < 1024) std::snprintf(buf, sizeof(buf), "%zu B", n);
    else if (n < 1024*1024) std::snprintf(buf, sizeof(buf), "%.1f KiB", n / 1024.0);
    else if (n < 1024ULL*1024*1024) std::snprintf(buf, sizeof(buf), "%.1f MiB", n / (1024.0*1024));
    else std::snprintf(buf, sizeof(buf), "%.1f GiB", n / (1024.0*1024*1024));
    return buf;
}

static std::string prepare_sample(const std::string& data_dir, size_t target_bytes) {
    std::vector<fs::path> files;
    for (auto& e : fs::directory_iterator(data_dir)) {
        if (e.is_regular_file() && e.path().extension() == ".tsv" && e.file_size() > 1000)
            files.push_back(e.path());
    }
    std::sort(files.begin(), files.end(), [](const auto& a, const auto& b) {
        return fs::file_size(a) > fs::file_size(b);
    });

    std::string out;
    out.reserve(target_bytes + 4096);
    for (const auto& fp : files) {
        if (out.size() >= target_bytes) break;
        std::ifstream fin(fp, std::ios::binary);
        std::string line;
        while (std::getline(fin, line)) {
            if (out.size() >= target_bytes) break;
            bool empty = true;
            for (char c : line) {
                if (c != ' ' && c != '\t' && c != '\r' && c != '\n') { empty = false; break; }
            }
            if (empty) continue;
            out += line;
            out += '\n';
        }
    }
    return out;
}

// Split data into N chunks at newline boundaries
static std::vector<std::pair<const char*, size_t>> split_chunks(
        const std::string& data, int n_threads) {
    std::vector<std::pair<const char*, size_t>> chunks;
    if (n_threads <= 1 || data.size() < 4096) {
        chunks.push_back({data.data(), data.size()});
        return chunks;
    }

    size_t chunk_target = data.size() / n_threads;
    size_t start = 0;
    for (int i = 0; i < n_threads - 1; ++i) {
        size_t end = start + chunk_target;
        if (end >= data.size()) break;
        // Snap to next newline
        while (end < data.size() && data[end] != '\n') ++end;
        if (end < data.size()) ++end;  // include the newline
        chunks.push_back({data.data() + start, end - start});
        start = end;
    }
    if (start < data.size()) {
        chunks.push_back({data.data() + start, data.size() - start});
    }
    return chunks;
}

// ---------------------------------------------------------------
// Result
// ---------------------------------------------------------------

struct Result {
    std::string name;
    size_t orig_bytes = 0;
    size_t comp_bytes = 0;
    double comp_wall = 0;
    double comp_cpu  = 0;
    double decomp_wall = 0;
    double decomp_cpu  = 0;
    bool roundtrip_ok = false;

    double ratio()       const { return comp_bytes > 0 ? (double)orig_bytes / comp_bytes : 0; }
    double comp_mbps()   const { return comp_wall > 0 ? (orig_bytes / 1e6) / comp_wall : 0; }
    double decomp_mbps() const { return decomp_wall > 0 ? (orig_bytes / 1e6) / decomp_wall : 0; }
};

// ---------------------------------------------------------------
// OpenZL — single-threaded
// ---------------------------------------------------------------

static Result bench_openzl_1t(const std::string& data, openzl::Compressor& compressor) {
    Result r;
    r.name = "openzl-1t";
    r.orig_bytes = data.size();

    openzl::CCtx cctx;
    cctx.setParameter(openzl::CParam::FormatVersion, ZL_MAX_FORMAT_VERSION);
    cctx.refCompressor(compressor);

    std::string compressed(ZL_compressBound(data.size()), '\0');

    double cpu0 = get_cpu_time();
    auto t0 = Clock::now();
    size_t comp_sz = cctx.compressSerial(compressed, data);
    auto t1 = Clock::now();
    double cpu1 = get_cpu_time();

    compressed.resize(comp_sz);
    r.comp_bytes = comp_sz;
    r.comp_wall = std::chrono::duration<double>(t1 - t0).count();
    r.comp_cpu  = cpu1 - cpu0;

    openzl::DCtx dctx;
    cpu0 = get_cpu_time();
    t0 = Clock::now();
    std::string decompressed = dctx.decompressSerial(compressed);
    t1 = Clock::now();
    cpu1 = get_cpu_time();

    r.decomp_wall = std::chrono::duration<double>(t1 - t0).count();
    r.decomp_cpu  = cpu1 - cpu0;
    r.roundtrip_ok = (decompressed == data);
    return r;
}

// ---------------------------------------------------------------
// OpenZL — multi-threaded (chunk + parallel compress/decompress)
// ---------------------------------------------------------------

static Result bench_openzl_mt(const std::string& data, openzl::Compressor& compressor,
                               int n_threads) {
    Result r;
    char label[32];
    std::snprintf(label, sizeof(label), "openzl-%dt", n_threads);
    r.name = label;
    r.orig_bytes = data.size();

    auto chunks = split_chunks(data, n_threads);
    int nc = (int)chunks.size();

    // --- Parallel compress ---
    std::vector<std::string> comp_bufs(nc);
    std::vector<size_t> comp_sizes(nc, 0);

    double cpu0 = get_cpu_time();
    auto t0 = Clock::now();

    std::vector<std::thread> threads;
    for (int i = 0; i < nc; ++i) {
        threads.emplace_back([&, i]() {
            openzl::CCtx cctx;
            cctx.setParameter(openzl::CParam::FormatVersion, ZL_MAX_FORMAT_VERSION);
            cctx.refCompressor(compressor);

            auto [ptr, len] = chunks[i];
            comp_bufs[i].resize(ZL_compressBound(len));
            openzl::poly::span<char> out_span(comp_bufs[i].data(), comp_bufs[i].size());
            openzl::poly::string_view in_view(ptr, len);
            comp_sizes[i] = cctx.compressSerial(out_span, in_view);
            comp_bufs[i].resize(comp_sizes[i]);
        });
    }
    for (auto& t : threads) t.join();

    auto t1 = Clock::now();
    double cpu1 = get_cpu_time();

    r.comp_wall = std::chrono::duration<double>(t1 - t0).count();
    r.comp_cpu  = cpu1 - cpu0;

    size_t total_comp = 0;
    for (int i = 0; i < nc; ++i) total_comp += comp_bufs[i].size();
    r.comp_bytes = total_comp;

    // --- Parallel decompress ---
    std::vector<std::string> decomp_bufs(nc);

    cpu0 = get_cpu_time();
    t0 = Clock::now();

    threads.clear();
    for (int i = 0; i < nc; ++i) {
        threads.emplace_back([&, i]() {
            openzl::DCtx dctx;
            decomp_bufs[i] = dctx.decompressSerial(comp_bufs[i]);
        });
    }
    for (auto& t : threads) t.join();

    t1 = Clock::now();
    cpu1 = get_cpu_time();

    r.decomp_wall = std::chrono::duration<double>(t1 - t0).count();
    r.decomp_cpu  = cpu1 - cpu0;

    // Verify round-trip
    size_t offset = 0;
    r.roundtrip_ok = true;
    for (int i = 0; i < nc; ++i) {
        auto [ptr, len] = chunks[i];
        if (decomp_bufs[i].size() != len ||
            std::memcmp(decomp_bufs[i].data(), ptr, len) != 0) {
            r.roundtrip_ok = false;
            break;
        }
        offset += len;
    }

    return r;
}

// ---------------------------------------------------------------
// zstd — single-threaded
// ---------------------------------------------------------------

static Result bench_zstd_1t(const std::string& data, int level,
                             ZSTD_CCtx* zcctx, ZSTD_DCtx* zdctx) {
    Result r;
    r.name = "zstd-1t -" + std::to_string(level);
    r.orig_bytes = data.size();

    size_t bound = ZSTD_compressBound(data.size());
    std::string compressed(bound, '\0');

    double cpu0 = get_cpu_time();
    auto t0 = Clock::now();
    size_t comp_sz = ZSTD_compressCCtx(zcctx, compressed.data(), bound,
                                        data.data(), data.size(), level);
    auto t1 = Clock::now();
    double cpu1 = get_cpu_time();

    if (ZSTD_isError(comp_sz)) {
        std::cerr << "zstd compress error: " << ZSTD_getErrorName(comp_sz) << "\n";
        return r;
    }
    compressed.resize(comp_sz);
    r.comp_bytes = comp_sz;
    r.comp_wall = std::chrono::duration<double>(t1 - t0).count();
    r.comp_cpu  = cpu1 - cpu0;

    size_t orig_sz = ZSTD_getFrameContentSize(compressed.data(), compressed.size());
    std::string decompressed(orig_sz, '\0');

    cpu0 = get_cpu_time();
    t0 = Clock::now();
    size_t dec_sz = ZSTD_decompressDCtx(zdctx, decompressed.data(), orig_sz,
                                         compressed.data(), compressed.size());
    t1 = Clock::now();
    cpu1 = get_cpu_time();

    if (ZSTD_isError(dec_sz)) {
        std::cerr << "zstd decompress error: " << ZSTD_getErrorName(dec_sz) << "\n";
        return r;
    }
    decompressed.resize(dec_sz);
    r.decomp_wall = std::chrono::duration<double>(t1 - t0).count();
    r.decomp_cpu  = cpu1 - cpu0;
    r.roundtrip_ok = (decompressed == data);
    return r;
}

// ---------------------------------------------------------------
// zstd — multi-threaded (chunk + parallel)
// ---------------------------------------------------------------

static Result bench_zstd_mt(const std::string& data, int level, int n_threads) {
    Result r;
    char label[32];
    std::snprintf(label, sizeof(label), "zstd-%dt -%d", n_threads, level);
    r.name = label;
    r.orig_bytes = data.size();

    auto chunks = split_chunks(data, n_threads);
    int nc = (int)chunks.size();

    std::vector<std::string> comp_bufs(nc);

    double cpu0 = get_cpu_time();
    auto t0 = Clock::now();

    std::vector<std::thread> threads;
    for (int i = 0; i < nc; ++i) {
        threads.emplace_back([&, i]() {
            auto [ptr, len] = chunks[i];
            size_t bound = ZSTD_compressBound(len);
            comp_bufs[i].resize(bound);
            size_t sz = ZSTD_compress(comp_bufs[i].data(), bound, ptr, len, level);
            comp_bufs[i].resize(ZSTD_isError(sz) ? 0 : sz);
        });
    }
    for (auto& t : threads) t.join();

    auto t1 = Clock::now();
    double cpu1 = get_cpu_time();

    r.comp_wall = std::chrono::duration<double>(t1 - t0).count();
    r.comp_cpu  = cpu1 - cpu0;

    size_t total_comp = 0;
    for (auto& b : comp_bufs) total_comp += b.size();
    r.comp_bytes = total_comp;

    // Parallel decompress
    std::vector<std::string> decomp_bufs(nc);

    cpu0 = get_cpu_time();
    t0 = Clock::now();

    threads.clear();
    for (int i = 0; i < nc; ++i) {
        threads.emplace_back([&, i]() {
            auto [ptr, len] = chunks[i];
            decomp_bufs[i].resize(len);
            ZSTD_decompress(decomp_bufs[i].data(), len,
                            comp_bufs[i].data(), comp_bufs[i].size());
        });
    }
    for (auto& t : threads) t.join();

    t1 = Clock::now();
    cpu1 = get_cpu_time();

    r.decomp_wall = std::chrono::duration<double>(t1 - t0).count();
    r.decomp_cpu  = cpu1 - cpu0;

    r.roundtrip_ok = true;
    for (int i = 0; i < nc; ++i) {
        auto [ptr, len] = chunks[i];
        if (decomp_bufs[i].size() != len ||
            std::memcmp(decomp_bufs[i].data(), ptr, len) != 0) {
            r.roundtrip_ok = false;
            break;
        }
    }
    return r;
}

// ---------------------------------------------------------------
// Snappy (always single-threaded, baseline)
// ---------------------------------------------------------------

static Result bench_snappy(const std::string& data) {
    Result r;
    r.name = "snappy-1t";
    r.orig_bytes = data.size();

    size_t max_len = snappy::MaxCompressedLength(data.size());
    std::string compressed(max_len, '\0');
    size_t comp_len = 0;

    double cpu0 = get_cpu_time();
    auto t0 = Clock::now();
    snappy::RawCompress(data.data(), data.size(), compressed.data(), &comp_len);
    auto t1 = Clock::now();
    double cpu1 = get_cpu_time();

    compressed.resize(comp_len);
    r.comp_bytes = comp_len;
    r.comp_wall = std::chrono::duration<double>(t1 - t0).count();
    r.comp_cpu  = cpu1 - cpu0;

    size_t decomp_len = 0;
    snappy::GetUncompressedLength(compressed.data(), compressed.size(), &decomp_len);
    std::string decompressed(decomp_len, '\0');

    cpu0 = get_cpu_time();
    t0 = Clock::now();
    snappy::RawUncompress(compressed.data(), compressed.size(), decompressed.data());
    t1 = Clock::now();
    cpu1 = get_cpu_time();

    r.decomp_wall = std::chrono::duration<double>(t1 - t0).count();
    r.decomp_cpu  = cpu1 - cpu0;
    r.roundtrip_ok = (decompressed == data);
    return r;
}

// ---------------------------------------------------------------
// Run N times, return median
// ---------------------------------------------------------------

static Result run_n(int runs, std::function<Result()> fn) {
    std::vector<Result> results;
    results.reserve(runs);
    for (int i = 0; i < runs; ++i) results.push_back(fn());
    std::sort(results.begin(), results.end(),
              [](const Result& a, const Result& b) { return a.comp_wall < b.comp_wall; });
    return results[results.size() / 2];
}

// ---------------------------------------------------------------
// Output
// ---------------------------------------------------------------

static void print_header() {
    std::printf("  %-18s %10s %8s %11s %9s %13s %9s %4s\n",
                "Compressor", "Compressed", "Ratio",
                "Comp MB/s", "CPU%",
                "Decomp MB/s", "CPU%", "RT");
    std::printf("  ----------------------------------------"
                "-----------------------------------------------\n");
}

static void print_row(const Result& r) {
    double ccpu = r.comp_wall > 0 ? (r.comp_cpu / r.comp_wall) * 100 : 0;
    double dcpu = r.decomp_wall > 0 ? (r.decomp_cpu / r.decomp_wall) * 100 : 0;
    const char* rt = r.roundtrip_ok ? "OK" : (r.comp_bytes > 0 ? "FAIL" : "SKIP");
    std::printf("  %-18s %10s %7.2fx %10.1f %8.0f%% %12.1f %8.0f%% %4s\n",
                r.name.c_str(),
                human_size(r.comp_bytes).c_str(),
                r.ratio(),
                r.comp_mbps(), ccpu,
                r.decomp_mbps(), dcpu,
                rt);
}

// ---------------------------------------------------------------
// Main
// ---------------------------------------------------------------

int main(int argc, char* argv[]) {
    if (argc < 3) {
        std::fprintf(stderr,
            "Usage: benchmark <data_dir> <compressor.zl_compressor>\n"
            "         [--runs N] [--sizes 1,2,5,10,20,50] [--threads N] [--csv out.csv]\n");
        return 1;
    }

    std::string data_dir = argv[1];
    std::string compressor_path = argv[2];

    int runs = 3;
    int n_threads = (int)std::thread::hardware_concurrency();
    std::vector<int> sizes_mb = {1, 2, 5, 10, 20, 50};
    std::string csv_path;

    for (int i = 3; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--runs" && i + 1 < argc) {
            runs = std::atoi(argv[++i]);
        } else if (arg == "--sizes" && i + 1 < argc) {
            sizes_mb.clear();
            std::string s = argv[++i];
            size_t pos = 0;
            while (pos < s.size()) {
                size_t comma = s.find(',', pos);
                if (comma == std::string::npos) comma = s.size();
                sizes_mb.push_back(std::atoi(s.substr(pos, comma - pos).c_str()));
                pos = comma + 1;
            }
        } else if (arg == "--threads" && i + 1 < argc) {
            n_threads = std::atoi(argv[++i]);
        } else if (arg == "--csv" && i + 1 < argc) {
            csv_path = argv[++i];
        }
    }

    // Load compressor
    auto comp_data = read_file(compressor_path);
    auto compressor = openzl::custom_parsers::createCompressorFromSerialized(comp_data);
    std::fprintf(stderr, "Loaded compressor: %s (%zu bytes)\n",
                 compressor_path.c_str(), comp_data.size());

    // Reusable zstd contexts (for single-threaded benchmarks)
    ZSTD_CCtx* zcctx = ZSTD_createCCtx();
    ZSTD_DCtx* zdctx = ZSTD_createDCtx();

    std::printf("==============================================================================\n");
    std::printf("  DNS TSV COMPRESSION BENCHMARK (C++, in-process)\n");
    std::printf("  Data: %s\n", data_dir.c_str());
    std::printf("  Compressor: %s (%zu bytes)\n", compressor_path.c_str(), comp_data.size());
    std::printf("  Runs: %d (median)  |  Threads: %d  |  Sizes: ", runs, n_threads);
    for (size_t i = 0; i < sizes_mb.size(); ++i)
        std::printf("%s%d", i ? "," : "", sizes_mb[i]);
    std::printf(" MB\n");
    std::printf("  CPU%%: process user+sys / wall (>100%% = multi-core)\n");
    std::printf("==============================================================================\n\n");

    FILE* csvfp = nullptr;
    if (!csv_path.empty()) {
        csvfp = std::fopen(csv_path.c_str(), "w");
        std::fprintf(csvfp,
            "size_mb,compressor,orig_bytes,comp_bytes,ratio,"
            "comp_wall_s,comp_cpu_s,comp_mbps,comp_cpu_pct,"
            "decomp_wall_s,decomp_cpu_s,decomp_mbps,decomp_cpu_pct,"
            "roundtrip\n");
    }

    struct SizeResults { int size_mb; std::vector<Result> results; };
    std::vector<SizeResults> all_results;

    for (int size_mb : sizes_mb) {
        size_t target = size_t(size_mb) * 1024 * 1024;
        auto sample = prepare_sample(data_dir, target);
        std::printf("--- %d MB (actual: %.2f MB) ---\n", size_mb, sample.size() / 1e6);

        // Warm cache
        volatile char sink = 0;
        for (size_t i = 0; i < sample.size(); i += 4096) sink += sample[i];
        (void)sink;

        SizeResults sr;
        sr.size_mb = size_mb;

        // OpenZL 1-thread
        sr.results.push_back(run_n(runs, [&]() {
            return bench_openzl_1t(sample, *compressor);
        }));

        // OpenZL N-threads
        if (n_threads > 1) {
            sr.results.push_back(run_n(runs, [&]() {
                return bench_openzl_mt(sample, *compressor, n_threads);
            }));
        }

        // zstd-9 1-thread
        sr.results.push_back(run_n(runs, [&]() {
            return bench_zstd_1t(sample, 9, zcctx, zdctx);
        }));

        // zstd-9 N-threads
        if (n_threads > 1) {
            sr.results.push_back(run_n(runs, [&]() {
                return bench_zstd_mt(sample, 9, n_threads);
            }));
        }

        // Snappy 1-thread
        sr.results.push_back(run_n(runs, [&]() {
            return bench_snappy(sample);
        }));

        print_header();
        for (const auto& r : sr.results) {
            print_row(r);
            if (csvfp) {
                double ccpu = r.comp_wall > 0 ? r.comp_cpu / r.comp_wall * 100 : 0;
                double dcpu = r.decomp_wall > 0 ? r.decomp_cpu / r.decomp_wall * 100 : 0;
                std::fprintf(csvfp,
                    "%d,%s,%zu,%zu,%.3f,%.6f,%.6f,%.1f,%.1f,%.6f,%.6f,%.1f,%.1f,%s\n",
                    size_mb, r.name.c_str(), r.orig_bytes, r.comp_bytes, r.ratio(),
                    r.comp_wall, r.comp_cpu, r.comp_mbps(), ccpu,
                    r.decomp_wall, r.decomp_cpu, r.decomp_mbps(), dcpu,
                    r.roundtrip_ok ? "PASS" : "FAIL");
            }
        }
        std::printf("\n");
        all_results.push_back(std::move(sr));
    }

    // ---- Summary ----
    std::printf("==============================================================================\n");
    std::printf("  SUMMARY\n");
    std::printf("==============================================================================\n\n");

    auto& ref = all_results[0].results;

    std::printf("  Compression:\n");
    std::printf("  %6s", "Size");
    for (const auto& r : ref) std::printf("  %-18s", r.name.c_str());
    std::printf("\n  %6s", "");
    for (size_t i = 0; i < ref.size(); ++i) std::printf("  %6s %5s %5s", "ratio", "MB/s", "CPU%");
    std::printf("\n  ------");
    for (size_t i = 0; i < ref.size(); ++i) std::printf("--------------------");
    std::printf("\n");

    for (const auto& sr : all_results) {
        std::printf("  %4d MB", sr.size_mb);
        for (const auto& r : sr.results) {
            double cpu = r.comp_wall > 0 ? r.comp_cpu / r.comp_wall * 100 : 0;
            std::printf("  %5.2fx %5.0f %4.0f%%", r.ratio(), r.comp_mbps(), cpu);
        }
        std::printf("\n");
    }

    std::printf("\n  Decompression:\n");
    std::printf("  %6s", "Size");
    for (const auto& r : ref) std::printf("  %-18s", r.name.c_str());
    std::printf("\n  %6s", "");
    for (size_t i = 0; i < ref.size(); ++i) std::printf("  %6s %5s %5s", "", "MB/s", "CPU%");
    std::printf("\n  ------");
    for (size_t i = 0; i < ref.size(); ++i) std::printf("--------------------");
    std::printf("\n");

    for (const auto& sr : all_results) {
        std::printf("  %4d MB", sr.size_mb);
        for (const auto& r : sr.results) {
            double cpu = r.decomp_wall > 0 ? r.decomp_cpu / r.decomp_wall * 100 : 0;
            std::printf("  %6s %5.0f %4.0f%%", "", r.decomp_mbps(), cpu);
        }
        std::printf("\n");
    }
    std::printf("\n");

    ZSTD_freeCCtx(zcctx);
    ZSTD_freeDCtx(zdctx);
    if (csvfp) {
        std::fclose(csvfp);
        std::printf("  CSV: %s\n\n", csv_path.c_str());
    }
    return 0;
}
