// vcf_gtcodec — reversible genotype symbol coding for VCF body parts.
//
//   vcf_gtcodec pack   <in.vcfbody>  <out.vgt>
//   vcf_gtcodec unpack <in.vgt>      <out.vcfbody>
//
// Why
// ---
// In a genotype-dense VCF the sample block is ~99% of the body and is stored as
// 3-4 byte ASCII text per call ("0|0\t"), yet a whole chromosome typically uses
// only a handful of distinct genotype strings (17 across a 30k-row slice of
// 1000G chr22). Mapping each distinct genotype to a single byte shrinks that
// block ~4x before any entropy coding, which is worth ~60% on the panel/cohort
// archetypes.
//
// The block stays ROW-MAJOR on purpose. Transposing to column-major was
// measured and is worse for sparse panels: consecutive variant rows are nearly
// identical, so row-major keeps that redundancy inside an LZ window while a
// transpose scatters it thousands of bytes apart.
//
// Reversibility
// -------------
// Only applied when the part is uniformly `FORMAT=GT` with a constant column
// count and <=255 distinct genotypes. Anything else -> mode 0 passthrough, the
// bytes are stored verbatim. VCF fields cannot contain '\t' or '\n' (spec), so
// splitting on '\t' and rejoining reproduces the input byte for byte.
//
// Container ("VGT1", little-endian):
//   Byte[4] magic
//   u8      mode                0 = passthrough, 1 = GT-coded
//   mode 0: Byte[...]           the part, verbatim
//   mode 1: u32 nrows, u32 nsamples, u32 ndict, u32 dict_bytes, u64 prefix_bytes
//           Byte[dict_bytes]    ndict NUL-terminated genotype strings, code order
//           Byte[prefix_bytes]  columns 1..9 per row, tab-joined, '\n'-terminated
//           Byte[nrows*nsamples] u8 codes, row-major
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <string>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <unordered_map>
#include <vector>

namespace {

constexpr char kMagic[4] = { 'V', 'G', 'T', '1' };
constexpr uint32_t kMaxDict = 255;
constexpr int kFixedCols = 9; // CHROM POS ID REF ALT QUAL FILTER INFO FORMAT

[[noreturn]] void die(const std::string& m)
{
    std::fprintf(stderr, "vcf_gtcodec: %s\n", m.c_str());
    std::exit(1);
}

struct Mapped {
    const char* data = nullptr;
    size_t size      = 0;
    int fd           = -1;
    void* addr       = nullptr;
    explicit Mapped(const std::string& p)
    {
        fd = ::open(p.c_str(), O_RDONLY);
        if (fd < 0) die("cannot open " + p);
        struct stat st {};
        if (::fstat(fd, &st) != 0) die("cannot stat " + p);
        size = (size_t)st.st_size;
        if (size == 0) return;
        addr = ::mmap(nullptr, size, PROT_READ, MAP_PRIVATE, fd, 0);
        if (addr == MAP_FAILED) die("cannot mmap " + p);
        ::madvise(addr, size, MADV_SEQUENTIAL);
        data = (const char*)addr;
    }
    ~Mapped()
    {
        if (addr) ::munmap(addr, size);
        if (fd >= 0) ::close(fd);
    }
};

void putLE32(std::string& b, uint32_t v)
{
    for (int i = 0; i < 4; i++) b.push_back((char)((v >> (8 * i)) & 0xFF));
}
void putLE64(std::string& b, uint64_t v)
{
    for (int i = 0; i < 8; i++) b.push_back((char)((v >> (8 * i)) & 0xFF));
}
uint32_t getLE32(const uint8_t* p) { uint32_t v = 0; for (int i = 0; i < 4; i++) v |= (uint32_t)p[i] << (8 * i); return v; }
uint64_t getLE64(const uint8_t* p) { uint64_t v = 0; for (int i = 0; i < 8; i++) v |= (uint64_t)p[i] << (8 * i); return v; }

void writeAll(const std::string& path, const char* data, size_t n)
{
    FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) die("cannot open output " + path);
    if (n && std::fwrite(data, 1, n, f) != n) die("short write to " + path);
    std::fclose(f);
}

// Emit a passthrough container.
void emitPassthrough(const std::string& out, const char* data, size_t n)
{
    std::string hdr(kMagic, 4);
    hdr.push_back(0); // mode 0
    FILE* f = std::fopen(out.c_str(), "wb");
    if (!f) die("cannot open output " + out);
    std::fwrite(hdr.data(), 1, hdr.size(), f);
    if (n) std::fwrite(data, 1, n, f);
    std::fclose(f);
}

int cmdPack(const std::string& in, const std::string& out)
{
    Mapped m(in);
    const char* p = m.data;
    const size_t n = m.size;

    // Must be non-empty and newline-terminated to be a clean set of rows.
    if (n == 0 || p[n - 1] != '\n') { emitPassthrough(out, p, n); return 0; }

    std::unordered_map<std::string, uint8_t> dict;
    std::vector<std::string> dictOrder;
    std::string prefix;      // cols 1..9 per row, '\n'-terminated
    std::vector<uint8_t> gt; // row-major codes
    prefix.reserve(n / 16);

    uint32_t nrows = 0;
    int64_t nsamples = -1;
    bool ok = true;

    size_t i = 0;
    while (i < n && ok) {
        const char* lineBeg = p + i;
        const void* nl = std::memchr(p + i, '\n', n - i);
        if (!nl) { ok = false; break; }
        const char* lineEnd = (const char*)nl;
        size_t lineLen = (size_t)(lineEnd - lineBeg);
        i = (size_t)(lineEnd - p) + 1;

        // Walk the tab-separated fields.
        const char* fbeg = lineBeg;
        int col = 0;
        const char* prefixEnd = nullptr; // end of column 9
        int64_t thisSamples = 0;
        const char* cur = lineBeg;
        // First pass over columns.
        while (cur <= lineEnd) {
            const char* tab = (const char*)std::memchr(cur, '\t', (size_t)(lineEnd - cur));
            const char* fend = tab ? tab : lineEnd;
            col++;
            if (col == kFixedCols) {
                // column 9 is FORMAT and must be exactly "GT"
                if (!(fend - cur == 2 && cur[0] == 'G' && cur[1] == 'T')) { ok = false; break; }
                prefixEnd = fend;
            } else if (col > kFixedCols) {
                std::string g(cur, (size_t)(fend - cur));
                auto it = dict.find(g);
                uint8_t code;
                if (it == dict.end()) {
                    if (dictOrder.size() >= kMaxDict) { ok = false; break; }
                    code = (uint8_t)dictOrder.size();
                    dict.emplace(g, code);
                    dictOrder.push_back(g);
                } else {
                    code = it->second;
                }
                gt.push_back(code);
                thisSamples++;
            }
            if (!tab) break;
            cur = tab + 1;
        }
        if (!ok) break;
        if (col < kFixedCols || !prefixEnd) { ok = false; break; }
        if (nsamples < 0) nsamples = thisSamples;
        else if (thisSamples != nsamples) { ok = false; break; }
        if (nsamples == 0) { ok = false; break; } // no genotype block: nothing to gain

        prefix.append(fbeg, (size_t)(prefixEnd - fbeg));
        prefix.push_back('\n');
        nrows++;
        if (nrows == 0xFFFFFFFFu) { ok = false; break; }
    }

    if (!ok || nrows == 0 || nsamples <= 0) { emitPassthrough(out, p, n); return 0; }

    std::string dictBlob;
    for (const auto& s : dictOrder) { dictBlob.append(s); dictBlob.push_back('\0'); }

    std::string hdr(kMagic, 4);
    hdr.push_back(1); // mode 1
    putLE32(hdr, nrows);
    putLE32(hdr, (uint32_t)nsamples);
    putLE32(hdr, (uint32_t)dictOrder.size());
    putLE32(hdr, (uint32_t)dictBlob.size());
    putLE64(hdr, (uint64_t)prefix.size());

    FILE* f = std::fopen(out.c_str(), "wb");
    if (!f) die("cannot open output " + out);
    std::fwrite(hdr.data(), 1, hdr.size(), f);
    if (!dictBlob.empty()) std::fwrite(dictBlob.data(), 1, dictBlob.size(), f);
    if (!prefix.empty()) std::fwrite(prefix.data(), 1, prefix.size(), f);
    if (!gt.empty()) std::fwrite(gt.data(), 1, gt.size(), f);
    std::fclose(f);
    return 0;
}

int cmdUnpack(const std::string& in, const std::string& out)
{
    Mapped m(in);
    const uint8_t* p = (const uint8_t*)m.data;
    size_t n = m.size;
    if (n < 5 || std::memcmp(p, kMagic, 4) != 0) die("not a VGT1 container: " + in);
    uint8_t mode = p[4];
    size_t pos = 5;

    if (mode == 0) { writeAll(out, (const char*)p + pos, n - pos); return 0; }
    if (mode != 1) die("unknown VGT1 mode");

    if (pos + 24 > n) die("truncated VGT1 header");
    uint32_t nrows      = getLE32(p + pos); pos += 4;
    uint32_t nsamples   = getLE32(p + pos); pos += 4;
    uint32_t ndict      = getLE32(p + pos); pos += 4;
    uint32_t dictBytes  = getLE32(p + pos); pos += 4;
    uint64_t prefixLen  = getLE64(p + pos); pos += 8;

    if (pos + dictBytes > n) die("truncated dictionary");
    std::vector<const char*> dict(ndict, nullptr);
    std::vector<uint32_t> dlen(ndict, 0);
    {
        const char* d = (const char*)p + pos;
        size_t off = 0;
        for (uint32_t k = 0; k < ndict; ++k) {
            const void* z = std::memchr(d + off, '\0', dictBytes - off);
            if (!z) die("corrupt dictionary");
            dict[k] = d + off;
            dlen[k] = (uint32_t)((const char*)z - (d + off));
            off = (size_t)((const char*)z - d) + 1;
        }
    }
    pos += dictBytes;

    if (pos + prefixLen > n) die("truncated prefix block");
    const char* pref = (const char*)p + pos;
    pos += prefixLen;

    uint64_t gtCount = (uint64_t)nrows * (uint64_t)nsamples;
    if (pos + gtCount > n) die("truncated genotype block");
    const uint8_t* gt = p + pos;

    FILE* f = std::fopen(out.c_str(), "wb");
    if (!f) die("cannot open output " + out);
    // Buffered rebuild: prefix line, then the genotype strings tab-separated.
    std::string buf;
    buf.reserve(1 << 22);
    size_t po = 0;
    uint64_t gi = 0;
    for (uint32_t r = 0; r < nrows; ++r) {
        const void* nl = std::memchr(pref + po, '\n', (size_t)prefixLen - po);
        if (!nl) die("corrupt prefix block");
        size_t plen = (size_t)((const char*)nl - (pref + po));
        buf.append(pref + po, plen);
        po += plen + 1;
        for (uint32_t s = 0; s < nsamples; ++s) {
            uint8_t c = gt[gi++];
            if (c >= ndict) die("genotype code out of range");
            buf.push_back('\t');
            buf.append(dict[c], dlen[c]);
        }
        buf.push_back('\n');
        if (buf.size() > (1u << 22)) { std::fwrite(buf.data(), 1, buf.size(), f); buf.clear(); }
    }
    if (!buf.empty()) std::fwrite(buf.data(), 1, buf.size(), f);
    std::fclose(f);
    return 0;
}

} // namespace

int main(int argc, char** argv)
{
    if (argc != 4) {
        std::fprintf(stderr,
                "Usage:\n  %s pack   <in.vcfbody> <out.vgt>\n"
                "  %s unpack <in.vgt>     <out.vcfbody>\n",
                argv[0], argv[0]);
        return 2;
    }
    std::string cmd = argv[1];
    if (cmd == "pack") return cmdPack(argv[2], argv[3]);
    if (cmd == "unpack") return cmdUnpack(argv[2], argv[3]);
    std::fprintf(stderr, "vcf_gtcodec: unknown command '%s'\n", cmd.c_str());
    return 2;
}
