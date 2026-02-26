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
#include <string_view>
#include <sys/mman.h>
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

struct MappedFile {
    int fd = -1;
    const char* data = nullptr;
    size_t size = 0;

    explicit MappedFile(const std::string& path) {
        fd = ::open(path.c_str(), O_RDONLY);
        if (fd < 0) die("Failed to open: " + path + " (" + std::strerror(errno) + ")");
        struct stat st;
        if (::fstat(fd, &st) != 0) die("Failed to stat: " + path);
        size = static_cast<size_t>(st.st_size);
        if (size == 0) die("Empty input file");
        void* mapped = ::mmap(nullptr, size, PROT_READ, MAP_PRIVATE, fd, 0);
        if (mapped == MAP_FAILED) die("mmap failed");
        data = reinterpret_cast<const char*>(mapped);
        ::madvise((void*)data, size, MADV_SEQUENTIAL);
    }

    ~MappedFile() {
        if (data && data != MAP_FAILED) ::munmap((void*)data, size);
        if (fd >= 0) ::close(fd);
    }

    MappedFile(const MappedFile&) = delete;
    MappedFile& operator=(const MappedFile&) = delete;
};

size_t find_header_end(const char* data, size_t size) {
    // Header is maximal prefix of lines that start with '#'.
    size_t pos = 0;
    while (pos < size) {
        size_t line_start = pos;
        if (data[line_start] != '#') break;
        const void* nl = ::memchr(data + pos, '\n', size - pos);
        if (!nl) {
            // Whole file is header.
            return size;
        }
        size_t nl_pos = static_cast<const char*>(nl) - data;
        pos = nl_pos + 1;
    }
    return pos;
}

size_t clamp_body_end_to_newline(const char* data, size_t size, size_t desired_end) {
    if (desired_end >= size) return size;
    const void* nl = ::memchr(data + desired_end, '\n', size - desired_end);
    if (!nl) return size;
    return static_cast<const char*>(nl) - data + 1;
}

size_t parse_u64(const char* s) {
    errno = 0;
    char* end = nullptr;
    unsigned long long v = std::strtoull(s, &end, 10);
    if (errno != 0 || end == s || *end != '\0') die(std::string("Bad integer: ") + s);
    return static_cast<size_t>(v);
}

struct Part {
    size_t start = 0;
    size_t end = 0;
};

std::vector<Part> compute_parts(const char* data, size_t body_start, size_t body_end, size_t threads, size_t max_chunk_bytes) {
    if (body_end <= body_start) return {};

    size_t body_bytes = body_end - body_start;
    size_t min_parts = threads > 0 ? threads : 1;
    size_t parts = std::max(min_parts, (body_bytes + max_chunk_bytes - 1) / max_chunk_bytes);
    if (parts == 0) parts = 1;

    std::vector<size_t> boundaries;
    boundaries.resize(parts + 1);
    boundaries[0] = body_start;
    boundaries[parts] = body_end;

    for (size_t i = 1; i < parts; i++) {
        size_t approx = body_start + (body_bytes * i) / parts;
        if (approx >= body_end) {
            boundaries[i] = body_end;
            continue;
        }
        const void* nl = ::memchr(data + approx, '\n', body_end - approx);
        if (!nl) {
            boundaries[i] = body_end;
            continue;
        }
        boundaries[i] = static_cast<const char*>(nl) - data + 1;
    }

    // Ensure monotonic boundaries.
    for (size_t i = 1; i <= parts; i++) {
        if (boundaries[i] < boundaries[i - 1]) boundaries[i] = boundaries[i - 1];
        if (boundaries[i] > body_end) boundaries[i] = body_end;
    }

    std::vector<Part> out;
    out.reserve(parts);
    for (size_t i = 0; i < parts; i++) {
        size_t a = boundaries[i];
        size_t b = boundaries[i + 1];
        if (b > a) out.push_back(Part{a, b});
    }
    return out;
}

void ensure_dir_empty_or_create(const fs::path& p, bool force) {
    if (!fs::exists(p)) {
        fs::create_directories(p);
        return;
    }
    if (!fs::is_directory(p)) die("Output path exists but is not a directory: " + p.string());

    bool empty = fs::directory_iterator(p) == fs::directory_iterator();
    if (!empty && !force) die("Output dir not empty (use --force): " + p.string());
    if (!empty && force) {
        for (auto& entry : fs::directory_iterator(p)) {
            fs::remove_all(entry.path());
        }
    }
}

void write_file_range(const fs::path& out_path, const char* data, size_t start, size_t end) {
    int out_fd = ::open(out_path.c_str(), O_CREAT | O_WRONLY | O_TRUNC, 0644);
    if (out_fd < 0) die("Failed to create: " + out_path.string());

    constexpr size_t BUF = 8 * 1024 * 1024;
    size_t pos = start;
    while (pos < end) {
        size_t n = std::min(BUF, end - pos);
        ssize_t w = ::write(out_fd, data + pos, n);
        if (w < 0) die("write failed: " + out_path.string());
        pos += static_cast<size_t>(w);
    }

    ::close(out_fd);
}

std::string json_escape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 8);
    for (char c : s) {
        switch (c) {
            case '\\': out += "\\\\"; break;
            case '"': out += "\\\""; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", static_cast<unsigned>(static_cast<unsigned char>(c)));
                    out += buf;
                } else {
                    out += c;
                }
        }
    }
    return out;
}

} // namespace

int main(int argc, char** argv) {
    if (argc < 3) {
        std::cerr
            << "Usage: " << argv[0] << " <input.vcf> <out_dir> [--threads N] [--target-mib M] [--max-chunk-mib M] [--force]\n";
        return 1;
    }

    std::string input = argv[1];
    fs::path out_dir = argv[2];

    size_t threads = std::max<size_t>(1, std::thread::hardware_concurrency());
    size_t target_mib = 0;          // body only
    size_t max_chunk_mib = 450;     // safe under 500MB limit
    bool force = false;

    for (int i = 3; i < argc; i++) {
        std::string arg = argv[i];
        if (arg == "--threads" && i + 1 < argc) {
            threads = parse_u64(argv[++i]);
            if (threads == 0) threads = 1;
        } else if (arg == "--target-mib" && i + 1 < argc) {
            target_mib = parse_u64(argv[++i]);
        } else if (arg == "--max-chunk-mib" && i + 1 < argc) {
            max_chunk_mib = parse_u64(argv[++i]);
            if (max_chunk_mib == 0) die("--max-chunk-mib must be > 0");
        } else if (arg == "--force") {
            force = true;
        } else {
            die("Unknown arg: " + arg);
        }
    }

    ensure_dir_empty_or_create(out_dir, force);
    fs::path chunks_dir = out_dir / "body_parts";
    fs::create_directories(chunks_dir);

    MappedFile mf(input);

    size_t header_end = find_header_end(mf.data, mf.size);
    size_t body_start = header_end;

    size_t body_end = mf.size;
    if (target_mib > 0) {
        size_t target_bytes = target_mib * 1024ull * 1024ull;
        size_t desired = std::min(mf.size, body_start + target_bytes);
        body_end = clamp_body_end_to_newline(mf.data, mf.size, desired);
    }

    // Write header bytes once.
    fs::path header_path = out_dir / "header.vcf";
    write_file_range(header_path, mf.data, 0, header_end);

    size_t max_chunk_bytes = max_chunk_mib * 1024ull * 1024ull;
    auto parts = compute_parts(mf.data, body_start, body_end, threads, max_chunk_bytes);

    std::vector<std::string> part_files(parts.size());
    std::vector<size_t> part_sizes(parts.size());

    std::atomic<size_t> next{0};
    std::vector<std::thread> pool;
    pool.reserve(threads);

    for (size_t t = 0; t < threads; t++) {
        pool.emplace_back([&]() {
            while (true) {
                size_t idx = next.fetch_add(1);
                if (idx >= parts.size()) break;

                char name[64];
                std::snprintf(name, sizeof(name), "part_%06zu.vcfbody", idx);
                fs::path out_path = chunks_dir / name;

                write_file_range(out_path, mf.data, parts[idx].start, parts[idx].end);
                part_files[idx] = std::string("body_parts/") + name;
                part_sizes[idx] = parts[idx].end - parts[idx].start;
            }
        });
    }

    for (auto& th : pool) th.join();

    // Manifest.
    fs::path manifest_path = out_dir / "manifest.json";
    std::ofstream man(manifest_path);
    if (!man) die("Failed to write manifest");

    const std::string input_name = fs::path(input).filename().string();

    man << "{\n";
    man << "  \"format\": \"openzl_vcf_header_body_v1\",\n";
    man << "  \"input_name\": \"" << json_escape(input_name) << "\",\n";
    man << "  \"header_file\": \"header.vcf\",\n";
    man << "  \"header_bytes\": " << header_end << ",\n";
    man << "  \"body_bytes\": " << (body_end - body_start) << ",\n";
    man << "  \"target_mib\": " << target_mib << ",\n";
    man << "  \"max_chunk_mib\": " << max_chunk_mib << ",\n";
    man << "  \"parts\": [\n";

    for (size_t i = 0; i < part_files.size(); i++) {
        man << "    { \"file\": \"" << json_escape(part_files[i]) << "\", \"bytes\": " << part_sizes[i] << " }";
        if (i + 1 != part_files.size()) man << ",";
        man << "\n";
    }

    man << "  ]\n";
    man << "}\n";

    size_t total_written = 0;
    for (size_t b : part_sizes) total_written += b;

    std::cerr << "Wrote: " << header_path << " (" << header_end << " bytes)\n";
    std::cerr << "Wrote: " << parts.size() << " body parts to " << chunks_dir << " (" << total_written << " bytes)\n";
    std::cerr << "Manifest: " << manifest_path << "\n";

    return 0;
}
