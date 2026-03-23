// DNS TSV compressor — reads TSV, strips empty lines, compresses with trained OpenZL compressor.
// Usage: dns_compress <input.tsv> <output.zl> [compressor_path]

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

#include "openzl/cpp/CCtx.hpp"
#include "openzl/cpp/Compressor.hpp"
#include "openzl/zl_compress.h"
#include "openzl/zl_version.h"
#include "custom_parsers/dependency_registration.h"

using namespace openzl;

static std::string read_file(const char* path) {
    std::ifstream f(path, std::ios::binary | std::ios::ate);
    if (!f) { std::cerr << "Cannot open: " << path << "\n"; std::exit(1); }
    auto sz = f.tellg();
    f.seekg(0);
    std::string buf(sz, '\0');
    f.read(buf.data(), sz);
    return buf;
}

static void write_file(const char* path, const std::string& data) {
    std::ofstream f(path, std::ios::binary);
    if (!f) { std::cerr << "Cannot write: " << path << "\n"; std::exit(1); }
    f.write(data.data(), data.size());
}

// Strip empty lines from TSV data (batch separators from nom-kafka-dump)
static std::string strip_empty_lines(const std::string& input) {
    std::string out;
    out.reserve(input.size());
    size_t pos = 0;
    while (pos < input.size()) {
        size_t eol = input.find('\n', pos);
        if (eol == std::string::npos) eol = input.size();
        // Skip lines that are empty or whitespace-only
        bool empty = true;
        for (size_t i = pos; i < eol; ++i) {
            char c = input[i];
            if (c != ' ' && c != '\t' && c != '\r') { empty = false; break; }
        }
        if (!empty) {
            out.append(input, pos, eol - pos + (eol < input.size() ? 1 : 0));
        }
        pos = eol + 1;
    }
    return out;
}

int main(int argc, char* argv[]) {
    if (argc < 3) {
        std::cerr << "Usage: dns_compress <input.tsv> <output.zl> [compressor.zl_compressor]\n";
        return 1;
    }

    const char* input_path = argv[1];
    const char* output_path = argv[2];
    const char* compressor_path = argc > 3 ? argv[3] : nullptr;

    // Read and clean input
    auto raw = read_file(input_path);
    auto clean = strip_empty_lines(raw);
    std::fprintf(stderr, "Input: %zu bytes (%zu after stripping empty lines)\n",
                 raw.size(), clean.size());

    // Load compressor
    std::unique_ptr<Compressor> compressor;
    if (compressor_path) {
        auto comp_data = read_file(compressor_path);
        compressor = custom_parsers::createCompressorFromSerialized(comp_data);
        std::fprintf(stderr, "Loaded trained compressor: %s (%zu bytes)\n",
                     compressor_path, comp_data.size());
    } else {
        std::cerr << "No compressor provided — use trained compressor for best results.\n";
        return 1;
    }

    // Compress
    CCtx cctx;
    cctx.setParameter(CParam::FormatVersion, ZL_MAX_FORMAT_VERSION);
    cctx.refCompressor(*compressor);

    auto t0 = std::chrono::steady_clock::now();
    std::string compressed = cctx.compressSerial(clean);
    auto t1 = std::chrono::steady_clock::now();

    double secs = std::chrono::duration<double>(t1 - t0).count();
    double ratio = (double)clean.size() / compressed.size();
    double mbps = (clean.size() / 1e6) / secs;

    write_file(output_path, compressed);

    std::fprintf(stderr, "Compressed: %zu -> %zu (%.2fx) in %.3fs (%.1f MB/s)\n",
                 clean.size(), compressed.size(), ratio, secs, mbps);
    return 0;
}
