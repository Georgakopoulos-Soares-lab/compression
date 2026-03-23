// DNS TSV decompressor — decompresses OpenZL frame back to TSV.
// Usage: dns_decompress <input.zl> <output.tsv>

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <string>

#include "openzl/cpp/DCtx.hpp"

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

int main(int argc, char* argv[]) {
    if (argc < 3) {
        std::cerr << "Usage: dns_decompress <input.zl> <output.tsv>\n";
        return 1;
    }

    const char* input_path = argv[1];
    const char* output_path = argv[2];

    auto compressed = read_file(input_path);
    std::fprintf(stderr, "Input: %zu bytes (compressed)\n", compressed.size());

    DCtx dctx;
    auto t0 = std::chrono::steady_clock::now();
    std::string decompressed = dctx.decompressSerial(compressed);
    auto t1 = std::chrono::steady_clock::now();

    double secs = std::chrono::duration<double>(t1 - t0).count();
    double mbps = (decompressed.size() / 1e6) / secs;

    write_file(output_path, decompressed);

    std::fprintf(stderr, "Decompressed: %zu -> %zu in %.3fs (%.1f MB/s)\n",
                 compressed.size(), decompressed.size(), secs, mbps);
    return 0;
}
