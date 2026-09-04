// vcf_field_codec — reversible field-aware decomposition for VCF.
//
//   vcf_field_codec pack   <in.vcf>  <out_dir> [--max-chunk-mib N] [--force]
//   vcf_field_codec unpack <out_dir> <reconstructed.vcf>
//
// Rationale
// ---------
// The existing pipeline (tools/vcf_preprocessing) feeds each ~40 MiB slice of
// the VCF body to OpenZL as raw row-major TSV text. That leaves the biggest
// structural win on the table: a VCF body is a fixed-width table, and every
// genotype column is one sample's calls. Splitting the body into per-column
// streams (and storing the sample block column-major, i.e. transposed) puts
// each sample's calls contiguously and gives every fixed column its own
// stream, which any downstream compressor models far better.
//
// This tool performs ONLY that decomposition and its exact inverse. It does
// not compress anything and it does no semantic parsing (POS stays a string,
// INFO stays one blob) — that keeps the round trip trivially byte-exact. The
// benchmark script compresses each emitted stream separately.
//
// Reversibility
// -------------
// VCF fields cannot contain '\n' or '\t' (spec), so splitting a line on '\t'
// and rejoining with '\t' + '\n' reproduces the original bytes exactly. Cases
// handled: empty fields, trailing '\t' (=> trailing empty field, preserved),
// '\r\n' endings (the '\r' rides along in the last field), and a final line
// with no trailing '\n'. Any chunk whose rows are not all the same width is
// stored verbatim ("raw") so unusual inputs still round-trip.

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <filesystem>
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

constexpr int MAX_FIXED = 8;   // CHROM POS ID REF ALT QUAL FILTER INFO
constexpr int FORMAT_COL = 8;  // 0-indexed FORMAT column

[[noreturn]] void die(const std::string& m) {
    std::cerr << "Error: " << m << "\n";
    std::exit(1);
}

struct MappedFile {
    int fd = -1;
    const char* data = nullptr;
    size_t size = 0;
    explicit MappedFile(const std::string& path) {
        fd = ::open(path.c_str(), O_RDONLY);
        if (fd < 0) die("open failed: " + path + " (" + std::strerror(errno) + ")");
        struct stat st{};
        if (::fstat(fd, &st) != 0) die("fstat failed: " + path);
        size = static_cast<size_t>(st.st_size);
        if (size == 0) { data = ""; return; }
        void* m = ::mmap(nullptr, size, PROT_READ, MAP_PRIVATE, fd, 0);
        if (m == MAP_FAILED) die("mmap failed: " + path);
        data = static_cast<const char*>(m);
        ::madvise((void*)data, size, MADV_SEQUENTIAL);
    }
    ~MappedFile() {
        if (data && size) ::munmap((void*)data, size);
        if (fd >= 0) ::close(fd);
    }
    MappedFile(const MappedFile&) = delete;
    MappedFile& operator=(const MappedFile&) = delete;
};

size_t find_header_end(const char* d, size_t n) {
    size_t pos = 0;
    while (pos < n) {
        if (d[pos] != '#') break;
        const void* nl = ::memchr(d + pos, '\n', n - pos);
        if (!nl) return n;
        pos = (static_cast<const char*>(nl) - d) + 1;
    }
    return pos;
}

void write_all(const fs::path& p, const char* buf, size_t n) {
    int fd = ::open(p.c_str(), O_CREAT | O_WRONLY | O_TRUNC, 0644);
    if (fd < 0) die("create failed: " + p.string());
    size_t off = 0;
    while (off < n) {
        ssize_t w = ::write(fd, buf + off, std::min<size_t>(1u << 23, n - off));
        if (w < 0) die("write failed: " + p.string());
        off += static_cast<size_t>(w);
    }
    ::close(fd);
}

std::string slurp(const fs::path& p) {
    std::ifstream f(p, std::ios::binary);
    if (!f) die("read failed: " + p.string());
    return std::string((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
}

std::string jesc(const std::string& s) {
    std::string o;
    for (char c : s) {
        if (c == '"' || c == '\\') { o += '\\'; o += c; }
        else o += c;
    }
    return o;
}

// ---- line-safe body chunking -------------------------------------------------
struct Range { size_t a, b; };

std::vector<Range> chunk_body(const char* d, size_t start, size_t end, size_t max_bytes) {
    std::vector<Range> out;
    size_t pos = start;
    while (pos < end) {
        size_t want = std::min(end, pos + max_bytes);
        if (want < end) {
            const void* nl = ::memchr(d + want, '\n', end - want);
            want = nl ? (static_cast<const char*>(nl) - d) + 1 : end;
        }
        out.push_back({pos, want});
        pos = want;
    }
    return out;
}

// ---- pack ------------------------------------------------------------------
struct ChunkMeta {
    size_t nrows = 0;
    int ncols = 0;
    bool ends_nl = true;
    bool raw = false;
    size_t orig_bytes = 0;
};

void pack_one_chunk(const char* d, Range r, const fs::path& dir, ChunkMeta& meta) {
    fs::create_directories(dir);
    meta.orig_bytes = r.b - r.a;
    meta.ends_nl = (r.b > r.a) && d[r.b - 1] == '\n';

    // Split into lines (view over the mapped file).
    std::vector<std::string_view> lines;
    size_t p = r.a;
    while (p < r.b) {
        const void* nl = ::memchr(d + p, '\n', r.b - p);
        size_t e = nl ? (static_cast<const char*>(nl) - d) : r.b;
        lines.emplace_back(d + p, e - p);
        p = (e < r.b) ? e + 1 : r.b;
    }
    meta.nrows = lines.size();
    if (lines.empty()) { meta.ncols = 0; return; }

    auto split_tabs = [](std::string_view ln, std::vector<std::string_view>& out) {
        out.clear();
        size_t s = 0;
        while (true) {
            size_t t = ln.find('\t', s);
            if (t == std::string_view::npos) { out.push_back(ln.substr(s)); break; }
            out.push_back(ln.substr(s, t - s));
            s = t + 1;
        }
    };

    std::vector<std::string_view> f0;
    split_tabs(lines[0], f0);
    const int ncols = static_cast<int>(f0.size());
    meta.ncols = ncols;

    // Verify uniform width; otherwise store the chunk verbatim.
    std::vector<std::vector<std::string_view>> rows;
    rows.reserve(lines.size());
    rows.push_back(f0);
    std::vector<std::string_view> tmp;
    for (size_t i = 1; i < lines.size(); i++) {
        split_tabs(lines[i], tmp);
        if (static_cast<int>(tmp.size()) != ncols) { meta.raw = true; break; }
        rows.push_back(tmp);
    }
    if (meta.raw) {
        write_all(dir / "raw", d + r.a, r.b - r.a);
        return;
    }

    const int nfixed = std::min(ncols, MAX_FIXED);
    const bool has_format = ncols > FORMAT_COL;
    const int first_sample = FORMAT_COL + 1;
    const int nsamples = ncols > first_sample ? ncols - first_sample : 0;

    // Fixed columns + FORMAT: one file each, values '\n'-terminated.
    auto write_col = [&](int col, const char* name) {
        std::string buf;
        for (auto& row : rows) { buf.append(row[col].data(), row[col].size()); buf += '\n'; }
        write_all(dir / name, buf.data(), buf.size());
    };
    char nm[16];
    for (int c = 0; c < nfixed; c++) { std::snprintf(nm, sizeof nm, "col_%02d", c); write_col(c, nm); }
    if (has_format) write_col(FORMAT_COL, "col_08");

    // Sample block, column-major (transposed): sample s -> nrows lines.
    if (nsamples > 0) {
        std::string buf;
        for (int s = 0; s < nsamples; s++) {
            int col = first_sample + s;
            for (auto& row : rows) { buf.append(row[col].data(), row[col].size()); buf += '\n'; }
        }
        write_all(dir / "gt.mat", buf.data(), buf.size());
    }
}

int do_pack(const std::string& in, const fs::path& out, size_t max_mib, bool force) {
    if (fs::exists(out)) {
        if (!force && !fs::is_empty(out)) die("out dir not empty (use --force): " + out.string());
        if (force) for (auto& e : fs::directory_iterator(out)) fs::remove_all(e.path());
    }
    fs::create_directories(out / "parts");

    MappedFile mf(in);
    size_t hend = find_header_end(mf.data, mf.size);
    write_all(out / "header.vcf", mf.data, hend);

    auto chunks = chunk_body(mf.data, hend, mf.size, max_mib * 1024 * 1024);
    std::vector<ChunkMeta> metas(chunks.size());

    std::atomic<size_t> next{0};
    unsigned nthreads = std::max(1u, std::thread::hardware_concurrency());
    std::vector<std::thread> pool;
    for (unsigned t = 0; t < nthreads; t++) {
        pool.emplace_back([&] {
            for (;;) {
                size_t i = next.fetch_add(1);
                if (i >= chunks.size()) break;
                char sub[16];
                std::snprintf(sub, sizeof sub, "%06zu", i);
                pack_one_chunk(mf.data, chunks[i], out / "parts" / sub, metas[i]);
            }
        });
    }
    for (auto& th : pool) th.join();

    std::ofstream man(out / "manifest.json");
    man << "{\n  \"format\": \"vcf_field_split_v1\",\n";
    man << "  \"input_name\": \"" << jesc(fs::path(in).filename().string()) << "\",\n";
    man << "  \"header_bytes\": " << hend << ",\n";
    man << "  \"body_bytes\": " << (mf.size - hend) << ",\n";
    man << "  \"chunks\": [\n";
    for (size_t i = 0; i < metas.size(); i++) {
        auto& m = metas[i];
        man << "    {\"nrows\": " << m.nrows << ", \"ncols\": " << m.ncols
            << ", \"ends_nl\": " << (m.ends_nl ? "true" : "false")
            << ", \"raw\": " << (m.raw ? "true" : "false")
            << ", \"orig_bytes\": " << m.orig_bytes << "}"
            << (i + 1 < metas.size() ? "," : "") << "\n";
    }
    man << "  ]\n}\n";

    size_t raw_chunks = 0;
    for (auto& m : metas) raw_chunks += m.raw;
    std::cerr << "packed " << chunks.size() << " chunks (" << raw_chunks << " raw), header "
              << hend << " B, body " << (mf.size - hend) << " B\n";
    return 0;
}

// ---- unpack ---------------------------------------------------------------
// Minimal manifest reader: pull the fields we wrote, in the order we wrote them.
struct CM { size_t nrows; int ncols; bool ends_nl; bool raw; size_t orig_bytes; };

std::vector<CM> read_manifest(const std::string& j, size_t& header_bytes) {
    auto num_after = [&](size_t from, const char* key) -> long long {
        size_t k = j.find(key, from);
        if (k == std::string::npos) return -1;
        k = j.find(':', k) + 1;
        while (k < j.size() && (j[k] == ' ' || j[k] == '\t')) k++;
        return std::strtoll(j.c_str() + k, nullptr, 10);
    };
    auto bool_after = [&](size_t from, const char* key) -> bool {
        size_t k = j.find(key, from);
        k = j.find(':', k) + 1;
        while (k < j.size() && j[k] == ' ') k++;
        return j.compare(k, 4, "true") == 0;
    };
    header_bytes = static_cast<size_t>(num_after(0, "\"header_bytes\""));

    std::vector<CM> out;
    size_t arr = j.find("\"chunks\"");
    size_t pos = j.find('[', arr);
    while (true) {
        size_t o = j.find('{', pos);
        if (o == std::string::npos) break;
        size_t c = j.find('}', o);
        CM m{};
        m.nrows = static_cast<size_t>(num_after(o, "\"nrows\""));
        m.ncols = static_cast<int>(num_after(o, "\"ncols\""));
        m.ends_nl = bool_after(o, "\"ends_nl\"");
        m.raw = bool_after(o, "\"raw\"");
        m.orig_bytes = static_cast<size_t>(num_after(o, "\"orig_bytes\""));
        out.push_back(m);
        pos = c + 1;
    }
    return out;
}

// Split a '\n'-terminated buffer into exactly `count` views.
void views_n(const std::string& buf, size_t count, std::vector<std::string_view>& out) {
    out.clear();
    out.reserve(count);
    size_t p = 0;
    for (size_t i = 0; i < count; i++) {
        size_t e = buf.find('\n', p);
        if (e == std::string::npos) die("stream truncated: expected " + std::to_string(count) + " values");
        out.emplace_back(buf.data() + p, e - p);
        p = e + 1;
    }
}

int do_unpack(const fs::path& in, const std::string& out_vcf) {
    size_t header_bytes = 0;
    auto metas = read_manifest(slurp(in / "manifest.json"), header_bytes);

    std::string outbuf;
    outbuf += slurp(in / "header.vcf");

    for (size_t ci = 0; ci < metas.size(); ci++) {
        const CM& m = metas[ci];
        char sub[16];
        std::snprintf(sub, sizeof sub, "%06zu", ci);
        fs::path dir = in / "parts" / sub;

        if (m.raw) { outbuf += slurp(dir / "raw"); continue; }
        if (m.nrows == 0) continue;

        const int nfixed = std::min(m.ncols, MAX_FIXED);
        const bool has_format = m.ncols > FORMAT_COL;
        const int first_sample = FORMAT_COL + 1;
        const int nsamples = m.ncols > first_sample ? m.ncols - first_sample : 0;

        std::vector<std::string> cols(nfixed);
        std::vector<std::vector<std::string_view>> colv(nfixed);
        char nm[16];
        for (int c = 0; c < nfixed; c++) {
            std::snprintf(nm, sizeof nm, "col_%02d", c);
            cols[c] = slurp(dir / nm);
            views_n(cols[c], m.nrows, colv[c]);
        }
        std::string fmt;
        std::vector<std::string_view> fmtv;
        if (has_format) { fmt = slurp(dir / "col_08"); views_n(fmt, m.nrows, fmtv); }

        std::string gt;
        std::vector<std::string_view> gtv;  // size nsamples*nrows, sample-major
        if (nsamples > 0) {
            gt = slurp(dir / "gt.mat");
            views_n(gt, static_cast<size_t>(nsamples) * m.nrows, gtv);
        }

        for (size_t r = 0; r < m.nrows; r++) {
            for (int c = 0; c < nfixed; c++) {
                if (c) outbuf += '\t';
                outbuf.append(colv[c][r].data(), colv[c][r].size());
            }
            if (has_format) { outbuf += '\t'; outbuf.append(fmtv[r].data(), fmtv[r].size()); }
            for (int s = 0; s < nsamples; s++) {
                outbuf += '\t';
                const std::string_view& v = gtv[static_cast<size_t>(s) * m.nrows + r];
                outbuf.append(v.data(), v.size());
            }
            bool last_row = (r + 1 == m.nrows);
            if (!(last_row && !m.ends_nl)) outbuf += '\n';
        }
    }

    write_all(out_vcf, outbuf.data(), outbuf.size());
    std::cerr << "unpacked -> " << out_vcf << " (" << outbuf.size() << " bytes)\n";
    return 0;
}

} // namespace

int main(int argc, char** argv) {
    if (argc < 4) {
        std::cerr << "Usage:\n"
                  << "  " << argv[0] << " pack   <in.vcf>  <out_dir> [--max-chunk-mib N] [--force]\n"
                  << "  " << argv[0] << " unpack <out_dir> <reconstructed.vcf>\n";
        return 1;
    }
    std::string cmd = argv[1];
    if (cmd == "pack") {
        std::string in = argv[2];
        fs::path out = argv[3];
        size_t max_mib = 32;
        bool force = false;
        for (int i = 4; i < argc; i++) {
            std::string a = argv[i];
            if (a == "--max-chunk-mib" && i + 1 < argc) max_mib = std::strtoull(argv[++i], nullptr, 10);
            else if (a == "--force") force = true;
            else die("unknown arg: " + a);
        }
        if (max_mib == 0) die("--max-chunk-mib must be > 0");
        return do_pack(in, out, max_mib, force);
    }
    if (cmd == "unpack") return do_unpack(argv[2], argv[3]);
    die("unknown command: " + cmd);
}
