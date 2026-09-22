// nyx_stream.h — the stream-bundle and OpenZL plumbing shared by the NYX codecs.
//
// Extracted verbatim from nyx_vcf.cpp, which had been the only user. Nothing
// here knows about any file format: a Bundle is a named set of byte streams and
// a Codec turns one into an OpenZL multi-frame and back. The format-specific
// part is the *classifier* -- the function mapping a stream name to the class
// whose trained model should be tried on it -- so that is a constructor
// argument rather than a hard-coded function.
//
// Keeping this in one place matters because the rule it encodes is the one this
// codebase has got wrong five times: a trained model is tried against the
// generic graph and kept only if it is actually smaller. See
// Codec::compressStream.

#ifndef NYX_STREAM_H
#define NYX_STREAM_H

#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "openzl/zl_compress.h"
#include "openzl/zl_compressor.h"
#include "openzl/zl_compressor_serialization.h"
#include "openzl/zl_decompress.h"
#include <zlib.h>

#include "openzl/codecs/zl_generic.h"

namespace nyx {

constexpr uint32_t kFormatVer = 16;   // OpenZL frame format version

// Set once from main() so diagnostics name the tool the user actually ran.
inline const char*& progName() { static const char* n = "nyx"; return n; }

[[noreturn]] inline void die(const std::string& m) {
    std::fprintf(stderr, "%s: %s\n", progName(), m.c_str());
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
inline uint64_t zigzag(int64_t v)    { return ((uint64_t)v << 1) ^ (uint64_t)(v >> 63); }
inline int64_t  unzigzag(uint64_t v) { return (int64_t)(v >> 1) ^ -(int64_t)(v & 1); }

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

// As above but allowing a leading '-'. "-0" is rejected: it is a distinct
// spelling of zero and would not round-trip.
inline bool parse_canonical_i64(const char* s, size_t n, int64_t& out) {
    bool neg = (n > 1 && s[0] == '-');
    uint64_t v = 0;
    if (!parse_canonical_u64(s + (neg ? 1 : 0), n - (neg ? 1 : 0), v)) return false;
    if (v > (uint64_t)INT64_MAX) return false;
    if (neg && v == 0) return false;
    out = neg ? -(int64_t)v : (int64_t)v;
    return true;
}

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
    ST_DECIMAL  = 6,   // fixed-point decimal text
    ST_INTLIST  = 7,   // comma-separated integer lists
    ST_DICT     = 8,   // low-cardinality text
    ST_PIPE     = 9,   // '|'-separated annotation fields
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
    // deque, not vector: encoders hold references to streams while still adding
    // new ones, and vector would reallocate those references out from under it.
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

// Maps a stream name and type to the class whose shipped model should be tried.
// Classes are deliberately coarse and never name a particular key or column, so
// a file carrying fields the trainer never saw still routes to a shipped model.
using Classifier = const char* (*)(const std::string& name, uint8_t type);

// One compressor per thread; graphs are stateless so this only exists because
// ZL_CCtx is not thread safe.
class Codec {
   public:
    // `modelDir` holds one serialized OpenZL compressor per stream class, named
    // <class>.zlc. Classes with no model fall back to the generic graph.
    // Decompression never needs any of this: OpenZL frames are self-describing.
    Codec(const std::string& modelDir, Classifier cls, int level = 0)
        : modelDir_(modelDir), classify_(cls), level_(level) {
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
            std::string f = compressStream(b.data[i], classify_(b.names[i], b.type[i]));
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

    // OpenZL's default compression level is 6 (ZL_COMPRESSIONLEVEL_DEFAULT), and
    // it is handed straight to the zstd backend. That default was never chosen
    // by us and it is not obviously right for streams that have already been
    // made homogeneous by a format transform: the entropy left in them is
    // long-range and repetitive, which is exactly what higher levels find. It
    // has to be measured per file, not assumed -- a hard-coded level 12 in the
    // FASTQ codec cost 22% of ratio on one library. 0 leaves OpenZL's default.
    void setLevel(int n) { level_ = n; }
    int level() const { return level_; }

    // Compresses one stream on its own; used by `stats` and by encoders that
    // price competing encodings of the same data against each other.
    size_t compressOne(const std::string& d, const std::string& name = "text",
                       uint8_t type = ST_SERIAL) {
        return compressStream(d, classify_(name, type)).size();
    }

    // One frame back to its bytes; public so a caller holding several Codecs can
    // decompress a bundle's streams in parallel.
    std::string unframe(const char* p, size_t n) { return decompressStream(p, n); }

    // One stream's frame, exactly as compressBundle would emit it. Public so a
    // caller holding several Codecs can compress a bundle's streams in parallel
    // and assemble the same bytes compressBundle produces serially.
    std::string frame(const std::string& d, const std::string& name, uint8_t type) {
        return compressStream(d, classify_(name, type));
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
        // Parameters reset between sessions unless made sticky, so set it here
        // rather than once at construction.
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
    Classifier classify_;
    int level_ = 0;
    ZL_CCtx* cctx_ = nullptr;
    ZL_DCtx* dctx_ = nullptr;
    ZL_Compressor* generic_ = nullptr;
    std::unordered_map<std::string, ZL_Compressor*> models_;
};

// Runs fn(i, codec) for i in [0, n) over the given Codecs, one thread each.
// Codecs are not thread safe, so each worker owns exactly one for its lifetime.
template <class Fn>
void parallelFor(size_t n, const std::vector<Codec*>& cods, Fn fn) {
    const size_t k = std::min(n, cods.size());
    if (k <= 1) { for (size_t i = 0; i < n; i++) fn(i, *cods[0]); return; }
    std::atomic<size_t> next{0};
    std::vector<std::thread> ts;
    for (size_t w = 0; w < k; w++)
        ts.emplace_back([&, w] {
            for (size_t i; (i = next++) < n;) fn(i, *cods[w]);
        });
    for (auto& t : ts) t.join();
}

// Byte-identical to Codec::compressBundle, with the streams compressed in
// parallel. A small file is one or two blocks, so block-level parallelism alone
// leaves most of the machine idle: measured, a 53 MB broadPeak compressed at
// 107% CPU on sixteen threads.
inline std::string compressBundleParallel(const Bundle& b, const std::vector<Codec*>& cods) {
    std::vector<std::string> frames(b.size());
    parallelFor(b.size(), cods, [&](size_t i, Codec& c) {
        frames[i] = c.frame(b.data[i], b.names[i], b.type[i]);
    });
    std::string table, body;
    put_varint(table, b.size());
    for (auto& f : frames) { put_varint(table, f.size()); body += f; }
    return table + body;
}

// Inverse of compressBundleParallel (and of Codec::compressBundle): the length
// table is read once, then every frame is decompressed on its own Codec.
inline std::vector<std::string> decompressBundleParallel(const char* p, size_t n, size_t nStreams,
                                                         const std::vector<Codec*>& cods) {
    const char* e = p + n;
    if (get_varint(p, e) != nStreams) die("stream count mismatch in block");
    std::vector<uint64_t> lens(nStreams), offs(nStreams);
    uint64_t at = 0;
    for (size_t i = 0; i < nStreams; i++) { lens[i] = get_varint(p, e); offs[i] = at; at += lens[i]; }
    if ((uint64_t)(e - p) < at) die("truncated block");
    std::vector<std::string> out(nStreams);
    parallelFor(nStreams, cods, [&](size_t i, Codec& c) { out[i] = c.unframe(p + offs[i], lens[i]); });
    return out;
}

// ------------------------------------------------------- input / framing

// Reads a plain or gzip/bgzip file transparently. Format detection is by magic
// bytes, not by name, so a .gz-less gzip file still decompresses.
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

// One entry of the block index written at the end of an archive.
struct BlockRec {
    uint64_t compLen  = 0;
    uint64_t rawLen   = 0;
    uint32_t nStreams = 0;
    uint8_t  kind     = 0;      // 0 = modelled bundle, 1 = verbatim
};

inline void writeAll(FILE* f, const void* p, size_t n) {
    if (n && std::fwrite(p, 1, n, f) != n) die("write failed");
}

}  // namespace nyx

#endif  // NYX_STREAM_H
