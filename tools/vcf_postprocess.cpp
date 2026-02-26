#include <algorithm>
#include <atomic>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <sys/stat.h>
#include <fcntl.h>
#include <thread>
#include <unistd.h>
#include <vector>

namespace fs = std::filesystem;

namespace {

[[noreturn]] void die(const std::string& msg) {
    std::cerr << "Error: " << msg << "\n";
    std::exit(1);
}

size_t parse_u64(const char* s) {
    errno = 0;
    char* end = nullptr;
    unsigned long long v = std::strtoull(s, &end, 10);
    if (errno != 0 || end == s || *end != '\0') die(std::string("Bad integer: ") + s);
    return static_cast<size_t>(v);
}

std::string read_text_file(const fs::path& p) {
    std::ifstream in(p);
    if (!in) die("Failed to read: " + p.string());
    std::string s((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    return s;
}

struct Part {
    std::string file;
    size_t bytes = 0;
};

// Minimal JSON extractor for our manifest format.
// Not a general-purpose JSON parser.
std::string extract_string_field(const std::string& json, const std::string& key) {
    std::string pat = "\"" + key + "\"";
    size_t k = json.find(pat);
    if (k == std::string::npos) return "";
    size_t colon = json.find(':', k + pat.size());
    if (colon == std::string::npos) return "";
    size_t q1 = json.find('"', colon + 1);
    if (q1 == std::string::npos) return "";
    size_t q2 = json.find('"', q1 + 1);
    if (q2 == std::string::npos) return "";
    return json.substr(q1 + 1, q2 - (q1 + 1));
}

size_t extract_u64_field(const std::string& json, const std::string& key) {
    std::string pat = "\"" + key + "\"";
    size_t k = json.find(pat);
    if (k == std::string::npos) return 0;
    size_t colon = json.find(':', k + pat.size());
    if (colon == std::string::npos) return 0;
    size_t start = json.find_first_of("0123456789", colon + 1);
    if (start == std::string::npos) return 0;
    size_t end = json.find_first_not_of("0123456789", start);
    return static_cast<size_t>(std::stoull(json.substr(start, end - start)));
}

std::vector<Part> extract_parts(const std::string& json) {
    std::vector<Part> parts;

    size_t pos = json.find("\"parts\"");
    if (pos == std::string::npos) return parts;
    pos = json.find('[', pos);
    if (pos == std::string::npos) return parts;

    while (true) {
        size_t obj = json.find('{', pos);
        if (obj == std::string::npos) break;
        size_t obj_end = json.find('}', obj);
        if (obj_end == std::string::npos) break;
        std::string_view block(json.data() + obj, obj_end - obj + 1);

        auto find_field = [&](std::string_view k) -> std::string {
            std::string pat = "\"" + std::string(k) + "\"";
            size_t kk = block.find(pat);
            if (kk == std::string::npos) return "";
            size_t colon = block.find(':', kk + pat.size());
            if (colon == std::string::npos) return "";
            size_t q1 = block.find('"', colon + 1);
            if (q1 == std::string::npos) return "";
            size_t q2 = block.find('"', q1 + 1);
            if (q2 == std::string::npos) return "";
            return std::string(block.substr(q1 + 1, q2 - (q1 + 1)));
        };

        auto find_bytes = [&]() -> size_t {
            std::string pat = "\"bytes\"";
            size_t kk = block.find(pat);
            if (kk == std::string::npos) return 0;
            size_t colon = block.find(':', kk + pat.size());
            if (colon == std::string::npos) return 0;
            size_t start = block.find_first_of("0123456789", colon + 1);
            if (start == std::string::npos) return 0;
            size_t end = block.find_first_not_of("0123456789", start);
            return static_cast<size_t>(std::stoull(std::string(block.substr(start, end - start))));
        };

        Part p;
        p.file = find_field("file");
        p.bytes = find_bytes();
        if (!p.file.empty() && p.bytes > 0) parts.push_back(p);

        pos = obj_end + 1;
        if (pos >= json.size()) break;
        if (json.find(']', obj_end) < json.find('{', obj_end)) break;
    }

    return parts;
}

void copy_file_to_offset(int out_fd, const fs::path& in_path, size_t out_off) {
    int in_fd = ::open(in_path.c_str(), O_RDONLY);
    if (in_fd < 0) die("Failed to open chunk: " + in_path.string());

    constexpr size_t BUF = 8 * 1024 * 1024;
    std::vector<char> buf(BUF);

    size_t pos = 0;
    while (true) {
        ssize_t r = ::read(in_fd, buf.data(), buf.size());
        if (r < 0) die("read failed: " + in_path.string());
        if (r == 0) break;

        size_t wpos = 0;
        while (wpos < static_cast<size_t>(r)) {
            ssize_t w = ::pwrite(out_fd, buf.data() + wpos, static_cast<size_t>(r) - wpos, static_cast<off_t>(out_off + pos + wpos));
            if (w < 0) die("pwrite failed");
            wpos += static_cast<size_t>(w);
        }
        pos += static_cast<size_t>(r);
    }

    ::close(in_fd);
}

} // namespace

int main(int argc, char** argv) {
    if (argc < 3) {
        std::cerr << "Usage: " << argv[0] << " <pack_dir> <out.vcf> [--threads N] [--chunk-suffix SUFFIX] [--chunk-dir DIR]\n";
        std::cerr << "  pack_dir must contain manifest.json and header.vcf.\n";
        std::cerr << "  chunk-dir defaults to pack_dir. chunk-suffix defaults to empty (uses manifest paths).\n";
        std::cerr << "  Example for decompressed chunks: --chunk-dir pack_dir --chunk-suffix .dec\n";
        return 1;
    }

    fs::path pack_dir = argv[1];
    fs::path out_vcf = argv[2];

    size_t threads = std::max<size_t>(1, std::thread::hardware_concurrency());
    fs::path chunk_dir = pack_dir;
    std::string chunk_suffix;

    for (int i = 3; i < argc; i++) {
        std::string arg = argv[i];
        if (arg == "--threads" && i + 1 < argc) {
            threads = parse_u64(argv[++i]);
            if (threads == 0) threads = 1;
        } else if (arg == "--chunk-suffix" && i + 1 < argc) {
            chunk_suffix = argv[++i];
        } else if (arg == "--chunk-dir" && i + 1 < argc) {
            chunk_dir = argv[++i];
        } else {
            die("Unknown arg: " + arg);
        }
    }

    fs::path manifest_path = pack_dir / "manifest.json";
    fs::path header_path = pack_dir / "header.vcf";

    std::string json = read_text_file(manifest_path);
    std::string format = extract_string_field(json, "format");
    if (format != "openzl_vcf_header_body_v1") die("Unexpected manifest format: " + format);

    size_t header_bytes = extract_u64_field(json, "header_bytes");
    auto parts = extract_parts(json);
    if (parts.empty()) die("No parts in manifest");

    // Compute output size.
    size_t body_bytes = 0;
    for (auto& p : parts) body_bytes += p.bytes;
    size_t out_size = header_bytes + body_bytes;

    fs::create_directories(out_vcf.parent_path());

    int out_fd = ::open(out_vcf.c_str(), O_CREAT | O_WRONLY | O_TRUNC, 0644);
    if (out_fd < 0) die("Failed to create out: " + out_vcf.string());

    if (::ftruncate(out_fd, static_cast<off_t>(out_size)) != 0) die("ftruncate failed");

    // Write header first (single-thread).
    copy_file_to_offset(out_fd, header_path, 0);

    // Compute offsets for parts.
    std::vector<size_t> offsets(parts.size());
    size_t cur = header_bytes;
    for (size_t i = 0; i < parts.size(); i++) {
        offsets[i] = cur;
        cur += parts[i].bytes;
    }

    std::atomic<size_t> next{0};
    std::vector<std::thread> pool;
    pool.reserve(threads);

    for (size_t t = 0; t < threads; t++) {
        pool.emplace_back([&]() {
            while (true) {
                size_t idx = next.fetch_add(1);
                if (idx >= parts.size()) break;

                fs::path rel = parts[idx].file;
                fs::path in_path = chunk_dir / rel;
                if (!chunk_suffix.empty()) {
                    in_path += chunk_suffix;
                }

                // If manifest path is relative, resolve against chunk_dir.
                if (!fs::exists(in_path)) {
                    die("Missing chunk: " + in_path.string());
                }

                // Optional size check.
                size_t sz = static_cast<size_t>(fs::file_size(in_path));
                if (sz != parts[idx].bytes) {
                    std::cerr << "Warning: chunk size mismatch for " << in_path << ": expected " << parts[idx].bytes << ", got " << sz << "\n";
                }

                copy_file_to_offset(out_fd, in_path, offsets[idx]);
            }
        });
    }

    for (auto& th : pool) th.join();

    ::close(out_fd);

    std::cerr << "Reassembled: " << out_vcf << " (" << out_size << " bytes)\n";
    return 0;
}
