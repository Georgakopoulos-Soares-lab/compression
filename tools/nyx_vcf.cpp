// nyx_vcf — byte-exact, format-aware VCF compression on OpenZL.
//
//   nyx_vcf compress   [opts] <in.vcf[.gz]> <out.nvcf>
//   nyx_vcf decompress [opts] <in.nvcf>     <out.vcf>
//   nyx_vcf inspect    <in.nvcf>
//
// Design
// ------
// The body is cut into independently-decodable blocks. Each block is parsed
// into a bundle of homogeneous streams, and the bundle is handed to OpenZL as
// one multi-input frame. Nothing here compresses anything itself; the whole
// job of this file is to turn one interleaved record stream into many
// homogeneous ones that an entropy backend can actually model.
//
// The streams, per block:
//
//   chrom,id,ref,alt,qual,filter   NUL-separated text, one value per variant
//   pos.delta                      zigzag varint delta of POS (canonical ints)
//   pos.exc                        verbatim POS text where the int is not canonical
//   info.keyset / info.dict        the ordered (key,has'=') pattern of each INFO
//   info.v.<KEY>                   one value stream per INFO key
//   fmt.id / fmt.dict              FORMAT string per variant
//   gt.runlen / gt.runsym / gt.nruns / gt.first
//                                  the genotype matrix after a haplotype-level
//                                  positional Burrows-Wheeler transform, run-
//                                  length coded
//   gt.sep, gt.ploidy, gt.exc      phasing / ploidy / anything GT-shaped that
//                                  did not parse, so the transform stays exact
//   fs.<fmtid>.<j>                 column-major stream per non-GT FORMAT subfield
//
// Exactness
// ---------
// Every block is decoded again in-process and compared to the source bytes
// before it is accepted (`--no-selfcheck` disables). A block that does not
// reproduce exactly is stored as a single verbatim stream instead. The
// transform is therefore exact on any input, including ones it models badly.

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cctype>
#include <cstring>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <vector>

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <zlib.h>

#include "openzl/zl_compress.h"
#include "openzl/zl_compressor.h"
#include "openzl/zl_compressor_serialization.h"
#include "openzl/zl_decompress.h"
#include "openzl/codecs/zl_generic.h"

// ---------------------------------------------------------------- utilities

namespace {

constexpr char     kMagic[8]   = {'N','Y','X','V','C','F','\0','1'};
constexpr uint32_t kFormatVer  = 16;   // OpenZL frame format version

[[noreturn]] void die(const std::string& m) {
    std::fprintf(stderr, "nyx_vcf: %s\n", m.c_str());
    std::exit(1);
}

inline void put_varint(std::string& o, uint64_t v) {
    while (v >= 0x80) { o += char((v & 0x7f) | 0x80); v >>= 7; }
    o += char(v);
}
inline uint64_t get_varint(const char*& p, const char* end) {
    uint64_t v = 0; int s = 0;
    while (p < end) {
        uint8_t b = (uint8_t)*p++;
        v |= (uint64_t)(b & 0x7f) << s;
        if (!(b & 0x80)) return v;
        s += 7;
        if (s > 63) break;
    }
    die("corrupt varint");
}
inline uint64_t zigzag(int64_t v)   { return ((uint64_t)v << 1) ^ (uint64_t)(v >> 63); }
inline int64_t  unzigzag(uint64_t v){ return (int64_t)(v >> 1) ^ -(int64_t)(v & 1); }

inline void put_u32(std::string& o, uint32_t v) { o.append((const char*)&v, 4); }
inline void put_u64(std::string& o, uint64_t v) { o.append((const char*)&v, 8); }

// Parse a non-negative integer that round-trips to exactly the same text
// (no leading zeros, no sign, fits in 63 bits). Returns false otherwise.
inline bool parse_canonical_u64(const char* s, size_t n, uint64_t& out) {
    if (n == 0 || n > 19) return false;
    if (n > 1 && s[0] == '0') return false;
    uint64_t v = 0;
    for (size_t i = 0; i < n; i++) {
        if (s[i] < '0' || s[i] > '9') return false;
        v = v * 10 + (uint64_t)(s[i] - '0');
    }
    out = v;
    return true;
}

// ------------------------------------------------------------ stream bundle

// A named byte stream. Order is fixed by the encoder and rebuilt identically by
// the decoder from the block directory, so names are only for inspect/debug.
// Stream encodings. Numeric streams are handed to OpenZL as typed numeric
// inputs so it can apply its integer transforms instead of treating them as
// opaque bytes.
enum StreamType : uint8_t {
    ST_SERIAL   = 0,   // opaque bytes
    ST_NUM2     = 1,   // native u16 values
    ST_NUM4     = 2,   // native u32 values
    ST_NUM8     = 3,   // native u64 values
    ST_TEXTNUM4 = 4,   // NUL-separated decimal text, stored as u32
    ST_TEXTNUM8 = 5,   // NUL-separated decimal text, stored as u64
    ST_DECIMAL  = 6,   // fixed-point decimal text, split into <name>#s/#i/#L/#f
    ST_INTLIST  = 7,   // comma-separated integer lists, split into <name>#n/#v
    ST_DICT     = 8,   // low-cardinality text, split into <name>#d/#x
    ST_PIPE     = 9,   // '|'-separated annotation fields, split into <name>#rn/#fn/#p<j>
    ST_NUMDELTA = 10,  // integer text stored as zigzag varint deltas
};
inline size_t stWidth(uint8_t t) {
    switch (t) {
        case ST_NUM2: return 2;
        case ST_NUM4: case ST_TEXTNUM4: return 4;
        case ST_NUM8: case ST_TEXTNUM8: return 8;
        default: return 0;
    }
}

struct Bundle {
    std::vector<std::string> names;
    std::vector<uint8_t> type;
    // deque, not vector: encodeBlock holds references to streams while it is
    // still adding new ones (one per INFO key), and vector would reallocate
    // those references out from under it.
    std::deque<std::string> data;

    std::string& at(const std::string& name) {
        auto it = index_.find(name);
        if (it != index_.end()) return data[it->second];
        index_.emplace(name, names.size());
        names.push_back(name);
        type.push_back(ST_SERIAL);
        data.emplace_back();
        return data.back();
    }
    void setType(const std::string& name, uint8_t t) {
        auto it = index_.find(name);
        if (it != index_.end()) type[it->second] = t;
    }
    size_t size() const { return names.size(); }
   private:
    std::unordered_map<std::string, size_t> index_;
};

// Streams are grouped into a small fixed set of classes, and one model is
// trained per class per archetype. Classes are deliberately coarse and never
// mention a particular INFO or FORMAT key, so a file carrying keys the trainer
// never saw still routes every stream to a shipped model.
inline const char* streamClass(const std::string& n, uint8_t type) {
    if (type == ST_NUMDELTA) return "varint";
    if (type == ST_TEXTNUM4) return "num4";
    if (type == ST_TEXTNUM8) return "num8";
    if (n == "@dir")  return "dir";
    if (n == "@raw")  return "raw";
    if (n == "gt.runlen") return "gt.runlen";
    if (n == "gt.runsym") return "gt.runsym";
    if (n == "gt.nruns")  return "gt.nruns";
    if (n == "gt.sep")    return "gt.sep";
    if (n == "gt.exc")    return "gt.exc";
    if (n == "pos.delta") return "pos.delta";
    if (n == "pos.flag")  return "flag";
    if (n == "info.keyset" || n == "fmt.id")   return "idx";
    if (n == "info.dict"   || n == "fmt.dict") return "meta";
    const size_t h = n.rfind('#');
    if (h != std::string::npos) {
        const std::string suf = n.substr(h);
        if (suf == "#x" || suf == "#rn")                       return "idx";
        if (suf == "#i" || suf == "#f")                        return "varint";
        if (suf.compare(0, 2, "#v") == 0)                      return "varint";
        if (suf == "#n" || suf == "#L" || suf == "#s" || suf == "#fn") return "flag";
    }
    return "text";
}

// ------------------------------------------------------------------ OpenZL

// One compressor per thread; graphs are stateless so this only exists because
// ZL_CCtx is not thread safe.
class Codec {
   public:
    // `modelDir` holds one serialized OpenZL compressor per stream class,
    // named <class>.zlc. Classes with no model fall back to the generic graph.
    // Decompression never needs any of this: OpenZL frames are self-describing.
    explicit Codec(const std::string& modelDir, int level = 0)
        : modelDir_(modelDir), level_(level) {
        cctx_ = ZL_CCtx_create();
        dctx_ = ZL_DCtx_create();
        if (!cctx_ || !dctx_) die("OpenZL context allocation failed");
        generic_ = makeGeneric();
    }
    ~Codec() {
        for (auto& kv : models_) if (kv.second) ZL_Compressor_free(kv.second);
        if (generic_) ZL_Compressor_free(generic_);
        if (cctx_) ZL_CCtx_free(cctx_);
        if (dctx_) ZL_DCtx_free(dctx_);
    }
    Codec(const Codec&) = delete;
    Codec& operator=(const Codec&) = delete;

    // Compresses every stream into its own frame, prefixed by a length table so
    // the decoder can split them again.
    std::string compressBundle(const Bundle& b) {
        std::string body, table;
        put_varint(table, b.size());
        for (size_t i = 0; i < b.data.size(); i++) {
            std::string f = compressStream(b.data[i], streamClass(b.names[i], b.type[i]));
            put_varint(table, f.size());
            body += f;
        }
        return table + body;
    }

    std::vector<std::string> decompressBundle(const char* p, size_t n, size_t nStreams) {
        const char* e = p + n;
        const uint64_t got = get_varint(p, e);
        if (got != nStreams) die("stream count mismatch in block");
        std::vector<uint64_t> lens(nStreams);
        for (size_t i = 0; i < nStreams; i++) lens[i] = get_varint(p, e);
        std::vector<std::string> out(nStreams);
        for (size_t i = 0; i < nStreams; i++) {
            if ((uint64_t)(e - p) < lens[i]) die("truncated block");
            out[i] = decompressStream(p, lens[i]);
            p += lens[i];
        }
        return out;
    }

    // Compresses one stream on its own; used by `stats`.
    size_t compressOne(const std::string& d, const std::string& name = "text",
                       uint8_t type = ST_SERIAL) {
        return compressStream(d, streamClass(name, type)).size();
    }

   private:
    // Compresses one stream with the generic profile and, if a model is shipped
    // for its class, with that model too -- keeping whichever is smaller. A
    // model trained on one archetype can be actively worse on another (measured:
    // -5% on a single-sample VCF), so it has to earn its use on each stream
    // rather than be trusted because it exists.
    std::string compressStream(const std::string& d, const char* cls) {
        if (d.empty()) return std::string();
        std::string best = runGraph(d, generic_);
        ZL_Compressor* c = modelFor(cls);
        if (c) {
            std::string alt = runGraph(d, c, /*mayFail=*/true);
            if (!alt.empty() && alt.size() < best.size()) best = std::move(alt);
        }
        return best;
    }

    // Returns the compressed frame, or an empty string if `mayFail` and the
    // graph rejected the input (a trained graph can reject data shaped unlike
    // its training set).
    std::string runGraph(const std::string& d, ZL_Compressor* c, bool mayFail = false) {
        std::string out;
        out.resize(ZL_compressBound(d.size()) + 4096);
        ZL_TypedRef* ref = ZL_TypedRef_createSerial(d.data(), d.size());
        const ZL_TypedRef* one[1] = {ref};
        check(ZL_CCtx_refCompressor(cctx_, c), "bind compressor");
        // OpenZL's default is ZL_COMPRESSIONLEVEL_DEFAULT (6), passed straight
        // to the zstd backend. 0 here leaves that default exactly as it was.
        if (level_ > 0)
            check(ZL_CCtx_setParameter(cctx_, ZL_CParam_compressionLevel, level_),
                  "set compression level");
        ZL_Report r = ZL_CCtx_compressMultiTypedRef(cctx_, &out[0], out.size(), one, 1);
        ZL_TypedRef_free(ref);
        if (ZL_isError(r)) {
            if (mayFail) return std::string();
            die(std::string("OpenZL compression failed: ") + ZL_ErrorCode_toString(ZL_errorCode(r)));
        }
        out.resize(ZL_validResult(r));
        return out;
    }

    std::string decompressStream(const char* p, size_t n) {
        if (n == 0) return std::string();
        ZL_TypedBuffer* tb = ZL_TypedBuffer_create();
        if (!tb) die("OpenZL buffer allocation failed");
        ZL_Report r = ZL_DCtx_decompressMultiTBuffer(dctx_, &tb, 1, p, n);
        if (ZL_isError(r)) {
            ZL_TypedBuffer_free(tb);
            die(std::string("OpenZL decompression failed: ") + ZL_ErrorCode_toString(ZL_errorCode(r)));
        }
        std::string s((const char*)ZL_TypedBuffer_rPtr(tb), ZL_TypedBuffer_byteSize(tb));
        ZL_TypedBuffer_free(tb);
        return s;
    }

    ZL_Compressor* makeGeneric() {
        ZL_Compressor* c = ZL_Compressor_create();
        if (!c) die("OpenZL compressor allocation failed");
        check(ZL_Compressor_selectStartingGraphID(c, ZL_GRAPH_COMPRESS_GENERIC), "select generic graph");
        check(ZL_Compressor_setParameter(c, ZL_CParam_formatVersion, (int)kFormatVer), "set format version");
        return c;
    }

    ZL_Compressor* modelFor(const char* cls) {
        auto it = models_.find(cls);
        if (it != models_.end()) return it->second;
        ZL_Compressor* c = nullptr;
        if (!modelDir_.empty()) {
            const std::string path = modelDir_ + "/" + cls + ".zlc";
            FILE* f = std::fopen(path.c_str(), "rb");
            if (f) {
                std::string blob;
                char buf[1 << 16];
                size_t k;
                while ((k = std::fread(buf, 1, sizeof buf, f)) > 0) blob.append(buf, k);
                std::fclose(f);
                c = ZL_Compressor_create();
                ZL_CompressorDeserializer* d = ZL_CompressorDeserializer_create();
                bool ok = c && d;
                if (ok) {
                    ZL_Report r = ZL_CompressorDeserializer_deserialize(
                            d, c, blob.data(), blob.size(), nullptr, 0);
                    ok = !ZL_isError(r);
                }
                if (d) ZL_CompressorDeserializer_free(d);
                if (ok) {
                    check(ZL_Compressor_setParameter(c, ZL_CParam_formatVersion, (int)kFormatVer),
                          "set format version");
                } else {
                    if (c) ZL_Compressor_free(c);
                    c = nullptr;
                }
            }
        }
        models_.emplace(cls, c);
        return c;
    }

    static void check(ZL_Report r, const char* what) {
        if (ZL_isError(r)) die(std::string("OpenZL: ") + what + " failed");
    }

    std::string modelDir_;
    int level_ = 0;
    ZL_CCtx* cctx_ = nullptr;
    ZL_DCtx* dctx_ = nullptr;
    ZL_Compressor* generic_ = nullptr;
    std::unordered_map<std::string, ZL_Compressor*> models_;
};

}  // namespace

// ================================================================= encoding

namespace {

constexpr uint8_t kMissingAllele = 254;   // '.' inside a GT
constexpr uint8_t kMaxAllele     = 250;

struct Field { const char* p; uint32_t n; };

// One row of a block: the nine fixed columns plus an unsplit view of the
// sample block. The sample columns are deliberately NOT indexed -- for a
// 2504-sample panel an index over every field costs more memory than the text
// it points at, so samples are re-scanned inline where they are consumed.
struct RowRef {
    Field fixed[9];
    const char* samples = nullptr;   // first sample field, or nullptr
    uint32_t samplesLen = 0;
};

// Splits the block into rows. Returns false if any row's column count differs
// from `expectCols`, which sends the block down the verbatim path.
bool parseRows(const char* text, size_t len, uint32_t expectCols, size_t nsamp,
               std::vector<RowRef>& out) {
    out.clear();
    const uint32_t nFixed = expectCols < 9 ? expectCols : 9;
    const char* p = text;
    const char* end = text + len;
    while (p < end) {
        const char* nl = (const char*)std::memchr(p, '\n', (size_t)(end - p));
        const char* stop = nl ? nl : end;
        RowRef r;
        const char* q = p;
        uint32_t c = 0;
        for (; c < nFixed; c++) {
            const char* t = (const char*)std::memchr(q, '\t', (size_t)(stop - q));
            if (!t) {
                if (c + 1 != expectCols) return false;
                r.fixed[c] = {q, (uint32_t)(stop - q)};
                q = stop;
                c++;
                break;
            }
            r.fixed[c] = {q, (uint32_t)(t - q)};
            q = t + 1;
        }
        if (c != nFixed) return false;
        if (expectCols > 9) {
            if (q > stop) return false;
            r.samples = q;
            r.samplesLen = (uint32_t)(stop - q);
            // column count check: nsamp fields => nsamp-1 tabs
            size_t tabs = 0;
            for (const char* z = q; z < stop; z++) if (*z == '\t') tabs++;
            if (tabs + 1 != nsamp) return false;
        } else if (q != stop) {
            return false;
        }
        out.push_back(r);
        p = nl ? nl + 1 : end;
    }
    return true;
}

// ---- small helpers for text streams --------------------------------------

inline void putField(std::string& s, const Field& f) {
    s.append(f.p, f.n);
    s += '\0';
}

// A block-local dictionary of repeated strings (FORMAT strings, INFO keysets).
struct Dict {
    std::unordered_map<std::string, uint32_t> index;
    std::vector<std::string> items;
    uint32_t intern(const std::string& s) {
        auto it = index.find(s);
        if (it != index.end()) return it->second;
        uint32_t id = (uint32_t)items.size();
        index.emplace(s, id);
        items.push_back(s);
        return id;
    }
    void serialize(std::string& out) const {
        put_varint(out, items.size());
        for (const auto& s : items) { put_varint(out, s.size()); out += s; }
    }
    static std::vector<std::string> deserialize(const char*& p, const char* end) {
        uint64_t n = get_varint(p, end);
        std::vector<std::string> v;
        v.reserve(n);
        for (uint64_t i = 0; i < n; i++) {
            uint64_t l = get_varint(p, end);
            if ((size_t)(end - p) < l) die("corrupt dictionary");
            v.emplace_back(p, l);
            p += l;
        }
        return v;
    }
};

// An INFO keyset: the ordered list of keys and whether each carried '='.
std::string keysetSignature(const Field& info, std::vector<std::pair<Field,bool>>& parts) {
    parts.clear();
    std::string sig;
    const char* p = info.p;
    const char* end = info.p + info.n;
    while (true) {
        const char* semi = (const char*)std::memchr(p, ';', (size_t)(end - p));
        const char* stop = semi ? semi : end;
        const char* eq = (const char*)std::memchr(p, '=', (size_t)(stop - p));
        if (eq) {
            sig.append(p, (size_t)(eq - p));
            sig += '=';
            parts.push_back({{eq + 1, (uint32_t)(stop - eq - 1)}, true});
        } else {
            sig.append(p, (size_t)(stop - p));
            parts.push_back({{nullptr, 0}, false});
        }
        sig += '\x01';
        if (!semi) break;
        p = semi + 1;
    }
    return sig;
}

// ---- the genotype matrix -------------------------------------------------

// Parses one GT subfield into up to `ploidy` allele codes.
// Returns false when the value is not a clean <a><sep><a>... of that ploidy.
inline bool parseGT(const char* p, uint32_t n, unsigned ploidy,
                    uint8_t* alleles, char& sep) {
    unsigned k = 0;
    uint32_t i = 0;
    sep = 0;
    while (i < n && k < ploidy) {
        if (p[i] == '.') {
            alleles[k++] = kMissingAllele;
            i++;
        } else {
            uint32_t v = 0, d = 0;
            while (i < n && p[i] >= '0' && p[i] <= '9') { v = v * 10 + (uint32_t)(p[i] - '0'); i++; d++; }
            if (d == 0 || d > 3 || v > kMaxAllele) return false;
            alleles[k++] = (uint8_t)v;
        }
        if (i == n) break;
        if (p[i] != '|' && p[i] != '/') return false;
        if (sep == 0) sep = p[i];
        else if (sep != p[i]) return false;   // mixed separators -> exception
        i++;
    }
    return i == n && k == ploidy;
}

// Haplotype-level positional Burrows-Wheeler transform, applied one variant at
// a time so the working set is O(haplotypes), not O(block).
struct PBWT {
    size_t NH = 0;
    std::vector<uint32_t> ppa, nxt;
    std::vector<uint32_t> cnt, off;

    void reset(size_t nh) {
        NH = nh;
        ppa.resize(nh);
        nxt.resize(nh);
        for (size_t i = 0; i < nh; i++) ppa[i] = (uint32_t)i;
        cnt.assign(256, 0);
        off.assign(256, 0);
    }
    // Permutes `row` into `perm` and advances the ordering.
    void forward(const uint8_t* row, uint8_t* perm) {
        std::fill(cnt.begin(), cnt.end(), 0u);
        for (size_t i = 0; i < NH; i++) { uint8_t s = row[ppa[i]]; perm[i] = s; cnt[s]++; }
        advance(perm);
    }
    // Inverse: given the permuted row, recover the original and advance.
    void inverse(const uint8_t* perm, uint8_t* row) {
        std::fill(cnt.begin(), cnt.end(), 0u);
        for (size_t i = 0; i < NH; i++) { row[ppa[i]] = perm[i]; cnt[perm[i]]++; }
        advance(perm);
    }
   private:
    void advance(const uint8_t* perm) {
        uint32_t acc = 0;
        for (size_t s = 0; s < 256; s++) { off[s] = acc; acc += cnt[s]; }
        for (size_t i = 0; i < NH; i++) nxt[off[perm[i]]++] = ppa[i];
        ppa.swap(nxt);
    }
};

}  // namespace

namespace {

// Which transforms the encoder is allowed to apply. Everything is on by
// default; the switches exist so the paper can attribute ratio to each stage.
// They affect the encoder only -- the directory records what was actually done,
// so any archive decodes without knowing which switches were set.
struct Ablation {
    bool pbwt = true;      // haplotype-level PBWT before run-length coding
    bool values = true;    // integer / delta / decimal / int-list rewrites
    bool dict = true;      // low-cardinality text dictionaries
    bool annot = true;     // VEP/SnpEff record+field split
    bool infoSplit = true; // per-INFO-key value streams
};

// Returns true for the NUL-separated text streams the value transforms may
// rewrite. Everything else (varint streams, dictionaries, the directory) is
// left alone.
bool isTextStream(const std::string& n) {
    return n.compare(0, 7, "info.v.") == 0
        || n.compare(0, 3, "fs.") == 0
        || n.compare(0, 4, "col.") == 0
        || n == "qual" || n == "id" || n == "chrom"
        || n == "ref"  || n == "alt" || n == "filter";
}

// VEP/SnpEff-style annotations (CSQ, ANN) pack a list of per-transcript
// records into one INFO value: records separated by ',', fields within a
// record by '|'. Splitting to one stream per field position turns a single
// high-entropy blob into columns that each repeat heavily across records.
//
// Emitted as <name>#rn (records per value), <name>#fn (fields per record) and
// <name>#p<j> (field j of every record long enough to have one).
void splitPipeStreams(Bundle& b, const Ablation& ab) {
    if (!ab.annot) return;
    const size_t n0 = b.names.size();
    for (size_t i = 0; i < n0; i++) {
        const std::string n = b.names[i];
        if (b.type[i] != ST_SERIAL || !isTextStream(n)) continue;
        const std::string& d = b.data[i];
        if (d.size() < (64u << 10)) continue;
        if (std::memchr(d.data(), '|', d.size()) == nullptr) continue;

        // Pass 1: shape and viability.
        std::string recCount, fieldCount;
        size_t maxF = 0, nVals = 0, nRecs = 0;
        bool ok = true;
        const char* p = d.data();
        const char* e = d.data() + d.size();
        while (p < e) {
            const char* z = (const char*)std::memchr(p, '\0', (size_t)(e - p));
            if (!z) { ok = false; break; }
            size_t recs = 0;
            const char* r = p;
            while (r <= z) {
                const char* comma = (const char*)std::memchr(r, ',', (size_t)(z - r));
                const char* rend = comma ? comma : z;
                size_t f = 1;
                for (const char* c = r; c < rend; c++) if (*c == '|') f++;
                if (f > 255) { ok = false; break; }
                fieldCount += (char)f;
                if (f > maxF) maxF = f;
                recs++;
                nRecs++;
                if (!comma) break;
                r = comma + 1;
            }
            if (!ok) break;
            put_varint(recCount, recs);
            nVals++;
            p = z + 1;
        }
        // Only worth it for genuinely large multi-field values; short ones
        // (1000G's "AA=.|||") compress better left whole.
        if (!ok || maxF < 2 || nVals == 0 || d.size() / nVals < 24) continue;
        // If the whole values repeat enough for the dictionary transform to
        // catch them, leave them alone: splitting scatters the repetition and
        // measures worse (ClinVar loses 3.4% when both are applied).
        {
            std::unordered_map<std::string_view, uint32_t> distinct;
            const char* q = d.data();
            while (q < e) {
                const char* z = (const char*)std::memchr(q, '\0', (size_t)(e - q));
                if (!z) break;
                distinct.emplace(std::string_view(q, (size_t)(z - q)), 0u);
                q = z + 1;
            }
            if (distinct.size() * 4 <= nVals) continue;
        }

        std::vector<std::string> parts(maxF);
        p = d.data();
        while (p < e) {
            const char* z = (const char*)std::memchr(p, '\0', (size_t)(e - p));
            const char* r = p;
            while (r <= z) {
                const char* comma = (const char*)std::memchr(r, ',', (size_t)(z - r));
                const char* rend = comma ? comma : z;
                const char* q = r;
                size_t j = 0;
                while (q <= rend) {
                    const char* c = (const char*)std::memchr(q, '|', (size_t)(rend - q));
                    const char* stop = c ? c : rend;
                    parts[j].append(q, (size_t)(stop - q));
                    parts[j] += '\0';
                    j++;
                    if (!c) break;
                    q = c + 1;
                }
                if (!comma) break;
                r = comma + 1;
            }
            p = z + 1;
        }
        b.data[i].clear();
        b.type[i] = ST_PIPE;
        b.at(n + "#rn") = std::move(recCount);
        b.at(n + "#fn") = std::move(fieldCount);
        for (size_t j = 0; j < maxF; j++)
            b.at(n + "#p" + std::to_string(j)) = std::move(parts[j]);
    }
}

// A NUL-separated text stream whose every value is a canonical unsigned
// integer is rewritten as a fixed-width numeric stream. Values that would not
// re-render to the identical text (leading zeros, signs, overflow) disqualify
// the whole stream, so the rewrite is always exactly reversible.
//
// Streams that are not integers but are fixed-point decimals (allele
// frequencies, mostly) are split into sign / integer part / fraction length /
// fraction value, which separates four very different distributions that are
// otherwise interleaved character by character.
void numerifyStreams(Bundle& b, const Ablation& ab) {
    if (!ab.values && !ab.dict) return;
    const size_t n0 = b.names.size();   // do not revisit streams added below
    for (size_t i = 0; i < n0; i++) {
        const std::string n = b.names[i];
        // NUL-separated text streams, including the '|'-split sub-streams
        const size_t hash = n.rfind('#');
        const bool pipePart = hash != std::string::npos
                           && n.compare(hash, 2, "#p") == 0 && n.size() > hash + 2
                           && std::isdigit((unsigned char)n[hash + 2]);
        const bool candidate = pipePart || isTextStream(n);
        if (!candidate || b.type[i] != ST_SERIAL) continue;
        const std::string& d = b.data[i];
        if (d.empty()) continue;

        // pass 1: all canonical unsigned integers?
        std::vector<uint64_t> vals;
        uint64_t maxv = 0;
        bool allInt = true;
        const char* p = d.data();
        const char* e = d.data() + d.size();
        while (p < e) {
            const char* z = (const char*)std::memchr(p, '\0', (size_t)(e - p));
            if (!z) { allInt = false; break; }
            uint64_t v;
            if (!parse_canonical_u64(p, (size_t)(z - p), v)) { allInt = false; break; }
            vals.push_back(v);
            if (v > maxv) maxv = v;
            p = z + 1;
        }
        if (allInt && !vals.empty() && ab.values) {
            const bool wide = maxv > 0xFFFFFFFFull;
            const size_t w = wide ? 8 : 4;
            std::string fixed;
            fixed.resize(vals.size() * w);
            if (wide) std::memcpy(&fixed[0], vals.data(), fixed.size());
            else for (size_t k = 0; k < vals.size(); k++) {
                uint32_t v32 = (uint32_t)vals[k];
                std::memcpy(&fixed[k * 4], &v32, 4);
            }
            // Coordinate-like keys (INFO END above all) are monotone, so their
            // successive differences are tiny where the absolute values are not.
            std::string delta;
            uint64_t prev = 0;
            for (uint64_t v : vals) {
                put_varint(delta, zigzag((int64_t)v - (int64_t)prev));
                prev = v;
            }
            if (delta.size() < fixed.size()) {
                b.data[i] = std::move(delta);
                b.type[i] = ST_NUMDELTA;
            } else {
                if (fixed.size() > d.size() * 2) continue;
                b.data[i] = std::move(fixed);
                b.type[i] = wide ? ST_TEXTNUM8 : ST_TEXTNUM4;
            }
            continue;
        }

        // pass 2: fixed-point decimals, optionally signed
        std::string sg, ip, fl, fv;
        bool allDec = true;
        size_t count = 0;
        p = d.data();
        while (p < e) {
            const char* z = (const char*)std::memchr(p, '\0', (size_t)(e - p));
            if (!z) { allDec = false; break; }
            const char* q = p;
            bool neg = false;
            if (q < z && *q == '-') { neg = true; q++; }
            const char* dot = (const char*)std::memchr(q, '.', (size_t)(z - q));
            if (!dot) { allDec = false; break; }
            uint64_t iv, fvv;
            const size_t fracLen = (size_t)(z - dot - 1);
            if (!parse_canonical_u64(q, (size_t)(dot - q), iv) ||
                fracLen == 0 || fracLen > 18) { allDec = false; break; }
            // the fraction may carry leading zeros, so parse it digit-wise and
            // keep its length; that pair re-renders the text exactly
            fvv = 0;
            bool okf = true;
            for (const char* c = dot + 1; c < z; c++) {
                if (*c < '0' || *c > '9') { okf = false; break; }
                fvv = fvv * 10 + (uint64_t)(*c - '0');
            }
            if (!okf) { allDec = false; break; }
            sg += (char)(neg ? 1 : 0);
            put_varint(ip, iv);
            fl += (char)fracLen;
            put_varint(fv, fvv);
            count++;
            p = z + 1;
        }
        if (allDec && count > 0 && ab.values) {
            b.data[i].clear();
            b.type[i] = ST_DECIMAL;
            b.at(n + "#s") = std::move(sg);
            b.at(n + "#i") = std::move(ip);
            b.at(n + "#L") = std::move(fl);
            b.at(n + "#f") = std::move(fv);
            continue;
        }

        // pass 3: comma-separated integer lists (AD, ADALL, PL, multiallelic AC).
        // Element counts go to their own stream and each element *position*
        // gets its own stream -- PL[0] is almost always 0 while PL[1] and PL[2]
        // are not, and interleaving them hides that from the entropy stage.
        constexpr size_t kMaxElem = 64;
        std::string cn;
        std::vector<std::string> cv;
        bool allList = true;
        size_t lcount = 0;
        p = d.data();
        while (p < e) {
            const char* z = (const char*)std::memchr(p, '\0', (size_t)(e - p));
            if (!z) { allList = false; break; }
            size_t nElem = 0;
            const char* q = p;
            while (q <= z) {
                const char* c = (const char*)std::memchr(q, ',', (size_t)(z - q));
                const char* stop = c ? c : z;
                uint64_t v;
                if (!parse_canonical_u64(q, (size_t)(stop - q), v)) { allList = false; break; }
                if (nElem >= kMaxElem) { allList = false; break; }
                if (cv.size() <= nElem) cv.resize(nElem + 1);
                put_varint(cv[nElem], v);
                nElem++;
                if (!c) break;
                q = c + 1;
            }
            if (!allList || nElem == 0) { allList = false; break; }
            cn += (char)nElem;
            lcount++;
            p = z + 1;
        }
        if (allList && lcount > 0 && ab.values) {
            b.data[i].clear();
            b.type[i] = ST_INTLIST;
            b.at(n + "#n") = std::move(cn);
            for (size_t j = 0; j < cv.size(); j++)
                b.at(n + "#v" + std::to_string(j)) = std::move(cv[j]);
            continue;
        }

        // pass 4: low-cardinality text becomes a dictionary plus an index
        // stream. Long repeated values (annotation lists, callset names) stop
        // being re-matched by the LZ stage and become one small integer each.
        if (!ab.dict) continue;
        {
            std::unordered_map<std::string, uint32_t> seen;
            std::vector<const std::string*> order;
            std::string idx;
            size_t total = 0;
            bool ok = true;
            p = d.data();
            while (p < e) {
                const char* z = (const char*)std::memchr(p, '\0', (size_t)(e - p));
                if (!z) { ok = false; break; }
                std::string v(p, (size_t)(z - p));
                auto it = seen.find(v);
                uint32_t id;
                if (it == seen.end()) {
                    id = (uint32_t)seen.size();
                    it = seen.emplace(std::move(v), id).first;
                    order.push_back(&it->first);
                } else id = it->second;
                put_varint(idx, id);
                total++;
                p = z + 1;
                if (seen.size() > 200000) { ok = false; break; }
            }
            if (!ok || total == 0) continue;
            // only worth it when values genuinely repeat
            if (seen.size() * 4 > total) continue;
            std::string dict;
            for (const std::string* v : order) { dict += *v; dict += '\0'; }
            if (dict.size() + idx.size() >= d.size()) continue;
            b.data[i].clear();
            b.type[i] = ST_DICT;
            b.at(n + "#d") = std::move(dict);
            b.at(n + "#x") = std::move(idx);
        }
    }
}

// Stream 0 of every block is the directory; every other stream is looked up by
// the name recorded there.
constexpr const char* kDirStream = "@dir";

// Serialises the bundle's stream names (and the fixed metadata the decoder
// needs) so the decoder can rebuild the same view of the block.
void writeDirectory(Bundle& b, uint64_t nVariants, uint32_t nCols,
                    uint32_t ploidy, uint8_t lastLineHasNewline,
                    const Ablation& ab) {
    std::string dir;
    put_varint(dir, nVariants);
    put_varint(dir, nCols);
    put_varint(dir, ploidy);
    dir += (char)lastLineHasNewline;
    // Record what the encoder actually did, so an archive written with
    // transforms disabled still decodes with no extra flags.
    dir += (char)((ab.pbwt ? 1 : 0) | (ab.infoSplit ? 2 : 0));
    put_varint(dir, b.size());
    for (size_t i = 0; i < b.names.size(); i++) {
        put_varint(dir, b.names[i].size());
        dir += b.names[i];
        dir += (char)b.type[i];
    }
    // The directory is prepended, so it becomes stream 0 on the wire.
    b.names.insert(b.names.begin(), kDirStream);
    b.type.insert(b.type.begin(), (uint8_t)ST_SERIAL);
    b.data.insert(b.data.begin(), std::move(dir));
}

// ---------------------------------------------------------------- encoder

// Encodes one block of VCF body text into a stream bundle.
// Returns false when the block cannot be modelled (caller stores it verbatim).
bool encodeBlock(const char* text, size_t len, size_t nsamp, uint32_t nCols,
                 Bundle& out, const Ablation& ab = Ablation()) {
    if (len == 0) return false;
    const bool endsNL = text[len - 1] == '\n';

    std::vector<RowRef> rows;
    if (!parseRows(text, len, nCols, nsamp, rows)) return false;
    const size_t nv = rows.size();
    if (nv == 0) return false;

    const bool hasSamples = nCols > 9 && nsamp > 0;

    std::string& sChrom  = out.at("chrom");
    std::string& sPosD   = out.at("pos.delta");
    std::string& sPosF   = out.at("pos.flag");
    std::string& sPosE   = out.at("pos.exc");
    std::string& sId     = out.at("id");
    std::string& sRef    = out.at("ref");
    std::string& sAlt    = out.at("alt");
    std::string& sQual   = out.at("qual");
    std::string& sFilter = out.at("filter");
    std::string& sKeyset = out.at("info.keyset");

    // ---- columns 0..7 -----------------------------------------------------
    Dict keysets;
    std::vector<std::string> keysetKeys;          // parallel to keysets.items
    std::vector<std::vector<std::pair<std::string,bool>>> keysetParsed;
    std::vector<std::pair<Field,bool>> parts;
    uint64_t prevPos = 0;

    for (size_t r = 0; r < nv; r++) {
        putField(sChrom, rows[r].fixed[0]);

        const Field& pf = rows[r].fixed[1];
        uint64_t pv;
        if (parse_canonical_u64(pf.p, pf.n, pv)) {
            sPosF += '\0';
            put_varint(sPosD, zigzag((int64_t)pv - (int64_t)prevPos));
            prevPos = pv;
        } else {
            sPosF += '\1';
            putField(sPosE, pf);
        }

        putField(sId,     rows[r].fixed[2]);
        putField(sRef,    rows[r].fixed[3]);
        putField(sAlt,    rows[r].fixed[4]);
        putField(sQual,   rows[r].fixed[5]);
        putField(sFilter, rows[r].fixed[6]);

        // INFO: one dictionary entry per distinct ordered key pattern, one
        // value stream per key.
        const Field& inf = rows[r].fixed[7];
        if (!ab.infoSplit) {
            putField(out.at("info.raw"), inf);
            put_varint(sKeyset, 0);
            continue;
        }
        std::string sig = keysetSignature(inf, parts);
        uint32_t ks = keysets.intern(sig);
        if (ks == keysetParsed.size()) {
            // first sighting: record the key names in order
            std::vector<std::pair<std::string,bool>> keys;
            size_t i = 0;
            while (i < sig.size()) {
                size_t j = sig.find('\x01', i);
                std::string tok = sig.substr(i, j - i);
                bool hasEq = !tok.empty() && tok.back() == '=';
                if (hasEq) tok.pop_back();
                keys.emplace_back(tok, hasEq);
                i = j + 1;
            }
            keysetParsed.push_back(std::move(keys));
        }
        put_varint(sKeyset, ks);
        const auto& keys = keysetParsed[ks];
        if (keys.size() != parts.size()) return false;
        for (size_t k = 0; k < keys.size(); k++) {
            if (!keys[k].second) continue;
            std::string& vs = out.at("info.v." + keys[k].first);
            vs.append(parts[k].first.p, parts[k].first.n);
            vs += '\0';
        }
    }
    keysets.serialize(out.at("info.dict"));

    if (!hasSamples) {
        // Sites-only: a 9th column (FORMAT with no samples) goes out as text.
        for (uint32_t c = 8; c < nCols; c++) {
            std::string& sc = out.at("col." + std::to_string(c));
            for (size_t r = 0; r < nv; r++) putField(sc, rows[r].fixed[c]);
        }
        splitPipeStreams(out, ab);
        numerifyStreams(out, ab);
        writeDirectory(out, nv, nCols, 0, endsNL ? 1 : 0, ab);
        return true;
    }

    // ---- FORMAT -----------------------------------------------------------
    Dict formats;
    std::vector<uint32_t> fmtOf(nv);
    std::string& sFmt = out.at("fmt.id");
    for (size_t r = 0; r < nv; r++) {
        const Field& f = rows[r].fixed[8];
        uint32_t id = formats.intern(std::string(f.p, f.n));
        fmtOf[r] = id;
        put_varint(sFmt, id);
    }
    formats.serialize(out.at("fmt.dict"));

    // Subfield names per FORMAT id, and which subfield (if any) is GT.
    std::vector<std::vector<std::string>> fmtSubs(formats.items.size());
    std::vector<int> gtIndex(formats.items.size(), -1);
    for (size_t f = 0; f < formats.items.size(); f++) {
        const std::string& s = formats.items[f];
        size_t i = 0;
        while (true) {
            size_t j = s.find(':', i);
            fmtSubs[f].push_back(s.substr(i, j == std::string::npos ? j : j - i));
            if (j == std::string::npos) break;
            i = j + 1;
        }
        for (size_t j = 0; j < fmtSubs[f].size(); j++)
            if (fmtSubs[f][j] == "GT") { gtIndex[f] = (int)j; break; }
        // Only a leading GT participates in the PBWT; anything else stays text.
        if (gtIndex[f] > 0) gtIndex[f] = -1;
    }

    // ---- ploidy: decided by the first GT-bearing sample of the block ------
    uint32_t ploidy = 0;
    for (size_t r = 0; r < nv && ploidy == 0; r++) {
        if (gtIndex[fmtOf[r]] != 0) continue;
        const char* q = rows[r].samples;
        const char* e = q + rows[r].samplesLen;
        const char* tab = (const char*)std::memchr(q, '\t', (size_t)(e - q));
        if (tab) e = tab;
        const char* colon = (const char*)std::memchr(q, ':', (size_t)(e - q));
        if (colon) e = colon;
        uint32_t k = 1;
        for (const char* z = q; z < e; z++) if (*z == '|' || *z == '/') k++;
        if (k >= 1 && k <= 8) ploidy = k;
    }
    if (ploidy == 0) ploidy = 2;
    const size_t NH = nsamp * ploidy;

    // ---- genotype matrix + PBWT ------------------------------------------
    std::string& sRunLen = out.at("gt.runlen");
    std::string& sRunSym = out.at("gt.runsym");
    std::string& sNRuns  = out.at("gt.nruns");
    std::string& sSep    = out.at("gt.sep");
    std::string& sGtExc  = out.at("gt.exc");

    PBWT pbwt;
    pbwt.reset(NH);
    std::vector<uint8_t> row(NH), perm(NH);
    std::vector<uint8_t> alleles(ploidy);
    uint64_t prevExcVar = 0;
    bool anyGT = false;

    // Non-GT subfields, buffered so they can be emitted sample-major.
    // fsBuf[fmtId][subIdx] holds one (offset,length) per (variant,sample).
    std::vector<std::vector<std::vector<std::pair<uint32_t,uint32_t>>>> fsBuf(formats.items.size());
    std::vector<size_t> fsVariants(formats.items.size(), 0);
    for (size_t f = 0; f < formats.items.size(); f++)
        fsBuf[f].resize(fmtSubs[f].size());

    for (size_t r = 0; r < nv; r++) {
        const uint32_t f = fmtOf[r];
        const bool doGT = (gtIndex[f] == 0);
        const size_t nsub = fmtSubs[f].size();
        fsVariants[f]++;

        if (doGT) std::fill(row.begin(), row.end(), (uint8_t)0);
        char domSep = 0;
        uint32_t sepVotes[2] = {0, 0};   // '|' , '/'

        const char* scan = rows[r].samples;
        const char* scanEnd = scan + rows[r].samplesLen;
        for (size_t s = 0; s < nsamp; s++) {
            const char* tab = (const char*)std::memchr(scan, '\t', (size_t)(scanEnd - scan));
            const char* q = scan;
            const char* e = tab ? tab : scanEnd;
            scan = tab ? tab + 1 : scanEnd;
            size_t sub = 0;
            while (sub < nsub) {
                const char* c = (const char*)std::memchr(q, ':', (size_t)(e - q));
                const char* stop = c ? c : e;
                if (sub == 0 && doGT) {
                    char sep = 0;
                    if (parseGT(q, (uint32_t)(stop - q), ploidy, alleles.data(), sep)) {
                        for (uint32_t h = 0; h < ploidy; h++)
                            row[s * ploidy + h] = alleles[h];
                        if (sep == '|') sepVotes[0]++;
                        else if (sep == '/') sepVotes[1]++;
                    } else {
                        // keep the placeholder zeros; record the text verbatim
                        put_varint(sGtExc, r - prevExcVar);
                        prevExcVar = r;
                        put_varint(sGtExc, s);
                        put_varint(sGtExc, (uint64_t)(stop - q));
                        sGtExc.append(q, (size_t)(stop - q));
                    }
                } else {
                    auto& col = fsBuf[f][sub];
                    col.push_back({(uint32_t)(q - text), (uint32_t)(stop - q)});
                }
                if (!c) { sub++; break; }
                q = c + 1;
                sub++;
            }
            // A sample may carry fewer subfields than FORMAT declares.
            for (size_t k = sub; k < nsub; k++)
                if (!(k == 0 && doGT)) fsBuf[f][k].push_back({0u, 0xFFFFFFFFu});
        }

        if (doGT) {
            anyGT = true;
            domSep = sepVotes[1] > sepVotes[0] ? '/' : '|';
            sSep += domSep;
            if (ab.pbwt) pbwt.forward(row.data(), perm.data());
            else std::copy(row.begin(), row.end(), perm.begin());
            size_t i = 0, nruns = 0;
            while (i < NH) {
                size_t j = i + 1;
                while (j < NH && perm[j] == perm[i]) j++;
                sRunSym += (char)perm[i];
                put_varint(sRunLen, j - i);
                i = j;
                nruns++;
            }
            put_varint(sNRuns, nruns);
        }
    }
    (void)anyGT;

    // Emit the non-GT subfields sample-major: all of one sample's values for a
    // subfield land next to each other.
    for (size_t f = 0; f < formats.items.size(); f++) {
        const size_t vf = fsVariants[f];
        if (vf == 0) continue;
        for (size_t j = 0; j < fmtSubs[f].size(); j++) {
            const auto& col = fsBuf[f][j];
            if (col.empty()) continue;
            if (col.size() != vf * nsamp) return false;
            std::string& st = out.at("fs." + std::to_string(f) + "." + std::to_string(j));
            for (size_t s = 0; s < nsamp; s++) {
                for (size_t v = 0; v < vf; v++) {
                    const auto& e = col[v * nsamp + s];
                    if (e.second == 0xFFFFFFFFu) { st += '\x01'; st += '\0'; }
                    else { st.append(text + e.first, e.second); st += '\0'; }
                }
            }
        }
    }

    splitPipeStreams(out, ab);
    numerifyStreams(out, ab);
    writeDirectory(out, nv, nCols, ploidy, endsNL ? 1 : 0, ab);
    return true;
}

}  // namespace

// ================================================================= decoding

namespace {

// Pulls NUL-terminated values out of a stream, in order.
struct TextCursor {
    const char* p = nullptr;
    const char* end = nullptr;
    TextCursor() = default;
    explicit TextCursor(const std::string& s) : p(s.data()), end(s.data() + s.size()) {}
    bool next(const char*& out, size_t& len) {
        if (p >= end) return false;
        const char* z = (const char*)std::memchr(p, '\0', (size_t)(end - p));
        if (!z) return false;
        out = p;
        len = (size_t)(z - p);
        p = z + 1;
        return true;
    }
    void append(std::string& dst) {
        const char* q; size_t l;
        if (!next(q, l)) die("stream underrun");
        dst.append(q, l);
    }
};
struct ByteCursor {
    const char* p = nullptr;
    const char* end = nullptr;
    ByteCursor() = default;
    explicit ByteCursor(const std::string& s) : p(s.data()), end(s.data() + s.size()) {}
    uint64_t varint() { return get_varint(p, end); }
    char byte() { if (p >= end) die("stream underrun"); return *p++; }
    bool done() const { return p >= end; }
};

// Rebuilds the original block text from a decompressed bundle.
std::string decodeBlock(const std::vector<std::string>& streams, size_t nsamp) {
    if (streams.empty()) die("empty block");
    // stream 0 is the directory
    const std::string& dirBuf = streams[0];
    const char* dp = dirBuf.data();
    const char* de = dirBuf.data() + dirBuf.size();
    const uint64_t nv     = get_varint(dp, de);
    const uint32_t nCols  = (uint32_t)get_varint(dp, de);
    const uint32_t ploidy = (uint32_t)get_varint(dp, de);
    if (dp >= de) die("corrupt directory");
    const uint8_t endsNL  = (uint8_t)*dp++;
    if (dp >= de) die("corrupt directory");
    const uint8_t abBits  = (uint8_t)*dp++;
    const bool usedPbwt      = (abBits & 1) != 0;
    const bool usedInfoSplit = (abBits & 2) != 0;
    const uint64_t nStr   = get_varint(dp, de);
    std::unordered_map<std::string, const std::string*> S;
    // TEXTNUM streams are expanded back to NUL-separated decimal text here, so
    // everything downstream sees the same shape the encoder produced.
    std::deque<std::string> rendered;
    std::vector<std::string> joinNames;
    std::vector<uint8_t> joinKind;
    std::vector<size_t> joinSlots;
    for (uint64_t i = 0; i < nStr; i++) {
        uint64_t l = get_varint(dp, de);
        if ((size_t)(de - dp) < l) die("corrupt directory");
        std::string name(dp, l);
        dp += l;
        if (dp >= de) die("corrupt directory");
        const uint8_t st = (uint8_t)*dp++;
        if (i + 1 >= streams.size()) die("directory/stream count mismatch");
        const std::string* src = &streams[i + 1];
        if (st == ST_DECIMAL || st == ST_INTLIST || st == ST_DICT || st == ST_PIPE) {
            joinNames.push_back(name);
            joinKind.push_back(st);
            rendered.emplace_back();          // filled in a second pass below
            src = &rendered.back();
            joinSlots.push_back(rendered.size() - 1);
        } else if (st == ST_NUMDELTA) {
            ByteCursor cd(*src);
            std::string txt;
            uint64_t acc = 0;
            while (!cd.done()) {
                acc = (uint64_t)((int64_t)acc + unzigzag(cd.varint()));
                txt += std::to_string(acc);
                txt += '\0';
            }
            rendered.push_back(std::move(txt));
            src = &rendered.back();
        } else if (st == ST_TEXTNUM4 || st == ST_TEXTNUM8) {
            const size_t w = st == ST_TEXTNUM4 ? 4 : 8;
            if (src->size() % w != 0) die("corrupt numeric stream");
            const size_t n = src->size() / w;
            std::string txt;
            txt.reserve(n * (w == 4 ? 8 : 12));
            for (size_t k = 0; k < n; k++) {
                uint64_t v = 0;
                if (w == 4) { uint32_t t; std::memcpy(&t, src->data() + k * 4, 4); v = t; }
                else        { std::memcpy(&v, src->data() + k * 8, 8); }
                txt += std::to_string(v);
                txt += '\0';
            }
            rendered.push_back(std::move(txt));
            src = &rendered.back();
        }
        S.emplace(std::move(name), src);
    }
    auto get = [&](const char* n) -> const std::string& {
        auto it = S.find(n);
        static const std::string empty;
        return it == S.end() ? empty : *it->second;
    };

    // Re-join the split streams now that every sub-stream is in S.
    // Transforms nest (a '|'-split sub-stream may itself be dictionary-split),
    // and a transform always appends its children after itself in the
    // directory, so joining in reverse directory order guarantees every
    // sub-stream is already materialised when its parent needs it.
    for (size_t kk = joinNames.size(); kk-- > 0; ) {
        const size_t k = kk;
        const std::string& base = joinNames[k];
        auto pick = [&](const char* suf) -> const std::string& {
            auto it = S.find(base + suf);
            if (it == S.end()) die("missing sub-stream " + base + suf);
            return *it->second;
        };
        std::string txt;
        if (joinKind[k] == ST_DECIMAL) {
            const std::string& sg = pick("#s");
            const std::string& fl = pick("#L");
            ByteCursor ci(pick("#i")), cf(pick("#f"));
            txt.reserve(sg.size() * 12);
            for (size_t j = 0; j < sg.size(); j++) {
                if (sg[j]) txt += '-';
                txt += std::to_string(ci.varint());
                txt += '.';
                const size_t L = (uint8_t)fl[j];
                std::string f = std::to_string(cf.varint());
                if (f.size() < L) txt.append(L - f.size(), '0');
                txt += f;
                txt += '\0';
            }
        } else if (joinKind[k] == ST_PIPE) {
            ByteCursor crn(pick("#rn"));
            const std::string& fn = pick("#fn");
            size_t maxF = 0;
            for (char c : fn) maxF = std::max(maxF, (size_t)(uint8_t)c);
            std::vector<TextCursor> pc;
            pc.reserve(maxF);
            for (size_t j = 0; j < maxF; j++)
                pc.emplace_back(pick(("#p" + std::to_string(j)).c_str()));
            size_t recAt = 0;
            while (!crn.done()) {
                const uint64_t recs = crn.varint();
                for (uint64_t rr = 0; rr < recs; rr++) {
                    if (rr) txt += ',';
                    if (recAt >= fn.size()) die("corrupt annotation split");
                    const size_t nF = (uint8_t)fn[recAt++];
                    for (size_t t = 0; t < nF; t++) {
                        if (t) txt += '|';
                        pc[t].append(txt);
                    }
                }
                txt += '\0';
            }
        } else if (joinKind[k] == ST_DICT) {
            const std::string& dict = pick("#d");
            std::vector<std::pair<const char*, size_t>> vals;
            {
                const char* q = dict.data();
                const char* qe = q + dict.size();
                while (q < qe) {
                    const char* z = (const char*)std::memchr(q, '\0', (size_t)(qe - q));
                    if (!z) break;
                    vals.push_back({q, (size_t)(z - q)});
                    q = z + 1;
                }
            }
            ByteCursor cx(pick("#x"));
            while (!cx.done()) {
                uint64_t id = cx.varint();
                if (id >= vals.size()) die("corrupt dictionary index");
                txt.append(vals[id].first, vals[id].second);
                txt += '\0';
            }
        } else {
            const std::string& cn = pick("#n");
            size_t maxE = 0;
            for (char c : cn) maxE = std::max(maxE, (size_t)(uint8_t)c);
            std::vector<ByteCursor> cv;
            cv.reserve(maxE);
            for (size_t j = 0; j < maxE; j++)
                cv.emplace_back(pick(("#v" + std::to_string(j)).c_str()));
            txt.reserve(cn.size() * 8);
            for (size_t j = 0; j < cn.size(); j++) {
                const size_t nE = (uint8_t)cn[j];
                for (size_t t = 0; t < nE; t++) {
                    if (t) txt += ',';
                    txt += std::to_string(cv[t].varint());
                }
                txt += '\0';
            }
        }
        rendered[joinSlots[k]] = std::move(txt);
    }

    TextCursor cChrom(get("chrom")), cId(get("id")), cRef(get("ref")),
               cAlt(get("alt")), cQual(get("qual")), cFilter(get("filter")),
               cPosE(get("pos.exc"));
    ByteCursor cPosD(get("pos.delta")), cPosF(get("pos.flag")),
               cKeyset(get("info.keyset"));
    TextCursor cInfoRaw(get("info.raw"));

    // INFO dictionary
    const std::string& kdictBuf = get("info.dict");
    const char* kp = kdictBuf.data();
    const char* ke = kdictBuf.data() + kdictBuf.size();
    std::vector<std::string> keysetSigs = kdictBuf.empty()
            ? std::vector<std::string>() : Dict::deserialize(kp, ke);
    std::vector<std::vector<std::pair<std::string,bool>>> keysetParsed;
    keysetParsed.reserve(keysetSigs.size());
    for (const auto& sig : keysetSigs) {
        std::vector<std::pair<std::string,bool>> keys;
        size_t i = 0;
        while (i < sig.size()) {
            size_t j = sig.find('\x01', i);
            std::string tok = sig.substr(i, j - i);
            bool hasEq = !tok.empty() && tok.back() == '=';
            if (hasEq) tok.pop_back();
            keys.emplace_back(tok, hasEq);
            i = j + 1;
        }
        keysetParsed.push_back(std::move(keys));
    }
    std::unordered_map<std::string, TextCursor> infoCur;
    for (const auto& kv : S)
        if (kv.first.rfind("info.v.", 0) == 0)
            infoCur.emplace(kv.first.substr(7), TextCursor(*kv.second));

    const bool hasSamples = nCols > 9 && nsamp > 0;

    // FORMAT dictionary
    std::vector<std::string> formats;
    std::vector<std::vector<std::string>> fmtSubs;
    std::vector<int> gtIndex;
    ByteCursor cFmt(get("fmt.id"));
    if (hasSamples) {
        const std::string& fdb = get("fmt.dict");
        const char* fp = fdb.data();
        const char* fe = fdb.data() + fdb.size();
        formats = fdb.empty() ? std::vector<std::string>() : Dict::deserialize(fp, fe);
        fmtSubs.resize(formats.size());
        gtIndex.assign(formats.size(), -1);
        for (size_t f = 0; f < formats.size(); f++) {
            const std::string& s = formats[f];
            size_t i = 0;
            while (true) {
                size_t j = s.find(':', i);
                fmtSubs[f].push_back(s.substr(i, j == std::string::npos ? j : j - i));
                if (j == std::string::npos) break;
                i = j + 1;
            }
            if (!fmtSubs[f].empty() && fmtSubs[f][0] == "GT") gtIndex[f] = 0;
        }
    }

    // First pass: which FORMAT does each variant use, and how many variants use
    // each FORMAT (needed to slice the sample-major subfield streams).
    std::vector<uint32_t> fmtOf(nv, 0);
    std::vector<size_t> fsVariants(formats.size(), 0);
    if (hasSamples) {
        for (uint64_t r = 0; r < nv; r++) {
            uint32_t f = (uint32_t)cFmt.varint();
            if (f >= formats.size()) die("corrupt FORMAT id");
            fmtOf[r] = f;
            fsVariants[f]++;
        }
    }

    // Index the sample-major subfield streams so a (format, subfield, variant,
    // sample) lookup is O(1).
    struct SubStream {
        std::vector<const char*> starts;   // nsamp * vf entries, sample-major
        std::vector<uint32_t> lens;
        bool present = false;
    };
    std::vector<std::vector<SubStream>> fsIdx(formats.size());
    for (size_t f = 0; f < formats.size(); f++) {
        fsIdx[f].resize(fmtSubs[f].size());
        const size_t vf = fsVariants[f];
        if (vf == 0) continue;
        for (size_t j = 0; j < fmtSubs[f].size(); j++) {
            auto it = S.find("fs." + std::to_string(f) + "." + std::to_string(j));
            if (it == S.end()) continue;
            SubStream& ss = fsIdx[f][j];
            ss.present = true;
            ss.starts.reserve(vf * nsamp);
            ss.lens.reserve(vf * nsamp);
            const char* q = it->second->data();
            const char* qe = q + it->second->size();
            while (q < qe) {
                const char* z = (const char*)std::memchr(q, '\0', (size_t)(qe - q));
                if (!z) break;
                ss.starts.push_back(q);
                ss.lens.push_back((uint32_t)(z - q));
                q = z + 1;
            }
            if (ss.starts.size() != vf * nsamp) die("corrupt subfield stream");
        }
    }

    // Genotype streams
    ByteCursor cRunLen(get("gt.runlen")), cNRuns(get("gt.nruns")), cSep(get("gt.sep"));
    const std::string& runSymBuf = get("gt.runsym");
    size_t runSymPos = 0;
    ByteCursor cGtExc(get("gt.exc"));
    const size_t NH = hasSamples ? nsamp * ploidy : 0;
    PBWT pbwt;
    if (NH) pbwt.reset(NH);
    std::vector<uint8_t> row(NH), perm(NH);

    // Pending GT exceptions, read lazily in (variant, sample) order.
    uint64_t excVar = 0; bool excValid = false; uint64_t excSamp = 0;
    std::string excText;
    auto loadExc = [&]() {
        if (cGtExc.done()) { excValid = false; return; }
        excVar += cGtExc.varint();
        excSamp = cGtExc.varint();
        uint64_t l = cGtExc.varint();
        if ((size_t)(cGtExc.end - cGtExc.p) < l) die("corrupt gt.exc");
        excText.assign(cGtExc.p, l);
        cGtExc.p += l;
        excValid = true;
    };
    loadExc();

    std::vector<size_t> fsSeen(formats.size(), 0);   // variants of each fmt so far
    std::string out;
    out.reserve((size_t)nv * (nCols * 4 + nsamp * 4));
    char sepbuf[2] = {0, 0};
    uint64_t prevPos = 0;

    // Sites-only trailing columns (only used when there is no FORMAT block).
    std::vector<TextCursor> colCur;
    if (!hasSamples) {
        for (uint32_t c = 8; c < nCols; c++) {
            auto it = S.find("col." + std::to_string(c));
            if (it == S.end()) die("missing column stream");
            colCur.emplace_back(*it->second);
        }
    }

    for (uint64_t r = 0; r < nv; r++) {
        cChrom.append(out); out += '\t';
        if (cPosF.byte() == 0) {
            int64_t d = unzigzag(cPosD.varint());
            prevPos = (uint64_t)((int64_t)prevPos + d);
            out += std::to_string(prevPos);
        } else {
            cPosE.append(out);
        }
        out += '\t';
        cId.append(out);     out += '\t';
        cRef.append(out);    out += '\t';
        cAlt.append(out);    out += '\t';
        cQual.append(out);   out += '\t';
        cFilter.append(out); out += '\t';

        if (!usedInfoSplit) {
            (void)cKeyset.varint();
            cInfoRaw.append(out);
        } else {
            uint64_t ks = cKeyset.varint();
            if (ks >= keysetParsed.size()) die("corrupt INFO keyset id");
            const auto& keys = keysetParsed[ks];
            for (size_t k = 0; k < keys.size(); k++) {
                if (k) out += ';';
                out += keys[k].first;
                if (keys[k].second) {
                    out += '=';
                    auto it = infoCur.find(keys[k].first);
                    if (it == infoCur.end()) die("missing INFO value stream");
                    it->second.append(out);
                }
            }
        }

        if (!hasSamples) {
            for (uint32_t c = 8; c < nCols; c++) {
                out += '\t';
                colCur[c - 8].append(out);
            }
            out += '\n';
            continue;
        }

        const uint32_t f = fmtOf[r];
        out += '\t';
        out += formats[f];
        const size_t nsub = fmtSubs[f].size();
        const bool doGT = (gtIndex[f] == 0);
        const size_t vIdx = fsSeen[f]++;

        if (doGT) {
            // rebuild this variant's PBWT-permuted row from its runs
            uint64_t nruns = cNRuns.varint();
            size_t at = 0;
            for (uint64_t k = 0; k < nruns; k++) {
                if (runSymPos >= runSymBuf.size()) die("corrupt gt.runsym");
                uint8_t sym = (uint8_t)runSymBuf[runSymPos++];
                uint64_t L = cRunLen.varint();
                if (at + L > NH) die("corrupt gt.runlen");
                std::memset(perm.data() + at, sym, L);
                at += L;
            }
            if (at != NH) die("run lengths do not cover the row");
            if (usedPbwt) pbwt.inverse(perm.data(), row.data());
            else std::copy(perm.begin(), perm.end(), row.begin());
            sepbuf[0] = cSep.byte();
        }

        for (size_t s = 0; s < nsamp; s++) {
            out += '\t';
            for (size_t j = 0; j < nsub; j++) {
                if (j == 0 && doGT) {
                    if (excValid && excVar == r && excSamp == s) {
                        out += excText;
                        loadExc();
                    } else {
                        for (uint32_t h = 0; h < ploidy; h++) {
                            if (h) out += sepbuf[0];
                            uint8_t a = row[s * ploidy + h];
                            if (a == kMissingAllele) out += '.';
                            else out += std::to_string((unsigned)a);
                        }
                    }
                    continue;
                }
                const SubStream& ss = fsIdx[f][j];
                if (!ss.present) die("missing subfield stream");
                const size_t at = s * fsVariants[f] + vIdx;
                if (ss.lens[at] == 1 && ss.starts[at][0] == '\x01') break;  // absent tail
                if (j) out += ':';
                else if (!doGT) {}
                out.append(ss.starts[at], ss.lens[at]);
            }
        }
        out += '\n';
    }
    if (!endsNL && !out.empty() && out.back() == '\n') out.pop_back();
    return out;
}

}  // namespace

// ============================================================ file container

namespace {

// A VCF source that transparently handles gzip/bgzip, detected by magic bytes
// rather than by file name.
class Reader {
   public:
    explicit Reader(const std::string& path) {
        FILE* probe = std::fopen(path.c_str(), "rb");
        if (!probe) die("cannot open " + path);
        unsigned char m[2] = {0, 0};
        size_t got = std::fread(m, 1, 2, probe);
        std::fclose(probe);
        gz_ = (got == 2 && m[0] == 0x1f && m[1] == 0x8b);
        if (gz_) {
            gzf_ = gzopen(path.c_str(), "rb");
            if (!gzf_) die("cannot open " + path);
            gzbuffer(gzf_, 1 << 20);
        } else {
            f_ = std::fopen(path.c_str(), "rb");
            if (!f_) die("cannot open " + path);
        }
    }
    ~Reader() { if (gzf_) gzclose(gzf_); if (f_) std::fclose(f_); }
    Reader(const Reader&) = delete;
    Reader& operator=(const Reader&) = delete;

    size_t read(char* dst, size_t n) {
        if (gz_) {
            int r = gzread(gzf_, dst, (unsigned)n);
            if (r < 0) die("gzip read error");
            return (size_t)r;
        }
        return std::fread(dst, 1, n, f_);
    }
   private:
    bool gz_ = false;
    gzFile gzf_ = nullptr;
    FILE* f_ = nullptr;
};

struct BlockRec {
    uint64_t compLen = 0;
    uint64_t rawLen  = 0;
    uint32_t nStreams = 0;
    uint8_t  kind = 0;      // 0 = modelled bundle, 1 = verbatim
};

void writeAll(FILE* f, const void* p, size_t n) {
    if (n && std::fwrite(p, 1, n, f) != n) die("write failed");
}

struct Options {
    std::string model;
    std::string dumpDir;
    int threads = 0;
    size_t blockBytes = 64u << 20;
    size_t maxMemMB = 0;         // 0 = use blockBytes as given
    bool selfcheck = true;
    bool dual = true;
    bool rawBlocks = false;   // ablation floor: no format awareness at all
    size_t dualMaxBytes = 96u << 20;
    Ablation ab;
    bool verify = false;
    bool quiet = false;
    int level = 0;    // 0 = calibrate (or OpenZL's default with --no-calibrate)
    bool calibrate = true;
};

// Candidate entropy levels, priced per file. OpenZL's default is 6 and nothing
// in this tool set it until 2026-09-11. Measured with full round trips
// (results/vcf_level_probe.csv), level 16 over the default is worth +9.4% on
// CIViC, +5.4% on a gVCF and +4.6% on HG002; level 19 adds at most 2% more for
// 1.4-3x the time. The same sweep on BED showed the curve is not monotone --
// a higher level made some files larger -- so the level is measured on each
// file rather than fixed, by the same rule the FASTQ and BED codecs use.
constexpr int    kCalibLevels[] = { 0 /* OpenZL default */, 12, 16 };
constexpr double kLevelSlack    = 0.02;     // prefer the cheaper level within 2%
constexpr size_t kCalibBytes    = 8u << 20; // a prefix of the first block

int calibrateLevel(const std::string& block, size_t nsamp, uint32_t nCols,
                   const Options& o) {
    size_t n = std::min(block.size(), kCalibBytes);
    if (n < block.size()) {
        const size_t nl = block.rfind('\n', n);
        if (nl == std::string::npos) return 0;
        n = nl + 1;
    }
    if (n < (1u << 16)) return 0;                 // too small to rank anything
    Bundle b;
    if (!encodeBlock(block.data(), n, nsamp, nCols, b, o.ab)) return 0;

    const size_t L = sizeof kCalibLevels / sizeof *kCalibLevels;
    std::vector<size_t> sz(L, SIZE_MAX);
    std::vector<std::thread> ts;
    for (size_t li = 0; li < L; li++)
        ts.emplace_back([&, li] {
            Codec c(o.model, kCalibLevels[li]);
            sz[li] = c.compressBundle(b).size();
        });
    for (auto& t : ts) t.join();

    size_t best = *std::min_element(sz.begin(), sz.end());
    size_t pick = 0;
    for (size_t li = 0; li < L; li++)
        if ((double)sz[li] <= (double)best * (1.0 + kLevelSlack)) { pick = li; break; }
    if (!o.quiet) {
        std::fprintf(stderr, "nyx_vcf: level calibration on %.1f MB:", n / 1e6);
        for (size_t li = 0; li < L; li++)
            std::fprintf(stderr, " %s=%.0fKB",
                         kCalibLevels[li] ? std::to_string(kCalibLevels[li]).c_str() : "def",
                         sz[li] / 1e3);
        std::fprintf(stderr, "  -> %s\n",
                     kCalibLevels[pick] ? std::to_string(kCalibLevels[pick]).c_str() : "default");
    }
    return kCalibLevels[pick];
}

// ------------------------------------------------------------- compression

void cmdCompress(const std::string& in, const std::string& out, Options& o) {
    Reader rd(in);

    // --- read the ## header and the #CHROM line -------------------------
    std::string buf;
    buf.resize(1 << 22);
    std::string carry;
    std::string header;
    size_t nsamp = 0;
    uint32_t nCols = 0;
    bool haveHeader = false;
    size_t originalSize = 0;

    auto findHeaderEnd = [&](const std::string& s, size_t& consumed) -> bool {
        size_t p = 0;
        // Files exported by Windows tools often start with a UTF-8 BOM. Skip it
        // when looking for the header; it stays in the header bytes verbatim.
        if (s.size() >= 3 && (unsigned char)s[0] == 0xEF
                          && (unsigned char)s[1] == 0xBB
                          && (unsigned char)s[2] == 0xBF) p = 3;
        while (p < s.size()) {
            size_t nl = s.find('\n', p);
            if (nl == std::string::npos) break;
            if (s[p] != '#') { consumed = p; return true; }
            if (s.compare(p, 7, "#CHROM\t") == 0) {
                size_t tabs = 0;
                for (size_t i = p; i < nl; i++) if (s[i] == '\t') tabs++;
                nCols = (uint32_t)(tabs + 1);
                nsamp = nCols > 9 ? nCols - 9 : 0;
            }
            p = nl + 1;
        }
        consumed = p;
        return false;
    };

    while (!haveHeader) {
        size_t got = rd.read(&buf[0], buf.size());
        if (got == 0) break;
        originalSize += got;
        carry.append(buf.data(), got);
        size_t consumed = 0;
        if (findHeaderEnd(carry, consumed)) {
            header.assign(carry, 0, consumed);
            carry.erase(0, consumed);
            haveHeader = true;
        } else if (consumed > 0 && carry.size() > (64u << 20)) {
            header.append(carry, 0, consumed);
            carry.erase(0, consumed);
        }
    }
    if (!haveHeader) {           // header-only file (no data rows)
        header += carry;
        carry.clear();
    }
    // A compressor should never refuse a file. Anything we cannot parse as VCF
    // -- no #CHROM line, an empty file, something that is not a VCF at all --
    // is stored block-by-block through the verbatim path, which is still exact
    // and still compressed, just without the format-aware transforms.
    const bool rawMode = (nCols == 0);
    if (rawMode) {
        // `header` holds precisely the bytes already taken off the stream, so
        // putting it back in front of `carry` reconstitutes the whole input.
        carry = header + carry;
        header.clear();
        nsamp = 0;
    }

    const int threads = o.threads > 0 ? o.threads
                                      : (int)std::max(1u, std::thread::hardware_concurrency());

    // Peak memory is (threads x block bytes x per-block overhead). The overhead
    // depends on the file's shape -- every INFO key becomes its own stream, so a
    // wide-INFO sites file costs far more per block than a genotype panel --
    // and measures between 3x and 8x. Size the block from the budget using the
    // conservative end so the budget holds for the worst shape we have seen.
    if (o.maxMemMB) {
        constexpr size_t kOverhead = 8;
        size_t b = (o.maxMemMB * 1000000ull) / ((size_t)threads * kOverhead);
        b = std::max<size_t>(b, 4u << 20);        // below this the ratio suffers badly
        b = std::min<size_t>(b, 512u << 20);
        o.blockBytes = b;
        if (!o.quiet)
            std::fprintf(stderr, "nyx_vcf: memory budget %zu MB over %d threads -> %.0f MB blocks\n",
                         o.maxMemMB, threads, b / 1e6);
    }

    FILE* fo = std::fopen(out.c_str(), "wb");
    if (!fo) die("cannot create " + out);

    Codec headerCodec(o.model, o.level);
    std::string hdrComp;
    {
        Bundle hb;
        hb.at("header") = header;      // one stream, class "text"
        hdrComp = headerCodec.compressBundle(hb);
    }

    std::string fileHdr;
    fileHdr.append(kMagic, 8);
    put_u32(fileHdr, 0);                       // flags
    put_u64(fileHdr, (uint64_t)nsamp);
    put_u32(fileHdr, nCols);
    put_u64(fileHdr, header.size());
    put_u64(fileHdr, hdrComp.size());
    writeAll(fo, fileHdr.data(), fileHdr.size());
    writeAll(fo, hdrComp.data(), hdrComp.size());

    // --- body: cut into blocks on line boundaries, compress in waves ----

    std::vector<BlockRec> index;
    std::vector<std::string> raws(threads), comps(threads);
    bool eof = false;

    auto fillBlock = [&](std::string& dst) -> bool {
        dst.clear();
        dst.swap(carry);
        while (dst.size() < o.blockBytes && !eof) {
            size_t got = rd.read(&buf[0], buf.size());
            if (got == 0) { eof = true; break; }
            originalSize += got;
            dst.append(buf.data(), got);
        }
        if (dst.empty()) return false;
        // give back everything after the last newline
        size_t nl = dst.rfind('\n');
        while (nl == std::string::npos && !eof) {
            // One record longer than the block budget: grow the block until the
            // line ends rather than refusing the file.
            size_t got = rd.read(&buf[0], buf.size());
            if (got == 0) { eof = true; break; }
            originalSize += got;
            dst.append(buf.data(), got);
            nl = dst.rfind('\n');
        }
        if (nl == std::string::npos) {
            /* final line has no terminator: the whole tail is one block */
        } else if (nl + 1 < dst.size()) {
            carry.assign(dst, nl + 1, dst.size() - nl - 1);
            dst.resize(nl + 1);
        }
        return true;
    };

    // Calibrate the entropy level on the first block before any worker exists,
    // then hold it for the whole file. Encoder-side only: OpenZL frames are
    // self-describing, so archives written at any level decode unchanged.
    bool havePending = fillBlock(raws[0]);
    if (havePending && o.calibrate && o.level == 0 && !rawMode && !o.rawBlocks)
        o.level = calibrateLevel(raws[0], nsamp, nCols, o);

    std::vector<Codec*> codecs;
    for (int i = 0; i < threads; i++) codecs.push_back(new Codec(o.model, o.level));

    while (true) {
        int n = 0;
        if (havePending) { n = 1; havePending = false; }
        for (; n < threads; n++) if (!fillBlock(raws[n])) break;
        if (n == 0) break;

        std::vector<std::thread> pool;
        std::vector<BlockRec> recs(n);
        for (int i = 0; i < n; i++) {
            pool.emplace_back([&, i] {
                Bundle b;
                bool ok = !o.rawBlocks && !rawMode
                       && encodeBlock(raws[i].data(), raws[i].size(), nsamp, nCols, b, o.ab);
                if (ok && o.selfcheck) {
                    std::vector<std::string> streams;
                    streams.reserve(b.size());
                    for (const auto& d : b.data) streams.push_back(d);
                    std::string back = decodeBlock(streams, nsamp);
                    if (back != raws[i]) ok = false;
                }
                auto verbatim = [&] {
                    Bundle vb;
                    vb.at("@raw") = raws[i];
                    return codecs[i]->compressBundle(vb);
                };
                if (!ok) {
                    comps[i] = verbatim();
                    recs[i] = {comps[i].size(), raws[i].size(), 1, 1};
                } else {
                    comps[i] = codecs[i]->compressBundle(b);
                    recs[i] = {comps[i].size(), raws[i].size(), (uint32_t)b.size(), 0};
                    // Splitting a block into streams severs the cross-field
                    // matches a large-window LZ would have found. On small,
                    // annotation-heavy files that loses, so compress the block
                    // whole as well and keep whichever is smaller.
                    if (o.dual && raws[i].size() <= o.dualMaxBytes) {
                        std::string alt = verbatim();
                        if (alt.size() < comps[i].size()) {
                            comps[i] = std::move(alt);
                            recs[i] = {comps[i].size(), raws[i].size(), 1, 1};
                        }
                    }
                }
            });
        }
        for (auto& t : pool) t.join();
        for (int i = 0; i < n; i++) {
            writeAll(fo, comps[i].data(), comps[i].size());
            index.push_back(recs[i]);
            comps[i].clear();
            comps[i].shrink_to_fit();
        }
        if (n < threads) break;
    }

    // --- index + trailer -------------------------------------------------
    long idxOff = std::ftell(fo);
    std::string idx;
    put_varint(idx, index.size());
    for (const auto& b : index) {
        put_varint(idx, b.compLen);
        put_varint(idx, b.rawLen);
        put_varint(idx, b.nStreams);
        idx += (char)b.kind;
    }
    writeAll(fo, idx.data(), idx.size());
    std::string trailer;
    put_u64(trailer, (uint64_t)idxOff);
    put_u64(trailer, originalSize);
    writeAll(fo, trailer.data(), trailer.size());
    std::fclose(fo);
    for (auto* c : codecs) delete c;

    size_t fallbacks = 0;
    for (const auto& b : index) if (b.kind == 1) fallbacks++;
    if (!o.quiet) {
        struct stat st{};
        stat(out.c_str(), &st);
        std::fprintf(stderr,
                "nyx_vcf: %.1f MB -> %.1f MB  (%.2fx)  blocks=%zu verbatim=%zu\n",
                originalSize / 1e6, (double)st.st_size / 1e6,
                st.st_size ? (double)originalSize / (double)st.st_size : 0.0,
                index.size(), fallbacks);
    }
}

// ----------------------------------------------------------- decompression

void cmdDecompress(const std::string& in, const std::string& out, Options& o) {
    FILE* fi = std::fopen(in.c_str(), "rb");
    if (!fi) die("cannot open " + in);
    struct stat st{};
    if (fstat(fileno(fi), &st) != 0) die("stat failed");
    const uint64_t fileSize = (uint64_t)st.st_size;
    if (fileSize < 40) die("not a nyx_vcf archive: " + in);

    char magic[8];
    if (std::fread(magic, 1, 8, fi) != 8 || std::memcmp(magic, kMagic, 8) != 0)
        die("not a nyx_vcf archive: " + in);
    uint32_t flags; uint64_t nsamp64; uint32_t nCols; uint64_t hdrLen, hdrComp;
    if (std::fread(&flags, 4, 1, fi) != 1) die("truncated archive");
    if (std::fread(&nsamp64, 8, 1, fi) != 1) die("truncated archive");
    if (std::fread(&nCols, 4, 1, fi) != 1) die("truncated archive");
    if (std::fread(&hdrLen, 8, 1, fi) != 1) die("truncated archive");
    if (std::fread(&hdrComp, 8, 1, fi) != 1) die("truncated archive");
    const size_t nsamp = (size_t)nsamp64;

    std::string hbuf(hdrComp, '\0');
    if (std::fread(&hbuf[0], 1, hdrComp, fi) != hdrComp) die("truncated archive");
    const long bodyStart = std::ftell(fi);

    // trailer
    if (fseek(fi, -16, SEEK_END) != 0) die("truncated archive");
    uint64_t idxOff, originalSize;
    if (std::fread(&idxOff, 8, 1, fi) != 1) die("truncated archive");
    if (std::fread(&originalSize, 8, 1, fi) != 1) die("truncated archive");
    if (idxOff >= fileSize) die("corrupt archive index");
    if (fseek(fi, (long)idxOff, SEEK_SET) != 0) die("corrupt archive index");
    std::string idxBuf(fileSize - idxOff - 16, '\0');
    if (!idxBuf.empty() && std::fread(&idxBuf[0], 1, idxBuf.size(), fi) != idxBuf.size())
        die("truncated archive");

    const char* ip = idxBuf.data();
    const char* ie = idxBuf.data() + idxBuf.size();
    uint64_t nBlocks = get_varint(ip, ie);
    std::vector<BlockRec> index(nBlocks);
    for (uint64_t i = 0; i < nBlocks; i++) {
        index[i].compLen  = get_varint(ip, ie);
        index[i].rawLen   = get_varint(ip, ie);
        index[i].nStreams = (uint32_t)get_varint(ip, ie);
        if (ip >= ie) die("corrupt archive index");
        index[i].kind = (uint8_t)*ip++;
    }

    const int threads = o.threads > 0 ? o.threads
                                      : (int)std::max(1u, std::thread::hardware_concurrency());
    std::vector<Codec*> codecs;
    for (int i = 0; i < threads; i++) codecs.push_back(new Codec(o.model, o.level));

    FILE* fo = std::fopen(out.c_str(), "wb");
    if (!fo) die("cannot create " + out);
    {
        auto hs = codecs[0]->decompressBundle(hbuf.data(), hbuf.size(), 1);
        writeAll(fo, hs[0].data(), hs[0].size());
    }

    if (fseek(fi, bodyStart, SEEK_SET) != 0) die("seek failed");
    std::vector<std::string> comps(threads), outs(threads);
    size_t at = 0;
    while (at < index.size()) {
        int n = 0;
        for (; n < threads && at + n < index.size(); n++) {
            comps[n].resize(index[at + n].compLen);
            if (std::fread(&comps[n][0], 1, comps[n].size(), fi) != comps[n].size())
                die("truncated archive body");
        }
        std::vector<std::thread> pool;
        for (int i = 0; i < n; i++) {
            pool.emplace_back([&, i] {
                const BlockRec& br = index[at + i];
                auto streams = codecs[i]->decompressBundle(
                        comps[i].data(), comps[i].size(), br.nStreams);
                outs[i] = br.kind == 1 ? std::move(streams[0])
                                       : decodeBlock(streams, nsamp);
            });
        }
        for (auto& t : pool) t.join();
        for (int i = 0; i < n; i++) {
            writeAll(fo, outs[i].data(), outs[i].size());
            outs[i].clear();
            outs[i].shrink_to_fit();
        }
        at += n;
    }
    std::fclose(fo);
    std::fclose(fi);
    for (auto* c : codecs) delete c;
    if (!o.quiet) std::fprintf(stderr, "nyx_vcf: wrote %s\n", out.c_str());
}

// Reports per-stream raw and standalone-compressed sizes over the whole file.
// Streams are compressed individually here, so the total is an upper bound on
// what `compress` writes (which shares one frame per block) -- the point is the
// breakdown, not the total.
void cmdStats(const std::string& in, Options& o) {
    Reader rd(in);
    std::string buf(1 << 22, '\0'), carry, header;
    size_t nsamp = 0;
    uint32_t nCols = 0;
    bool haveHeader = false;
    auto findHeaderEnd = [&](const std::string& str, size_t& consumed) -> bool {
        size_t p = 0;
        while (p < str.size()) {
            size_t nl = str.find('\n', p);
            if (nl == std::string::npos) break;
            if (str[p] != '#') { consumed = p; return true; }
            if (str.compare(p, 7, "#CHROM\t") == 0) {
                size_t tabs = 0;
                for (size_t i = p; i < nl; i++) if (str[i] == '\t') tabs++;
                nCols = (uint32_t)(tabs + 1);
                nsamp = nCols > 9 ? nCols - 9 : 0;
            }
            p = nl + 1;
        }
        consumed = p;
        return false;
    };
    while (!haveHeader) {
        size_t got = rd.read(&buf[0], buf.size());
        if (got == 0) break;
        carry.append(buf.data(), got);
        size_t consumed = 0;
        if (findHeaderEnd(carry, consumed)) {
            header.assign(carry, 0, consumed);
            carry.erase(0, consumed);
            haveHeader = true;
        }
    }
    // `stats` only reports; it has nothing useful to say about a non-VCF.
    if (nCols == 0) die("no #CHROM line found in " + in);

    Codec codec(o.model);
    std::map<std::string, std::pair<uint64_t,uint64_t>> acc;   // name -> (raw, comp)
    uint64_t bodyBytes = 0;
    bool eof = false;
    size_t blockNo = 0;
    std::string blk;
    // Sanitises a stream name into a directory name usable on disk.
    auto slug = [](const std::string& n) {
        std::string o2;
        for (char c : n) o2 += (std::isalnum((unsigned char)c) || c == '.' || c == '_') ? c : '_';
        return o2;
    };
    while (true) {
        blk.clear();
        blk.swap(carry);
        while (blk.size() < o.blockBytes && !eof) {
            size_t got = rd.read(&buf[0], buf.size());
            if (got == 0) { eof = true; break; }
            blk.append(buf.data(), got);
        }
        if (blk.empty()) break;
        size_t nl = blk.rfind('\n');
        if (nl != std::string::npos && nl + 1 < blk.size()) {
            carry.assign(blk, nl + 1, blk.size() - nl - 1);
            blk.resize(nl + 1);
        }
        bodyBytes += blk.size();
        Bundle b;
        if (!encodeBlock(blk.data(), blk.size(), nsamp, nCols, b, o.ab)) {
            auto& e = acc["@verbatim"];
            e.first += blk.size();
            e.second += codec.compressOne(blk, "@raw", ST_SERIAL);
        } else {
            for (size_t i = 0; i < b.size(); i++) {
                auto& e = acc[b.names[i]];
                e.first += b.data[i].size();
                e.second += codec.compressOne(b.data[i], b.names[i], b.type[i]);
                if (!o.dumpDir.empty() && !b.data[i].empty()) {
                    const std::string dir = o.dumpDir + "/"
                                          + slug(streamClass(b.names[i], b.type[i]));
                    ::mkdir(o.dumpDir.c_str(), 0755);
                    ::mkdir(dir.c_str(), 0755);
                    const std::string fp = dir + "/blk" + std::to_string(blockNo)
                                         + "_" + slug(b.names[i]) + ".bin";
                    FILE* df = std::fopen(fp.c_str(), "wb");
                    if (df) { std::fwrite(b.data[i].data(), 1, b.data[i].size(), df); std::fclose(df); }
                }
            }
        }
        blockNo++;
        if (eof && carry.empty()) break;
    }
    std::vector<std::pair<std::string, std::pair<uint64_t,uint64_t>>> v(acc.begin(), acc.end());
    std::sort(v.begin(), v.end(), [](const auto& a, const auto& c) {
        return a.second.second > c.second.second; });
    uint64_t totRaw = 0, totComp = 0;
    for (const auto& e : v) { totRaw += e.second.first; totComp += e.second.second; }
    std::printf("body bytes: %llu\n", (unsigned long long)bodyBytes);
    std::printf("%-28s %14s %14s %8s %7s\n", "STREAM", "RAW", "COMPRESSED", "RATIO", "%TOT");
    for (const auto& e : v) {
        if (e.second.second == 0) continue;
        std::printf("%-28s %14llu %14llu %8.1f %6.2f%%\n", e.first.c_str(),
                    (unsigned long long)e.second.first,
                    (unsigned long long)e.second.second,
                    e.second.first ? (double)e.second.first / (double)e.second.second : 0.0,
                    100.0 * (double)e.second.second / (double)totComp);
    }
    std::printf("%-28s %14llu %14llu %8.1f\n", "TOTAL",
                (unsigned long long)totRaw, (unsigned long long)totComp,
                totComp ? (double)bodyBytes / (double)totComp : 0.0);
}

void cmdInspect(const std::string& in) {
    FILE* fi = std::fopen(in.c_str(), "rb");
    if (!fi) die("cannot open " + in);
    char magic[8];
    if (std::fread(magic, 1, 8, fi) != 8 || std::memcmp(magic, kMagic, 8) != 0)
        die("not a nyx_vcf archive: " + in);
    uint32_t flags; uint64_t nsamp; uint32_t nCols; uint64_t hdrLen, hdrComp;
    if (std::fread(&flags, 4, 1, fi) != 1 || std::fread(&nsamp, 8, 1, fi) != 1 ||
        std::fread(&nCols, 4, 1, fi) != 1 || std::fread(&hdrLen, 8, 1, fi) != 1 ||
        std::fread(&hdrComp, 8, 1, fi) != 1) die("truncated archive");
    struct stat st{};
    fstat(fileno(fi), &st);
    fseek(fi, -16, SEEK_END);
    uint64_t idxOff, originalSize;
    if (std::fread(&idxOff, 8, 1, fi) != 1 || std::fread(&originalSize, 8, 1, fi) != 1)
        die("truncated archive");
    std::fclose(fi);
    std::printf("archive:   %s (%lld bytes)\n", in.c_str(), (long long)st.st_size);
    std::printf("original:  %llu bytes\n", (unsigned long long)originalSize);
    std::printf("ratio:     %.2fx\n", st.st_size ? (double)originalSize / (double)st.st_size : 0.0);
    std::printf("samples:   %llu\n", (unsigned long long)nsamp);
    std::printf("columns:   %u\n", nCols);
    std::printf("header:    %llu bytes -> %llu\n",
                (unsigned long long)hdrLen, (unsigned long long)hdrComp);
}

const char* kUsage =
    "nyx_vcf — byte-exact, format-aware VCF compression on OpenZL\n"
    "\n"
    "  nyx_vcf compress   [opts] <in.vcf[.gz]> <out.nvcf>\n"
    "  nyx_vcf decompress [opts] <in.nvcf> <out.vcf>\n"
    "  nyx_vcf inspect    <in.nvcf>\n"
    "  nyx_vcf stats      <in.vcf[.gz]>      per-stream size breakdown\n"
    "\n"
    "Options:\n"
    "  --threads N       worker threads (default: cores)\n"
    "  --block-mb N      body bytes per block, MB (default: 64)\n"
    "  --max-mem-mb N    target peak RSS; sizes the blocks from it and overrides --block-mb\n"
    "  --models DIR      directory of per-class trained compressors (<class>.zlc)\n"
    "  --dump-streams D  (stats) write every block's streams under D/<class>/\n"
    "  --no-selfcheck    skip the per-block decode-and-compare during encoding\n"
    "  --no-dual         do not also try compressing each block whole\n"
    "  --level N         entropy level for every stream (disables calibration)\n"
    "  --no-calibrate    OpenZL's default level (6), no calibration\n"
    "\n"
    "Ablation switches (encoder only; archives still decode normally):\n"
    "  --no-pbwt --no-values --no-dict --no-annot --no-info-split\n"
    "  --raw-blocks      store blocks whole: the no-format-awareness floor\n"
    "  --verify          after writing, decompress and compare to the input\n"
    "  --quiet\n";

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) { std::fputs(kUsage, stderr); return 2; }
    std::string cmd = argv[1];
    Options o;
    std::vector<std::string> pos;
    for (int i = 2; i < argc; i++) {
        std::string a = argv[i];
        auto need = [&](const char* what) -> std::string {
            if (i + 1 >= argc) die(std::string("missing value for ") + what);
            return argv[++i];
        };
        if      (a == "--threads")      o.threads = std::atoi(need("--threads").c_str());
        else if (a == "--block-mb")     o.blockBytes = (size_t)std::atoll(need("--block-mb").c_str()) << 20;
        else if (a == "--max-mem-mb")   o.maxMemMB = (size_t)std::atoll(need("--max-mem-mb").c_str());
        else if (a == "--models")       o.model = need("--models");
        else if (a == "--level")        o.level = std::atoi(need("--level").c_str());
        else if (a == "--no-calibrate") o.calibrate = false;
        else if (a == "--dump-streams") o.dumpDir = need("--dump-streams");
        else if (a == "--no-selfcheck") o.selfcheck = false;
        else if (a == "--no-dual")      o.dual = false;
        else if (a == "--raw-blocks")   o.rawBlocks = true;
        else if (a == "--no-pbwt")      o.ab.pbwt = false;
        else if (a == "--no-values")    o.ab.values = false;
        else if (a == "--no-dict")      o.ab.dict = false;
        else if (a == "--no-annot")     o.ab.annot = false;
        else if (a == "--no-info-split") o.ab.infoSplit = false;
        else if (a == "--verify")       o.verify = true;
        else if (a == "--quiet")        o.quiet = true;
        else if (a == "-h" || a == "--help") { std::fputs(kUsage, stderr); return 0; }
        else if (!a.empty() && a[0] == '-') die("unknown option: " + a);
        else pos.push_back(a);
    }

    if (cmd == "compress") {
        if (pos.size() != 2) { std::fputs(kUsage, stderr); return 2; }
        cmdCompress(pos[0], pos[1], o);
        if (o.verify) {
            std::string tmp = pos[1] + ".verify.vcf";
            Options vo = o;
            vo.quiet = true;
            cmdDecompress(pos[1], tmp, vo);
            // byte-for-byte against the input, decompressing it if needed
            Reader a(pos[0]);
            FILE* b = std::fopen(tmp.c_str(), "rb");
            if (!b) die("verify: cannot reopen " + tmp);
            std::string ba(1 << 20, '\0'), bb(1 << 20, '\0');
            bool same = true;
            while (same) {
                size_t na = a.read(&ba[0], ba.size());
                size_t nb = std::fread(&bb[0], 1, bb.size(), b);
                if (na != nb || std::memcmp(ba.data(), bb.data(), na) != 0) { same = false; break; }
                if (na == 0) break;
            }
            std::fclose(b);
            ::unlink(tmp.c_str());
            if (!same) { ::unlink(pos[1].c_str()); die("--verify FAILED: round trip is not byte-exact"); }
            std::fprintf(stderr, "nyx_vcf: --verify OK (byte-exact round trip)\n");
        }
    } else if (cmd == "decompress") {
        if (pos.size() != 2) { std::fputs(kUsage, stderr); return 2; }
        cmdDecompress(pos[0], pos[1], o);
    } else if (cmd == "stats") {
        if (pos.size() != 1) { std::fputs(kUsage, stderr); return 2; }
        cmdStats(pos[0], o);
    } else if (cmd == "inspect") {
        if (pos.size() != 1) { std::fputs(kUsage, stderr); return 2; }
        cmdInspect(pos[0]);
    } else {
        std::fputs(kUsage, stderr);
        return 2;
    }
    return 0;
}
