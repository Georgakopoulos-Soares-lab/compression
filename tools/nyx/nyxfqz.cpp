// NYX FASTQ compressor built on OpenZL.
//
// One binary, several subcommands:
//   nyxfqz pack       <in.fastq|-> <out.fqzc>          FASTQ  -> tagged container
//   nyxfqz unpack     <in.fqzc>    <out.fastq|->       container -> FASTQ
//   nyxfqz train      <sampleDir>  <out.zc>            train the compressor graph
//   nyxfqz compress   <trained.zc> <in.fastq|-> <out.nyxz>
//   nyxfqz decompress <in.nyxz>    <out.fastq|->       universal decompress + unpack
//
// Design: a FASTQ file is split (byte-exact, lossless) into per-field streams
// and serialised into a "tagged container" whose layout is understood by a
// standard OpenZL parsing/dispatch/clustering graph (modelled on
// examples/training.cpp). Because the graph only uses standard codecs, any
// frame it produces is reversible by the universal OpenZL decompressor. Our
// pack/unpack step is a pure byte transform independent of OpenZL, so
// decompress(compress(x)) == x for arbitrary input.
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <algorithm>
#include <atomic>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "openzl/codecs/zl_clustering.h"
#include "openzl/codecs/zl_conversion.h"
#include "openzl/codecs/zl_lz.h"
#include "openzl/cpp/CCtx.hpp"
#include "openzl/cpp/Compressor.hpp"
#include "openzl/cpp/DCtx.hpp"
#include "openzl/zl_compressor.h"
#include "openzl/zl_errors.h"
#include "openzl/zl_graph_api.h"

#include "tools/io/InputSetBuilder.h"
#include "tools/training/train.h"
#include "tools/training/utils/utils.h"

namespace nyx {

// ---------------------------------------------------------------------------
// Container format
// ---------------------------------------------------------------------------
// The container is a flat sequence of segments. Each segment is:
//   [4-byte LE numBytes][1-byte eltWidth][4-byte LE tag][numBytes of data]
// (This is exactly the format the parsing graph below lexes.)
//
// Records are grouped into chunks. Every chunk begins with a META segment that
// describes its layout, so the unpacker needs no other side information. After
// META come the field streams, in a fixed order determined by META:
//
//   SEQLEN, QUALLEN, PLUSLEN            (uint32-per-record length arrays)
//   SEQ, QUAL, PLUS                     (concatenated byte payloads, incl EOLs)
//   header streams (see below)
//
// Header handling has two modes (chosen per chunk):
//   * raw       : HDRLEN (uint32/rec) + HEADER bytes (as-is).
//   * columnar  : the header text is tokenised into maximal alphanumeric runs
//                 and delimiter runs. When every header in the chunk shares the
//                 same token signature, each token position becomes a column.
//                 Numeric columns (all-digit, <=18 digits) are stored as a
//                 uint64 value stream + a per-record digit-width stream (to
//                 preserve leading zeros); other columns are stored as a length
//                 stream + concatenated bytes. This exposes the incrementing
//                 read counter and the tile/x/y coordinates as numeric columns
//                 that OpenZL can delta/entropy-code far better than raw text.
//
// A record is reconstructed as '@' + header + SEQ + '+' + PLUS + QUAL, where
// each slice keeps its original trailing newline(s), so the transform is
// byte-exact. Malformed / truncated tails are emitted as a single RAW segment,
// guaranteeing losslessness on any input.

static constexpr uint32_t TAG_META    = 1;
static constexpr uint32_t TAG_SEQLEN  = 2;
static constexpr uint32_t TAG_QUALLEN = 3;
static constexpr uint32_t TAG_PLUSLEN = 4;
static constexpr uint32_t TAG_SEQ     = 5;
static constexpr uint32_t TAG_QUAL    = 6;
static constexpr uint32_t TAG_PLUS    = 7;
static constexpr uint32_t TAG_HDRLEN  = 8;  // raw header mode: lengths
static constexpr uint32_t TAG_HEADER  = 9;  // raw header mode: bytes
static constexpr uint32_t TAG_RAW     = 10; // verbatim tail

// Columnar header column-stream tag bases (+ column index).
static constexpr uint32_t TAG_HDVAL_BASE   = 1000; // uint64 values   (w8)
static constexpr uint32_t TAG_HDWID_BASE   = 2000; // uint8 digit widths (w1)
static constexpr uint32_t TAG_HTXTLEN_BASE = 3000; // uint32 token lengths (w4)
static constexpr uint32_t TAG_HTXT_BASE    = 4000; // token bytes     (w1)

// Quality order-1 context streams (+ previous-byte value 0..255, or the
// start-of-read context kQCtxStart). Each stream holds the quality bytes that
// followed a given previous value, so OpenZL's entropy coder reaches the
// order-1 conditional entropy of the quality track.
static constexpr uint32_t TAG_QCTX_BASE = 5000;
static constexpr uint32_t kQCtxStart    = 256; // start-of-read context id

// Quality RLE codec (qualMode==2): per-run char, per-run length (uint8,
// 255 means continuation — implicit for uniformity across long runs), and
// per-record run count (uint32). Activated when avg run length > 3.5.
static constexpr uint32_t TAG_QUAL_RLE_CHAR  = 50;
static constexpr uint32_t TAG_QUAL_RLE_CNT   = 51;
static constexpr uint32_t TAG_QUAL_RLE_NRUNS = 52;

// Read-clustering permutation (seqMode==1): uint32 per record giving, for each
// clustered position, the original record index. Body streams (SEQ/QUAL/PLUS +
// their lengths) are emitted in minimizer-sorted order; headers stay in
// original order; decode scatters bodies back to original order (lossless).
static constexpr uint32_t TAG_PERM = 53;

// Max header columns supported before falling back to raw header mode.
static constexpr size_t kMaxCols = 64;

static constexpr uint8_t kMetaVersion = 2;

// Records per chunk. Bounds per-segment sizes (OpenZL treats each segment as a
// stream) and gives the clustering trainer several samples per field.
static constexpr size_t kRecordsPerChunk = 128 * 1024;

// ---------------------------------------------------------------------------
// Per-chunk SEQ routing
// ---------------------------------------------------------------------------
// TAG_SEQ can be routed three ways at graph-execution time:
//   Cluster  - let the trained clustering graph decide (its baked ACE pipeline)
//   Zstd     - hard-route SEQ to plain zstd (fast; low-redundancy data)
//   BigLz    - hard-route SEQ to the 256 MiB-window LZ (slow; high-redundancy)
// The per-chunk picker sets a thread-local override before compressing each
// chunk; parsingGraphFn reads it (falling back to env vars when unset, so the
// single-model `compress`/`train` paths keep working unchanged). A thread-local
// (not an env var) is required because parallel workers compress different
// chunks with different routes simultaneously.
enum class SeqRoute : uint8_t { Unset = 0, Cluster = 1, Zstd = 2, BigLz = 3 };
static thread_local SeqRoute g_seqRouteOverride = SeqRoute::Unset;

// Resolve the effective SEQ route: thread-local override wins; else env vars.
// NYX_SEQ_ROUTE=zstd|bigwindowlz and NYX_FORCE_SEQ_BIGLZ=1 are all honoured.
static void resolveSeqRoute(bool& outZstd, bool& outBigLz)
{
    outZstd = false;
    outBigLz = false;
    switch (g_seqRouteOverride) {
        case SeqRoute::Zstd:
            outZstd = true;
            return;
        case SeqRoute::BigLz:
            outBigLz = true;
            return;
        case SeqRoute::Cluster:
            return; // neither flag -> clustering handles SEQ
        case SeqRoute::Unset:
            break;
    }
    const char* sre = std::getenv("NYX_SEQ_ROUTE");
    if (sre && std::strcmp(sre, "zstd") == 0) {
        outZstd = true;
        return;
    }
    if (sre
        && (std::strcmp(sre, "bigwindowlz") == 0
            || std::strcmp(sre, "biglz") == 0)) {
        outBigLz = true;
        return;
    }
    const char* fsb = std::getenv("NYX_FORCE_SEQ_BIGLZ");
    if (fsb && fsb[0] == '1') {
        outBigLz = true;
    }
}

static void putLE32(std::string& out, uint32_t v)
{
    char b[4];
    b[0] = (char)(v & 0xFF);
    b[1] = (char)((v >> 8) & 0xFF);
    b[2] = (char)((v >> 16) & 0xFF);
    b[3] = (char)((v >> 24) & 0xFF);
    out.append(b, 4);
}

static void putLE64(std::string& out, uint64_t v)
{
    char b[8];
    for (int i = 0; i < 8; ++i) {
        b[i] = (char)((v >> (8 * i)) & 0xFF);
    }
    out.append(b, 8);
}

static void putLE16(std::string& out, uint16_t v)
{
    out.push_back((char)(v & 0xFF));
    out.push_back((char)((v >> 8) & 0xFF));
}

static uint16_t readLE16(const uint8_t* p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static uint32_t readLE32(const uint8_t* p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16)
            | ((uint32_t)p[3] << 24);
}

static uint64_t readLE64(const uint8_t* p)
{
    uint64_t v = 0;
    for (int i = 0; i < 8; ++i) {
        v |= (uint64_t)p[i] << (8 * i);
    }
    return v;
}

static void emitSegment(
        std::string& out,
        uint8_t eltWidth,
        uint32_t tag,
        const char* data,
        size_t numBytes)
{
    putLE32(out, (uint32_t)numBytes);
    out.push_back((char)eltWidth);
    putLE32(out, tag);
    out.append(data, numBytes);
}

// Find the end (index just past the '\n') of the line starting at pos.
// Returns data.size() if there is no trailing newline (last line at EOF).
static size_t lineEnd(const std::string& data, size_t pos)
{
    size_t nl = data.find('\n', pos);
    if (nl == std::string::npos) {
        return data.size();
    }
    return nl + 1;
}

static inline bool isAlnumByte(char c)
{
    return (c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z')
            || (c >= 'a' && c <= 'z');
}

static inline bool isDigitByte(char c)
{
    return c >= '0' && c <= '9';
}

// A header slice within the source buffer (after '@', including trailing EOL).
struct Slice {
    const char* ptr;
    size_t len;
};

// One token = a maximal run that is either all-alphanumeric or all-delimiter.
struct Token {
    uint32_t off; // offset within the header slice
    uint32_t len;
};

// Tokenise a header slice into alnum / delimiter runs.
static void tokenizeHeader(const Slice& h, std::vector<Token>& toks)
{
    toks.clear();
    size_t i = 0;
    while (i < h.len) {
        bool alnum = isAlnumByte(h.ptr[i]);
        size_t j   = i + 1;
        while (j < h.len && isAlnumByte(h.ptr[j]) == alnum) {
            ++j;
        }
        toks.push_back({ (uint32_t)i, (uint32_t)(j - i) });
        i = j;
    }
}

// Emit the header field of a chunk in columnar mode. Returns false (emitting
// nothing) if the headers do not share a consistent token signature.
static bool emitColumnarHeaders(
        std::string& out,
        const std::vector<Slice>& headers,
        std::string& metaCols /* out: per-column numeric flags */)
{
    const size_t R = headers.size();
    std::vector<Token> t0;
    tokenizeHeader(headers[0], t0);
    const size_t nCols = t0.size();
    if (nCols == 0 || nCols > kMaxCols) {
        return false;
    }

    // Signature = alnum-ness of each token; must match for every record.
    std::vector<bool> alnumCol(nCols);
    for (size_t c = 0; c < nCols; ++c) {
        alnumCol[c] = isAlnumByte(headers[0].ptr[t0[c].off]);
    }

    // Tokenise all records; verify signature; collect per-record tokens.
    std::vector<std::vector<Token>> toks(R);
    toks[0] = std::move(t0);
    for (size_t r = 0; r < R; ++r) {
        if (r != 0) {
            tokenizeHeader(headers[r], toks[r]);
        }
        if (toks[r].size() != nCols) {
            return false;
        }
        for (size_t c = 0; c < nCols; ++c) {
            if (isAlnumByte(headers[r].ptr[toks[r][c].off]) != alnumCol[c]) {
                return false;
            }
        }
    }

    // A column is numeric iff every token is all-digit and short enough to fit
    // a uint64 without ambiguity.
    std::vector<bool> numericCol(nCols, false);
    for (size_t c = 0; c < nCols; ++c) {
        if (!alnumCol[c]) {
            continue;
        }
        bool numeric = true;
        for (size_t r = 0; r < R && numeric; ++r) {
            const Token& tk = toks[r][c];
            if (tk.len == 0 || tk.len > 18) {
                numeric = false;
                break;
            }
            const char* s = headers[r].ptr + tk.off;
            for (uint32_t k = 0; k < tk.len; ++k) {
                if (!isDigitByte(s[k])) {
                    numeric = false;
                    break;
                }
            }
        }
        numericCol[c] = numeric;
    }

    metaCols.clear();
    for (size_t c = 0; c < nCols; ++c) {
        metaCols.push_back((char)(numericCol[c] ? 1 : 0));
    }

    // Emit one (or two) stream(s) per column.
    for (size_t c = 0; c < nCols; ++c) {
        if (numericCol[c]) {
            std::string vals, wids;
            vals.reserve(R * 8);
            wids.reserve(R);
            for (size_t r = 0; r < R; ++r) {
                const Token& tk = toks[r][c];
                const char* s   = headers[r].ptr + tk.off;
                uint64_t v      = 0;
                for (uint32_t k = 0; k < tk.len; ++k) {
                    v = v * 10 + (uint64_t)(s[k] - '0');
                }
                putLE64(vals, v);
                wids.push_back((char)(uint8_t)tk.len);
            }
            emitSegment(
                    out, 8, TAG_HDVAL_BASE + (uint32_t)c, vals.data(),
                    vals.size());
            emitSegment(
                    out, 1, TAG_HDWID_BASE + (uint32_t)c, wids.data(),
                    wids.size());
        } else {
            std::string lens, bytes;
            lens.reserve(R * 4);
            for (size_t r = 0; r < R; ++r) {
                const Token& tk = toks[r][c];
                putLE32(lens, tk.len);
                bytes.append(headers[r].ptr + tk.off, tk.len);
            }
            emitSegment(
                    out, 4, TAG_HTXTLEN_BASE + (uint32_t)c, lens.data(),
                    lens.size());
            emitSegment(
                    out, 1, TAG_HTXT_BASE + (uint32_t)c, bytes.data(),
                    bytes.size());
        }
    }
    return true;
}

// Compute total bytes and total runs over the quality buffer (for RLE decision).
static std::pair<uint64_t, uint64_t> computeRleStats(
        const std::string& qual, const std::string& qualLen, size_t records)
{
    uint64_t totalBytes = 0, totalRuns = 0;
    size_t off = 0;
    for (size_t r = 0; r < records; ++r) {
        uint32_t ql = readLE32((const uint8_t*)qualLen.data() + r * 4);
        if (ql > 0) {
            totalBytes += ql;
            totalRuns++;
            for (uint32_t k = 1; k < ql; ++k) {
                if (qual[off + k] != qual[off + k - 1]) {
                    ++totalRuns;
                }
            }
        }
        off += ql;
    }
    return {totalBytes, totalRuns};
}

// ---- Read clustering (minimizer sort) -------------------------------------
// 2-bit base code: A=0 C=1 G=2 T=3, anything else = -1 (breaks the k-mer run).
static inline int baseCode(unsigned char c)
{
    switch (c) {
        case 'A': return 0;
        case 'C': return 1;
        case 'G': return 2;
        case 'T': return 3;
        default:  return -1;
    }
}

// Minimum canonical k-mer over a read, packed into a uint32 sort key (k<=16).
// Canonical = min(forward, reverse-complement). Reads sharing genomic origin
// tend to share their minimum minimizer, so sorting by this key makes
// overlapping reads adjacent -> genome-scale redundancy becomes local.
static uint32_t readMinimizer(const char* s, size_t len, int k)
{
    while (len > 0 && (s[len - 1] == '\n' || s[len - 1] == '\r')) {
        --len;
    }
    if ((int)len < k) {
        return UINT32_MAX;
    }
    const uint32_t mask = (k < 16) ? ((1u << (2 * k)) - 1) : 0xFFFFFFFFu;
    const int shift      = 2 * (k - 1);
    uint32_t fwd = 0, rev = 0;
    int valid    = 0;
    uint32_t best = UINT32_MAX;
    for (size_t i = 0; i < len; ++i) {
        int c = baseCode((unsigned char)s[i]);
        if (c < 0) {
            valid = 0;
            fwd   = 0;
            rev   = 0;
            continue;
        }
        fwd = ((fwd << 2) | (uint32_t)c) & mask;
        rev = (rev >> 2) | ((uint32_t)(3 - c) << shift);
        if (++valid >= k) {
            uint32_t can = (fwd < rev) ? fwd : rev;
            if (can < best) {
                best = can;
            }
        }
    }
    return best;
}

static constexpr int kMinimizerK = 15;

// Set true while the GLOBAL clustering path is compressing already-reordered
// chunks, so packFastq does NOT also cluster per-chunk (that would double-sort
// and emit a redundant per-chunk permutation). See globalClusterReorder / the
// NYXZCHK3 compress path.
static std::atomic<bool> g_globalClusterActive{ false };

// Pack FASTQ bytes into the tagged container. Always lossless.
std::string packFastq(const std::string& fq)
{
    std::string out;
    out.reserve(fq.size() + fq.size() / 8 + 64);

    size_t pos     = 0;
    const size_t n = fq.size();

    // Records per sub-chunk. When clustering is active a larger cap gives the
    // minimizer sort a bigger scope (more overlapping reads to gather), which
    // grows the sequence saving faster than the permutation cost (~log2 N).
    size_t recCap = kRecordsPerChunk;
    {
        // Per-chunk clustering mode (experiments only): NYX_CLUSTER_PERCHUNK.
        // NYX_CLUSTER (no suffix) uses the global reorder path in cmdCompress,
        // which pre-reorders reads and calls packFastq with normal recCap.
        const char* cl = std::getenv("NYX_CLUSTER_PERCHUNK");
        if (cl && cl[0] == '1') {
            recCap = 1024 * 1024; // 1M reads default when clustering
            if (const char* rc = std::getenv("NYX_CLUSTER_RECS")) {
                long v = std::atol(rc);
                if (v > 0) recCap = (size_t)v;
            }
        }
    }

    while (pos < n) {
        // Accumulators for one chunk.
        std::vector<Slice> headers;
        std::string hdr, hdrLen; // raw-header fallback buffers
        std::string seq, qual, plus;
        std::string seqLen, qualLen, plusLen;
        size_t records    = 0;
        bool aborted      = false;
        size_t chunkStart = pos; // rewind point if this chunk aborts

        while (pos < n && records < recCap) {
            size_t recStart = pos;

            // Line 1: header, must start with '@'.
            if (fq[pos] != '@') {
                aborted = true;
                pos     = recStart;
                break;
            }
            size_t l1 = lineEnd(fq, pos);
            if (l1 >= n && fq[l1 - 1] != '\n') {
                aborted = true;
                pos     = recStart;
                break;
            }

            // Line 2: sequence.
            size_t l2 = lineEnd(fq, l1);
            if (l2 >= n && (l2 == l1 || fq[l2 - 1] != '\n')) {
                aborted = true;
                pos     = recStart;
                break;
            }

            // Line 3: separator, must start with '+'.
            if (l2 >= n || fq[l2] != '+') {
                aborted = true;
                pos     = recStart;
                break;
            }
            size_t l3 = lineEnd(fq, l2);
            if (l3 >= n && fq[l3 - 1] != '\n') {
                aborted = true;
                pos     = recStart;
                break;
            }

            // Line 4: quality.
            size_t l4 = lineEnd(fq, l3);
            if (l4 > n) {
                aborted = true;
                pos     = recStart;
                break;
            }

            // Slices (byte-exact, include trailing newline where present).
            const char* hp = fq.data() + recStart + 1; // after '@'
            size_t hlen    = (l1 - recStart) - 1;
            const char* sp = fq.data() + l1; // sequence line
            size_t slen    = l2 - l1;
            const char* pp = fq.data() + l2 + 1; // after '+'
            size_t plen    = (l3 - l2) - 1;
            const char* qp = fq.data() + l3; // quality line
            size_t qlen    = l4 - l3;

            headers.push_back({ hp, hlen });
            hdr.append(hp, hlen);
            putLE32(hdrLen, (uint32_t)hlen);
            seq.append(sp, slen);
            plus.append(pp, plen);
            qual.append(qp, qlen);
            putLE32(seqLen, (uint32_t)slen);
            putLE32(qualLen, (uint32_t)qlen);
            putLE32(plusLen, (uint32_t)plen);

            ++records;
            pos = l4;
        }

        if (records > 0) {
            // ---- Optional read clustering (minimizer sort) ----------------
            // Reorder the read BODY streams (seq/qual/plus + their lengths) so
            // that reads sharing a minimum minimizer become adjacent, turning
            // genome-scale redundancy into local redundancy the fast SEQ coder
            // can reach. Headers stay in original order (their compression
            // relies on sequential IDs). A permutation stream restores exact
            // original order on decode. Gated by NYX_CLUSTER=1 for now.
            std::string permStream; // uint32 origIndex per clustered position
            bool clustered = false;
            {
                const char* cl = std::getenv("NYX_CLUSTER_PERCHUNK");
                if (cl && cl[0] == '1' && records > 1
                    && !g_globalClusterActive.load()) {
                    clustered = true;
                }
            }
            if (clustered) {
                // Per-record offsets into the concatenated body buffers.
                std::vector<size_t> sOff(records), qOff(records), pOff(records);
                std::vector<uint32_t> sLen(records), qLen(records), pLen(records);
                size_t so = 0, qo = 0, po = 0;
                for (size_t r = 0; r < records; ++r) {
                    uint32_t sl = readLE32((const uint8_t*)seqLen.data() + r * 4);
                    uint32_t ql = readLE32((const uint8_t*)qualLen.data() + r * 4);
                    uint32_t pl = readLE32((const uint8_t*)plusLen.data() + r * 4);
                    sOff[r] = so; qOff[r] = qo; pOff[r] = po;
                    sLen[r] = sl; qLen[r] = ql; pLen[r] = pl;
                    so += sl; qo += ql; po += pl;
                }
                // Sort record indices by (minimizer, original index) — stable.
                std::vector<uint32_t> order(records);
                std::vector<uint32_t> key(records);
                for (size_t r = 0; r < records; ++r) {
                    order[r] = (uint32_t)r;
                    key[r]   = readMinimizer(seq.data() + sOff[r], sLen[r],
                                             kMinimizerK);
                }
                std::sort(order.begin(), order.end(),
                          [&](uint32_t a, uint32_t b) {
                              if (key[a] != key[b]) return key[a] < key[b];
                              return a < b;
                          });
                // Rebuild body streams in clustered order.
                std::string nSeq, nQual, nPlus, nSeqLen, nQualLen, nPlusLen;
                nSeq.reserve(seq.size());
                nQual.reserve(qual.size());
                nPlus.reserve(plus.size());
                nSeqLen.reserve(seqLen.size());
                nQualLen.reserve(qualLen.size());
                nPlusLen.reserve(plusLen.size());
                permStream.reserve(records * 4);
                for (size_t np = 0; np < records; ++np) {
                    uint32_t r = order[np];
                    nSeq.append(seq.data() + sOff[r], sLen[r]);
                    nQual.append(qual.data() + qOff[r], qLen[r]);
                    nPlus.append(plus.data() + pOff[r], pLen[r]);
                    putLE32(nSeqLen, sLen[r]);
                    putLE32(nQualLen, qLen[r]);
                    putLE32(nPlusLen, pLen[r]);
                    putLE32(permStream, r);
                }
                seq.swap(nSeq);
                qual.swap(nQual);
                plus.swap(nPlus);
                seqLen.swap(nSeqLen);
                qualLen.swap(nQualLen);
                plusLen.swap(nPlusLen);
            }

            // Try columnar header mode; fall back to raw on inconsistency.
            std::string colBytes, metaCols;
            bool columnar =
                    emitColumnarHeaders(colBytes, headers, metaCols);

            // Quality: order-1 context demux (condition each byte on the
            // previous byte in the same read; reset at the start of each read).
            // Reversible because decoding reproduces the same context sequence.
            std::vector<std::string> qctx(kQCtxStart + 1);
            {
                size_t off = 0;
                for (size_t r = 0; r < records; ++r) {
                    uint32_t ql = readLE32(
                            (const uint8_t*)qualLen.data() + r * 4);
                    uint32_t ctx = kQCtxStart;
                    for (uint32_t k = 0; k < ql; ++k) {
                        unsigned char b = (unsigned char)qual[off + k];
                        qctx[ctx].push_back((char)b);
                        ctx = b;
                    }
                    off += ql;
                }
            }

            // Order-0 entropy (bits) of a byte buffer.
            auto h0bits = [](const std::string& s) -> double {
                if (s.empty()) {
                    return 0.0;
                }
                size_t cnt[256] = { 0 };
                for (unsigned char ch : s) {
                    cnt[ch]++;
                }
                double N    = (double)s.size();
                double bits = 0.0;
                for (int i = 0; i < 256; ++i) {
                    if (cnt[i]) {
                        bits += (double)cnt[i] * std::log2(N / (double)cnt[i]);
                    }
                }
                return bits;
            };
            // Demux only when conditioning on the previous value yields a real
            // reduction. For run-dominated / low-entropy quality (e.g. binned
            // NovaSeq) the plain stream compresses better via LZ, so keep it raw.
            double order0 = h0bits(qual);
            double order1 = 0.0;
            for (size_t cx = 0; cx <= kQCtxStart; ++cx) {
                order1 += h0bits(qctx[cx]);
            }
            bool qualDemux = order0 > 0 && order1 < 0.85 * order0;
            // NYX_FORCE_QUALDEMUX=0 -> force off; =1 -> force on; unset -> adaptive
            if (const char* fqd = std::getenv("NYX_FORCE_QUALDEMUX")) {
                qualDemux = (fqd[0] == '1');
            }

            // RLE decision: DISABLED by default. Empirically (variable Illumina
            // ERR9539086, avg run length 15.6) explicit RLE LOSES to both raw and
            // demux — OpenZL's entropy/context coder already exploits quality runs
            // implicitly, and splitting into char/count/nruns streams adds framing
            // overhead and loses cross-stream context. RLE only ever triggered on
            // variable Illumina (where it lost) and never on fixed/Nanopore, so
            // auto-RLE is pure downside. Kept env-gated for experimentation only.
            auto [rleBytes, rleRuns] = computeRleStats(qual, qualLen, records);
            double avgRunLen = (rleRuns > 0) ? (double)rleBytes / (double)rleRuns : 1.0;
            (void)avgRunLen;
            bool qualRle = false;
            // NYX_FORCE_QUALRLE=1 -> force on (experiments); =0 or unset -> off
            if (const char* frle = std::getenv("NYX_FORCE_QUALRLE")) {
                qualRle = (frle[0] == '1');
            }
            // RLE takes priority over demux.
            if (qualRle) {
                qualDemux = false;
            }

            uint16_t qCtxCount = 0;
            if (qualDemux) {
                for (size_t cx = 0; cx <= kQCtxStart; ++cx) {
                    if (!qctx[cx].empty()) {
                        ++qCtxCount;
                    }
                }
            }

            // In canonical FASTQ the quality length equals the sequence length;
            // when that holds for every read in the chunk, the QUALLEN stream is
            // pure duplication of SEQLEN and can be dropped.
            bool qualEqSeq = true;
            for (size_t r = 0; r < records; ++r) {
                if (readLE32((const uint8_t*)qualLen.data() + r * 4)
                    != readLE32((const uint8_t*)seqLen.data() + r * 4)) {
                    qualEqSeq = false;
                    break;
                }
            }

            std::string meta;
            meta.push_back((char)kMetaVersion);
            putLE32(meta, (uint32_t)records);
            meta.push_back((char)(columnar ? 1 : 0));
            meta.push_back((char)(clustered ? 1 : 0));            // seqMode: 0=raw, 1=clustered (body reordered, perm stream present)
            uint8_t qualModeVal = qualRle ? 2 : (qualDemux ? 1 : 0);
            meta.push_back((char)qualModeVal);                    // qualMode: 0=raw, 1=demux, 2=RLE
            meta.push_back((char)(qualEqSeq ? 1 : 0));            // lenFlags bit0
            if (columnar) {
                meta.push_back((char)(uint8_t)metaCols.size());
                meta.append(metaCols);
            }
            if (qualDemux) {
                putLE16(meta, qCtxCount);
            }
            emitSegment(out, 1, TAG_META, meta.data(), meta.size());

            // Sequence / plus streams + length arrays.
            emitSegment(out, 4, TAG_SEQLEN, seqLen.data(), seqLen.size());
            if (!qualEqSeq) {
                emitSegment(
                        out, 4, TAG_QUALLEN, qualLen.data(), qualLen.size());
            }
            emitSegment(out, 4, TAG_PLUSLEN, plusLen.data(), plusLen.size());
            if (clustered) {
                emitSegment(out, 4, TAG_PERM, permStream.data(), permStream.size());
            }
            emitSegment(out, 1, TAG_SEQ, seq.data(), seq.size());
            emitSegment(out, 1, TAG_PLUS, plus.data(), plus.size());

            // Quality: RLE streams, demuxed context streams, or a single raw stream.
            if (qualRle) {
                std::string rleChr, rleCnt, rleNruns;
                rleChr.reserve(rleRuns);
                rleCnt.reserve(rleRuns);
                rleNruns.reserve(records * 4);
                size_t off = 0;
                for (size_t r = 0; r < records; ++r) {
                    uint32_t ql = readLE32((const uint8_t*)qualLen.data() + r * 4);
                    uint32_t nruns = 0;
                    if (ql > 0) {
                        char prev = qual[off];
                        uint32_t cnt = 1;
                        for (uint32_t k = 1; k < ql; ++k) {
                            char b = qual[off + k];
                            if (b == prev) {
                                ++cnt;
                                if (cnt == 255) {
                                    rleChr.push_back(prev);
                                    rleCnt.push_back((char)255);
                                    ++nruns;
                                    cnt = 0;
                                }
                            } else {
                                if (cnt > 0) {
                                    rleChr.push_back(prev);
                                    rleCnt.push_back((char)(uint8_t)cnt);
                                    ++nruns;
                                }
                                prev = b;
                                cnt  = 1;
                            }
                        }
                        if (cnt > 0) {
                            rleChr.push_back(prev);
                            rleCnt.push_back((char)(uint8_t)cnt);
                            ++nruns;
                        }
                    }
                    putLE32(rleNruns, nruns);
                    off += ql;
                }
                emitSegment(out, 1, TAG_QUAL_RLE_CHAR,  rleChr.data(),   rleChr.size());
                emitSegment(out, 1, TAG_QUAL_RLE_CNT,   rleCnt.data(),   rleCnt.size());
                emitSegment(out, 4, TAG_QUAL_RLE_NRUNS, rleNruns.data(), rleNruns.size());
            } else if (qualDemux) {
                for (size_t cx = 0; cx <= kQCtxStart; ++cx) {
                    if (!qctx[cx].empty()) {
                        emitSegment(
                                out, 1, TAG_QCTX_BASE + (uint32_t)cx,
                                qctx[cx].data(), qctx[cx].size());
                    }
                }
            } else {
                emitSegment(out, 1, TAG_QUAL, qual.data(), qual.size());
            }

            // Header streams.
            if (columnar) {
                out.append(colBytes);
            } else {
                emitSegment(out, 4, TAG_HDRLEN, hdrLen.data(), hdrLen.size());
                emitSegment(out, 1, TAG_HEADER, hdr.data(), hdr.size());
            }
        }

        if (aborted || records == 0) {
            // Emit the rest verbatim and stop.
            size_t rawStart = (records == 0) ? chunkStart : pos;
            emitSegment(
                    out, 1, TAG_RAW, fq.data() + rawStart, n - rawStart);
            pos = n;
            break;
        }
    }

    return out;
}

// Reassemble FASTQ from the tagged container (inverse of packFastq).
std::string unpackContainer(const std::string& c)
{
    std::string out;
    out.reserve(c.size() + c.size() / 2 + 64);

    const uint8_t* p = (const uint8_t*)c.data();
    const size_t n   = c.size();
    size_t pos       = 0;

    auto nextSeg = [&](uint32_t& tag, const uint8_t*& data,
                       size_t& len) -> bool {
        if (pos + 9 > n) {
            return false;
        }
        uint32_t numBytes = readLE32(p + pos);
        tag               = readLE32(p + pos + 5);
        pos += 9;
        if (pos + numBytes > n) {
            return false;
        }
        data = p + pos;
        len  = numBytes;
        pos += numBytes;
        return true;
    };

    uint32_t tag;
    const uint8_t* data;
    size_t len;
    while (nextSeg(tag, data, len)) {
        if (tag == TAG_RAW) {
            out.append((const char*)data, len);
            continue;
        }
        if (tag != TAG_META) {
            // Unexpected: stop to avoid producing garbage.
            break;
        }

        // Parse META.
        if (len < 9) {
            break;
        }
        size_t mp        = 1; // skip version
        uint32_t records = readLE32(data + mp);
        mp += 4;
        uint8_t headerMode = data[mp++];
        uint8_t seqMode    = data[mp++]; // 0=raw, 1=clustered (body reordered)
        bool clustered     = (seqMode == 1);
        uint8_t qualMode = data[mp++];
        uint8_t lenFlags = data[mp++];
        bool qualEqSeq   = (lenFlags & 1) != 0;
        std::vector<uint8_t> numericCol;
        size_t nCols = 0;
        if (headerMode == 1) {
            if (mp >= len) {
                break;
            }
            nCols = data[mp++];
            if (mp + nCols > len) {
                break;
            }
            numericCol.assign(data + mp, data + mp + nCols);
            mp += nCols;
        }
        uint16_t qCtxCount = 0;
        if (qualMode == 1) {
            if (mp + 2 > len) {
                break;
            }
            qCtxCount = readLE16(data + mp);
            mp += 2;
        }

        // Collect the chunk's following segments into a tag map.
        std::unordered_map<uint32_t, std::pair<const uint8_t*, size_t>> seg;
        size_t want = 4 + (qualEqSeq ? 0 : 1) + (clustered ? 1u : 0u)
                + (qualMode == 1 ? (size_t)qCtxCount : (qualMode == 2 ? 3u : 1u))
                + (headerMode == 1 ? 2 * nCols : 2);
        for (size_t k = 0; k < want; ++k) {
            uint32_t t2;
            const uint8_t* d2;
            size_t l2;
            if (!nextSeg(t2, d2, l2)) {
                return out; // truncated container
            }
            seg[t2] = { d2, l2 };
        }

        const uint8_t* seqLen  = seg[TAG_SEQLEN].first;
        const uint8_t* qualLen = qualEqSeq ? seqLen : seg[TAG_QUALLEN].first;
        const uint8_t* plusLen = seg[TAG_PLUSLEN].first;
        const char* sp         = (const char*)seg[TAG_SEQ].first;
        const char* pp         = (const char*)seg[TAG_PLUS].first;

        // Quality reconstruction cursors.
        const char* qp = nullptr;
        std::vector<const char*> qctxCur;
        const char* rleChrCur      = nullptr;
        const char* rleCntCur      = nullptr;
        const uint8_t* rleNrunsCur = nullptr;
        if (qualMode == 2) {
            rleChrCur   = (const char*)seg[TAG_QUAL_RLE_CHAR].first;
            rleCntCur   = (const char*)seg[TAG_QUAL_RLE_CNT].first;
            rleNrunsCur = seg[TAG_QUAL_RLE_NRUNS].first;
        } else if (qualMode == 1) {
            qctxCur.assign(kQCtxStart + 1, nullptr);
            for (size_t cx = 0; cx <= kQCtxStart; ++cx) {
                auto it = seg.find(TAG_QCTX_BASE + (uint32_t)cx);
                if (it != seg.end()) {
                    qctxCur[cx] = (const char*)it->second.first;
                }
            }
        } else {
            qp = (const char*)seg[TAG_QUAL].first;
        }

        // Header reconstruction cursors.
        const char* rawHp = nullptr;
        const uint8_t* rawHl = nullptr;
        std::vector<const char*> txtCur(nCols, nullptr);
        if (headerMode == 0) {
            rawHl = seg[TAG_HDRLEN].first;
            rawHp = (const char*)seg[TAG_HEADER].first;
        } else {
            for (size_t cc = 0; cc < nCols; ++cc) {
                if (!numericCol[cc]) {
                    txtCur[cc] = (const char*)seg[TAG_HTXT_BASE + cc].first;
                }
            }
        }

        // Clustered mode: body streams are in minimizer-sorted order. Walk them
        // once and scatter each read's seq/plus/qual back to its ORIGINAL index
        // via the permutation, so the output loop below emits original order.
        std::vector<std::string> oSeq, oPlus, oQual;
        if (clustered) {
            const uint8_t* perm = seg[TAG_PERM].first;
            oSeq.resize(records);
            oPlus.resize(records);
            oQual.resize(records);
            for (size_t np = 0; np < records; ++np) {
                uint32_t sl   = readLE32(seqLen + np * 4);
                uint32_t ql   = readLE32(qualLen + np * 4);
                uint32_t pl   = readLE32(plusLen + np * 4);
                uint32_t orig = readLE32(perm + np * 4);
                oSeq[orig].assign(sp, sl);
                sp += sl;
                oPlus[orig].assign(pp, pl);
                pp += pl;
                std::string q;
                q.reserve(ql);
                if (qualMode == 2) {
                    uint32_t nruns = readLE32(rleNrunsCur + np * 4);
                    for (uint32_t run = 0; run < nruns; ++run) {
                        char ch     = *rleChrCur++;
                        uint8_t cnt = (uint8_t)*rleCntCur++;
                        q.append(cnt, ch);
                    }
                } else if (qualMode == 1) {
                    uint32_t ctx = kQCtxStart;
                    for (uint32_t k = 0; k < ql; ++k) {
                        char b = *qctxCur[ctx]++;
                        q.push_back(b);
                        ctx = (unsigned char)b;
                    }
                } else {
                    q.assign(qp, ql);
                    qp += ql;
                }
                oQual[orig].swap(q);
            }
        }

        for (size_t i = 0; i < records; ++i) {
            out.push_back('@');
            if (headerMode == 0) {
                uint32_t hl = readLE32(rawHl + i * 4);
                out.append(rawHp, hl);
                rawHp += hl;
            } else {
                for (size_t cc = 0; cc < nCols; ++cc) {
                    if (numericCol[cc]) {
                        const uint8_t* v = seg[TAG_HDVAL_BASE + cc].first;
                        const uint8_t* w = seg[TAG_HDWID_BASE + cc].first;
                        uint64_t val     = readLE64(v + i * 8);
                        uint8_t wid      = w[i];
                        char buf[20];
                        int bl = 0;
                        if (val == 0) {
                            buf[bl++] = '0';
                        } else {
                            while (val > 0) {
                                buf[bl++] = (char)('0' + (val % 10));
                                val /= 10;
                            }
                        }
                        // Zero-pad to the original digit width.
                        for (int z = bl; z < (int)wid; ++z) {
                            out.push_back('0');
                        }
                        for (int z = bl - 1; z >= 0; --z) {
                            out.push_back(buf[z]);
                        }
                    } else {
                        const uint8_t* tl =
                                seg[TAG_HTXTLEN_BASE + cc].first;
                        uint32_t tlen = readLE32(tl + i * 4);
                        out.append(txtCur[cc], tlen);
                        txtCur[cc] += tlen;
                    }
                }
            }

            if (clustered) {
                // Bodies were reconstructed into original order in pass 1.
                out.append(oSeq[i]);
                out.push_back('+');
                out.append(oPlus[i]);
                out.append(oQual[i]);
                continue;
            }

            uint32_t sl = readLE32(seqLen + i * 4);
            uint32_t ql = readLE32(qualLen + i * 4);
            uint32_t pl = readLE32(plusLen + i * 4);
            out.append(sp, sl);
            sp += sl;
            out.push_back('+');
            out.append(pp, pl);
            pp += pl;
            if (qualMode == 2) {
                uint32_t nruns = readLE32(rleNrunsCur + i * 4);
                for (uint32_t run = 0; run < nruns; ++run) {
                    char ch     = *rleChrCur++;
                    uint8_t cnt = (uint8_t)*rleCntCur++;
                    out.append(cnt, ch);
                }
            } else if (qualMode == 1) {
                uint32_t ctx = kQCtxStart;
                for (uint32_t k = 0; k < ql; ++k) {
                    char b = *qctxCur[ctx]++;
                    out.push_back(b);
                    ctx = (unsigned char)b;
                }
            } else {
                out.append(qp, ql);
                qp += ql;
            }
        }
    }
    return out;
}

// ---------------------------------------------------------------------------
// OpenZL parsing / dispatch / clustering graph
// (adapted from examples/training.cpp)
// ---------------------------------------------------------------------------

static ZL_Report parsingGraphFn(
        ZL_Graph* graph,
        ZL_Edge* inputEdges[],
        size_t numInputs) noexcept
{
    ZL_RESULT_DECLARE_SCOPE_REPORT(graph);
    assert(numInputs == 1);
    const ZL_Input* const input    = ZL_Edge_getData(inputEdges[0]);
    const uint8_t* const inputData = (const uint8_t*)ZL_Input_ptr(input);
    const size_t inputSize         = ZL_Input_numElts(input);

    std::vector<unsigned> dispatchIdxs;
    std::vector<size_t> sizes;
    // Element width for each distinct dispatch tag (metadata tags use width 1).
    std::unordered_map<uint32_t, uint8_t> dispatchIdxToEltWidth;

    std::unordered_map<uint32_t, uint32_t> tagToDispatchIdx;
    std::unordered_map<uint32_t, uint32_t> dispatchIdxToTag;
    uint32_t currentDispatchIdx     = 0;
    constexpr unsigned kNumBytesTag = 100;
    constexpr unsigned kEltWidthTag = 101;
    constexpr unsigned kInputTag    = 102;

    dispatchIdxToTag[currentDispatchIdx] = kNumBytesTag;
    tagToDispatchIdx[kNumBytesTag]       = currentDispatchIdx++;
    dispatchIdxToTag[currentDispatchIdx] = kEltWidthTag;
    tagToDispatchIdx[kEltWidthTag]       = currentDispatchIdx++;
    dispatchIdxToTag[currentDispatchIdx] = kInputTag;
    tagToDispatchIdx[kInputTag]          = currentDispatchIdx++;

    for (size_t inputPos = 0; inputPos < inputSize;) {
        ZL_ERR_IF_LT(inputSize - inputPos, 9, srcSize_tooSmall);
        const uint32_t numBytes = readLE32(inputData + inputPos);
        const uint8_t eltWidth  = inputData[inputPos + 4];
        const uint32_t inputTag = readLE32(inputData + inputPos + 5);

        if (tagToDispatchIdx.count(inputTag) == 0) {
            dispatchIdxToTag[currentDispatchIdx]     = inputTag;
            dispatchIdxToEltWidth[currentDispatchIdx] = eltWidth;
            tagToDispatchIdx[inputTag]               = currentDispatchIdx++;
        }
        ZL_ERR_IF_EQ(eltWidth, 0, corruption);
        ZL_ERR_IF_NE(numBytes % eltWidth, 0, corruption);
        // All segments sharing a tag must share an element width.
        ZL_ERR_IF_NE(
                (uint32_t)dispatchIdxToEltWidth[tagToDispatchIdx[inputTag]],
                (uint32_t)eltWidth,
                corruption);
        inputPos += 9;
        ZL_ERR_IF_LT(inputSize - inputPos, numBytes, srcSize_tooSmall);
        inputPos += numBytes;

        dispatchIdxs.push_back(tagToDispatchIdx[kNumBytesTag]);
        sizes.push_back(4);
        dispatchIdxs.push_back(tagToDispatchIdx[kEltWidthTag]);
        sizes.push_back(1);
        dispatchIdxs.push_back(tagToDispatchIdx[kInputTag]);
        sizes.push_back(4);
        dispatchIdxs.push_back(tagToDispatchIdx[inputTag]);
        sizes.push_back(numBytes);
    }

    const ZL_DispatchInstructions instructions = {
        .segmentSizes = sizes.data(),
        .tags         = dispatchIdxs.data(),
        .nbSegments   = sizes.size(),
        .nbTags       = currentDispatchIdx,
    };
    ZL_TRY_LET(
            ZL_EdgeList,
            dispatchEdges,
            ZL_Edge_runDispatchNode(inputEdges[0], &instructions));
    assert(dispatchEdges.nbEdges == 2 + currentDispatchIdx);

    std::vector<ZL_Edge*> outputEdges;
    outputEdges.reserve(dispatchEdges.nbEdges);

    // tags & sizes streams -> generic
    ZL_ERR_IF_ERR(ZL_Edge_setDestination(
            dispatchEdges.edges[0], ZL_GRAPH_COMPRESS_GENERIC));
    ZL_ERR_IF_ERR(ZL_Edge_setDestination(
            dispatchEdges.edges[1], ZL_GRAPH_COMPRESS_GENERIC));
    dispatchEdges.edges += 2;

    // The first three dispatch streams hold the per-segment framing fields
    // (numBytes / eltWidth / inputTag). These describe the container itself, not
    // user data; send them straight to generic compression. Routing them
    // through the *trained* clustering is fragile: the trainer may learn a
    // fixed-width struct transform that fails when another file's framing stream
    // has a different length.
    for (uint32_t d = 0; d < 3; d++) {
        ZL_ERR_IF_ERR(ZL_Edge_setDestination(
                dispatchEdges.edges[d], ZL_GRAPH_COMPRESS_GENERIC));
    }

    // Each remaining stream is one distinct data tag (segments of that tag
    // concatenated). Interpret it as little-endian numeric using the element
    // width recorded for that tag.
    // Each remaining stream is one distinct data tag (segments of that tag
    // concatenated). Control streams (META and the RAW verbatim tail) are sent
    // straight to generic compression; everything else is interpreted as
    // little-endian numeric (by its element width) and handed to clustering.
    ZL_GraphIDList customGraphs = ZL_Graph_getCustomGraphs(graph);
    ZL_ERR_IF_NE(customGraphs.nbGraphIDs, 2, graphParameter_invalid);
    // SEQ route for this chunk: thread-local override (set by the per-chunk
    // picker) wins, else NYX_SEQ_ROUTE / NYX_FORCE_SEQ_BIGLZ env vars. Read live
    // at graph-execution time (both during training's internal trial
    // compressions and at final compress time after deserialize rebuilds this
    // function graph).
    bool forceSeqBigLz = false;
    bool seqRouteZstd  = false;
    resolveSeqRoute(seqRouteZstd, forceSeqBigLz);
    for (uint32_t d = 3; d < currentDispatchIdx; d++) {
        uint32_t tagVal = dispatchIdxToTag[d];
        if (tagVal == TAG_META || tagVal == TAG_RAW) {
            ZL_ERR_IF_ERR(ZL_Edge_setDestination(
                    dispatchEdges.edges[d], ZL_GRAPH_COMPRESS_GENERIC));
            continue;
        }
        // NYX_SEQ_ROUTE=zstd: bypass clustering & bigWindowLz, send TAG_SEQ
        // directly to plain zstd.
        if (tagVal == TAG_SEQ && seqRouteZstd) {
            ZL_NodeID node = ZL_Node_interpretAsLE(dispatchIdxToEltWidth[d] * 8);
            ZL_TRY_LET_CONST(
                    ZL_EdgeList,
                    convertEdges,
                    ZL_Edge_runNode(dispatchEdges.edges[d], node));
            assert(convertEdges.nbEdges == 1);
            ZL_ERR_IF_ERR(ZL_Edge_setDestination(
                    convertEdges.edges[0], ZL_GRAPH_ZSTD));
            continue;
        }
        // NYX_FORCE_SEQ_BIGLZ=1: bypass clustering, send TAG_SEQ directly to bigWindowLz
        if (tagVal == TAG_SEQ && forceSeqBigLz) {
            ZL_NodeID node = ZL_Node_interpretAsLE(dispatchIdxToEltWidth[d] * 8);
            ZL_TRY_LET_CONST(
                    ZL_EdgeList,
                    convertEdges,
                    ZL_Edge_runNode(dispatchEdges.edges[d], node));
            assert(convertEdges.nbEdges == 1);
            ZL_ERR_IF_ERR(ZL_Edge_setDestination(
                    convertEdges.edges[0], customGraphs.graphids[1]));
            continue;
        }
        ZL_NodeID node = ZL_Node_interpretAsLE(dispatchIdxToEltWidth[d] * 8);
        ZL_TRY_LET_CONST(
                ZL_EdgeList,
                convertEdges,
                ZL_Edge_runNode(dispatchEdges.edges[d], node));
        assert(convertEdges.nbEdges == 1);
        ZL_ERR_IF_ERR(ZL_Edge_setIntMetadata(
                convertEdges.edges[0],
                ZL_CLUSTERING_TAG_METADATA_ID,
                dispatchIdxToTag[d]));
        outputEdges.push_back(convertEdges.edges[0]);
    }

    ZL_ERR_IF_ERR(ZL_Edge_setParameterizedDestination(
            outputEdges.data(),
            outputEdges.size(),
            customGraphs.graphids[0],
            NULL));
    return ZL_returnSuccess();
}

static ZL_GraphID registerParsingGraph(
        openzl::Compressor& compressor,
        const ZL_GraphID clusteringGraph,
        const ZL_GraphID bigWindowLz)
{
    auto parsingGraph = compressor.getGraph("FASTQ Parsing Compressor");
    if (!parsingGraph) {
        ZL_Type inputTypeMask                  = ZL_Type_serial;
        ZL_FunctionGraphDesc parsingCompressor = {
            .name           = "!FASTQ Parsing Compressor",
            .graph_f        = parsingGraphFn,
            .inputTypeMasks = &inputTypeMask,
            .nbInputs       = 1,
            .customGraphs   = NULL,
            .nbCustomGraphs = 0,
            .localParams    = {},
        };
        parsingGraph = compressor.registerFunctionGraph(parsingCompressor);
    }
    std::vector<ZL_GraphID> customGraphs = { clusteringGraph, bigWindowLz };
    openzl::GraphParameters params       = {
        .customGraphs = std::move(customGraphs),
    };
    parsingGraph = compressor.parameterizeGraph(parsingGraph.value(), params);
    return parsingGraph.value();
}

static ZL_GraphID registerFastqGraph(openzl::Compressor& compressor)
{
    ZL_ClusteringConfig defaultConfig{
        .nbClusters     = 0,
        .nbTypeDefaults = 0,
    };

    // A large-window LZ graph. ZL_NODE_LZ with a 2^28 (256 MiB) window captures
    // long-range repeats (e.g. in repetitive genomes) across the whole
    // concatenated sequence stream, which the default small LZ/zstd window
    // misses. Its four outputs (literals / offsets / literal-lengths /
    // match-lengths) are each sent to generic compression.
    // windowLog + acceleration are tunable via env for experiments:
    //   NYX_LZ_WINDOWLOG (10..28, default 28)
    //   NYX_LZ_ACCEL     (>=1; unset => level-derived default, i.e. 1)
    // Defaults reproduce the original 2^28 window / acceleration 1 behaviour.
    int wlog = 28;
    if (const char* e = std::getenv("NYX_LZ_WINDOWLOG")) {
        int v = atoi(e);
        if (v >= 10 && v <= 28) {
            wlog = v;
        }
    }
    int accel = 0; // 0 => leave the node's level-derived default
    if (const char* e = std::getenv("NYX_LZ_ACCEL")) {
        int v = atoi(e);
        if (v >= 1) {
            accel = v;
        }
    }
    ZL_IntParam ip[2];
    size_t nip  = 0;
    ip[nip++]   = ZL_IntParam{ ZL_LzParam_windowLog, wlog };
    if (accel > 0) {
        ip[nip++] = ZL_IntParam{ ZL_LzParam_acceleration, accel };
    }
    ZL_LocalParams lzParams         = {};
    lzParams.intParams              = ZL_LocalIntParams{ ip, nip };
    ZL_ParameterizedNodeDesc lzDesc = {};
    lzDesc.name                     = "nyx_bigwindow_lz";
    lzDesc.node                     = ZL_NODE_LZ;
    lzDesc.localParams              = &lzParams;
    ZL_NodeID bigLzNode =
            ZL_Compressor_registerParameterizedNode(compressor.get(), &lzDesc);
    ZL_GraphID lzOuts[4] = {
        ZL_GRAPH_COMPRESS_GENERIC,
        ZL_GRAPH_COMPRESS_GENERIC,
        ZL_GRAPH_COMPRESS_GENERIC,
        ZL_GRAPH_COMPRESS_GENERIC,
    };
    ZL_GraphID bigWindowLz = ZL_Compressor_registerStaticGraph_fromNode(
            compressor.get(), bigLzNode, lzOuts, 4);

    std::vector<ZL_GraphID> successors = {
        ZL_GRAPH_STORE,
        ZL_GRAPH_ZSTD,
        ZL_GRAPH_COMPRESS_GENERIC,
        ZL_GRAPH_LZ,
        bigWindowLz,
        ZL_Compressor_registerStaticGraph_fromNode1o(
                compressor.get(), ZL_NODE_DELTA_INT, ZL_GRAPH_FIELD_LZ),
    };
    ZL_GraphID clusteringGraph = ZL_Clustering_registerGraph(
            compressor.get(),
            &defaultConfig,
            successors.data(),
            successors.size());
    return registerParsingGraph(compressor, clusteringGraph, bigWindowLz);
}

static std::unique_ptr<openzl::Compressor> createCompressorFromSerialized(
        openzl::poly::string_view serialized,
        openzl::poly::string_view fatBundle)
{
    auto compressor = std::make_unique<openzl::Compressor>();
    registerFastqGraph(*compressor);
    compressor->deserialize(serialized, fatBundle);
    return compressor;
}

// ---------------------------------------------------------------------------
// I/O helpers
// ---------------------------------------------------------------------------

static std::string readAll(const std::string& path)
{
    if (path == "-") {
        std::ostringstream ss;
        ss << std::cin.rdbuf();
        return ss.str();
    }
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        throw std::runtime_error("cannot open input: " + path);
    }
    std::ostringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

static void writeAll(const std::string& path, const std::string& data)
{
    if (path == "-") {
        std::cout.write(data.data(), (std::streamsize)data.size());
        return;
    }
    std::ofstream f(path, std::ios::binary);
    if (!f) {
        throw std::runtime_error("cannot open output: " + path);
    }
    f.write(data.data(), (std::streamsize)data.size());
}

// ---------------------------------------------------------------------------
// Subcommands
// ---------------------------------------------------------------------------

static int cmdPack(const std::string& in, const std::string& out)
{
    writeAll(out, packFastq(readAll(in)));
    return 0;
}

static int cmdUnpack(const std::string& in, const std::string& out)
{
    writeAll(out, unpackContainer(readAll(in)));
    return 0;
}

static int cmdTrain(const std::string& sampleDir, const std::string& outPath)
{
    auto inputs =
            openzl::tools::io::InputSetBuilder(true).add_path(sampleDir).build();

    openzl::Compressor compressor;
    auto graphId = registerFastqGraph(compressor);
    openzl::unwrap(
            ZL_Compressor_selectStartingGraphID(compressor.get(), graphId),
            "Failed to select starting graph ID",
            compressor.get());

    openzl::training::TrainParams trainParams = {
        .compressorGenFunc = createCompressorFromSerialized,
        .threads           = std::max(1u, std::thread::hardware_concurrency()),
        .clusteringTrainer = openzl::training::ClusteringTrainer::Greedy,
    };
    auto multiInputs = openzl::training::inputSetToMultiInputs(*inputs);
    auto trained = openzl::training::train(multiInputs, compressor, trainParams);
    if (trained.empty()) {
        std::cerr << "training produced no compressor" << std::endl;
        return 1;
    }
    writeAll(outPath, trained[0].serializedCompressor);
    std::cerr << "wrote trained compressor to " << outPath << " ("
              << trained[0].serializedCompressor.size() << " bytes)"
              << std::endl;
    return 0;
}

// Peek at the first `nReads` records of a FASTQ and return true if every read
// has the same sequence length (fixed-length file), false if variable-length.
// Returns true on any I/O error (safe fallback: fixed is the smaller set).
static bool detectFixedLength(const std::string& path, size_t nReads = 20)
{
    try {
        std::istream* src = nullptr;
        std::ifstream f;
        if (path == "-") {
            src = &std::cin;
        } else {
            f.open(path, std::ios::binary);
            if (!f) {
                return true;
            }
            src = &f;
        }
        int firstLen = -1;
        size_t seen  = 0;
        std::string line;
        int lineNo = 0;
        while (seen < nReads && std::getline(*src, line)) {
            if (lineNo % 4 == 1) { // sequence line
                // strip possible CR
                int len = (int)line.size();
                if (!line.empty() && line.back() == '\r') {
                    --len;
                }
                if (firstLen < 0) {
                    firstLen = len;
                } else if (len != firstLen) {
                    return false;
                }
                ++seen;
            }
            ++lineNo;
        }
        return true; // all equal (or < nReads reads)
    } catch (...) {
        return true;
    }
}

// Resolve the actual .zc compressor file to use. If `compressorPath` is a
// directory, auto-detect fixed vs variable from the input and select:
//   <dir>/fastq_fixed.zc  or  <dir>/fastq_var.zc
// Falls back to a single .zc file in the directory if only one exists.
static std::string resolveCompressor(
        const std::string& compressorPath,
        const std::string& inputPath)
{
    namespace fs = std::filesystem;
    if (!fs::is_directory(compressorPath)) {
        return compressorPath; // plain .zc path, use as-is
    }
    std::string fixedZc = compressorPath + "/fastq_fixed.zc";
    std::string varZc   = compressorPath + "/fastq_var.zc";
    bool haveFixed      = fs::exists(fixedZc);
    bool haveVar        = fs::exists(varZc);
    if (!haveFixed && !haveVar) {
        throw std::runtime_error(
                "no fastq_fixed.zc or fastq_var.zc found in " + compressorPath);
    }
    if (haveFixed && !haveVar) {
        return fixedZc;
    }
    if (!haveFixed && haveVar) {
        return varZc;
    }
    // Both exist — auto-detect from the input.
    bool fixed = detectFixedLength(inputPath);
    std::cerr << "auto-detected read lengths: "
              << (fixed ? "fixed" : "variable") << " -> using "
              << (fixed ? fixedZc : varZc) << std::endl;
    return fixed ? fixedZc : varZc;
}

// ---------------------------------------------------------------------------
// Chunked, multithreaded compression
// ---------------------------------------------------------------------------
//
// Large files are split into record-aligned chunks that are compressed
// independently and in parallel, then packed into a single archive. This keeps
// peak memory bounded (only `threads` chunks are resident at once) and removes
// the whole-file OpenZL runtime-graph node limit, while giving near-linear
// speedup on multicore machines.
//
// Chunk sizing: with a memory budget of M MB and T threads, each chunk targets
// M/T MB of *input*, so at most ~M MB of raw records are in flight concurrently
// (working memory is a small multiple of that). Chunks are cut only at true
// 4-line record boundaries so every chunk is independently packable.
//
// Archive layout (little-endian):
//   [8B magic "NYXZCHK1"][u32 nChunks][u32 threadsUsed][u64 origSize]
//   [u64 frameLen] * nChunks
//   [frame bytes]  * nChunks     (each frame is a standard OpenZL frame)
//
// `decompress` detects the magic; files without it are treated as a legacy
// single-frame .nyxz and decoded with the universal path (backward compatible).

static const char kArchiveMagic[8] = { 'N', 'Y', 'X', 'Z', 'C', 'H', 'K', '1' };

// Scan the input file once and return contiguous, record-aligned byte ranges
// [offset, offset+len) that together cover the whole file. A cut is made after
// a completed 4-line record once the accumulated chunk reaches `targetBytes`.
static std::vector<std::pair<uint64_t, uint64_t>> scanChunkBoundaries(
        const std::string& path, uint64_t targetBytes)
{
    std::vector<std::pair<uint64_t, uint64_t>> ranges;
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        throw std::runtime_error("cannot open input: " + path);
    }
    f.seekg(0, std::ios::end);
    uint64_t fileSize = (uint64_t)f.tellg();
    f.seekg(0, std::ios::beg);
    if (fileSize == 0) {
        return ranges;
    }
    if (targetBytes < 1) {
        targetBytes = 1;
    }

    const size_t kBuf = 1u << 20;
    std::vector<char> buf(kBuf);
    uint64_t globalPos  = 0;
    uint64_t chunkStart = 0;
    uint64_t lineCount  = 0;
    while (globalPos < fileSize) {
        f.read(buf.data(), (std::streamsize)kBuf);
        std::streamsize got = f.gcount();
        if (got <= 0) {
            break;
        }
        const char* base = buf.data();
        const char* end  = base + got;
        const char* nl   = base;
        while ((nl = (const char*)memchr(nl, '\n', (size_t)(end - nl)))) {
            uint64_t nlPos = globalPos + (uint64_t)(nl - base);
            ++lineCount;
            if ((lineCount & 3u) == 0) { // completed a 4-line record
                uint64_t boundary = nlPos + 1;
                if (boundary - chunkStart >= targetBytes) {
                    ranges.emplace_back(chunkStart, boundary - chunkStart);
                    chunkStart = boundary;
                }
            }
            ++nl;
            if (nl >= end) {
                break;
            }
        }
        globalPos += (uint64_t)got;
    }
    if (chunkStart < fileSize) {
        ranges.emplace_back(chunkStart, fileSize - chunkStart);
    }
    return ranges;
}

// Read a byte range from a file (each worker owns its own stream).
static std::string readFileRange(std::ifstream& f, uint64_t off, uint64_t len)
{
    std::string s;
    s.resize(len);
    f.seekg((std::streamoff)off, std::ios::beg);
    f.read(&s[0], (std::streamsize)len);
    return s;
}

// Returns true if NYX_SORT_READS=1 is set.
static bool sortReadsEnabled()
{
    static const bool kEnabled = [](){
        const char* e = std::getenv("NYX_SORT_READS");
        return e && e[0] == '1';
    }();
    return kEnabled;
}

// Sort FASTQ reads in a raw chunk by their strand-canonical sequence prefix.
// Groups reads from similar genomic loci together, improving cross-read LZ
// redundancy within each chunk (approximates SPRING's read-reordering benefit).
// NOTE: this scrambles the original read order (sequential IDs become random),
// which hurts ID stream compression on low-coverage/metagenomics data. Enable
// only for high-coverage genomic data via NYX_SORT_READS=1.
static void sortReadsInChunk(std::string& raw, int K = 20)
{
    if (raw.empty()) return;

    // ACGT complement table; everything else maps to 'N'.
    static const auto kComp = [](){
        std::array<unsigned char, 256> t;
        t.fill('N');
        t['A']='T'; t['T']='A'; t['C']='G'; t['G']='C';
        t['a']='t'; t['t']='a'; t['c']='g'; t['g']='c';
        return t;
    }();

    // Strand-canonical prefix: lexicographic min of fwd[0:K] and its revcomp.
    auto makeKey = [&](const char* seq, size_t slen) -> std::string {
        int len = (int)std::min((size_t)K, slen);
        std::string fwd(seq, len);
        std::string rc(len, 'N');
        for (int i = 0; i < len; ++i)
            rc[len - 1 - i] = (char)kComp[(unsigned char)seq[i]];
        return fwd < rc ? fwd : rc;
    };

    struct Rec {
        const char *id_s, *id_e, *seq_s, *seq_e, *plus_s, *plus_e, *qual_s, *qual_e;
        std::string key;
    };

    std::vector<Rec> recs;
    recs.reserve(raw.size() / 200);

    const char* p   = raw.data();
    const char* end = raw.data() + raw.size();

    auto findEOL = [](const char* pp, const char* e) -> const char* {
        while (pp < e && *pp != '\n') ++pp;
        return pp;
    };

    while (p < end) {
        if (p >= end) break;
        Rec r;
        r.id_s   = p; r.id_e   = findEOL(p, end); p = r.id_e < end ? r.id_e + 1 : end;
        r.seq_s  = p; r.seq_e  = findEOL(p, end); p = r.seq_e < end ? r.seq_e + 1 : end;
        r.plus_s = p; r.plus_e = findEOL(p, end); p = r.plus_e < end ? r.plus_e + 1 : end;
        r.qual_s = p; r.qual_e = findEOL(p, end); p = r.qual_e < end ? r.qual_e + 1 : end;
        if (r.id_e == r.id_s) continue;
        if (*r.id_s != '@') continue;
        r.key = makeKey(r.seq_s, (size_t)(r.seq_e - r.seq_s));
        recs.push_back(std::move(r));
    }

    if (recs.size() < 2) return;

    std::sort(recs.begin(), recs.end(),
              [](const Rec& a, const Rec& b){ return a.key < b.key; });

    std::string out;
    out.reserve(raw.size());
    for (const auto& r : recs) {
        out.append(r.id_s,   r.id_e   - r.id_s);   out.push_back('\n');
        out.append(r.seq_s,  r.seq_e  - r.seq_s);  out.push_back('\n');
        out.append(r.plus_s, r.plus_e - r.plus_s); out.push_back('\n');
        out.append(r.qual_s, r.qual_e - r.qual_s); out.push_back('\n');
    }
    raw = std::move(out);
}

// Compress one already-loaded chunk of raw FASTQ bytes into an OpenZL frame.
static std::string compressChunkBytes(
        openzl::Compressor& compressor, const std::string& rawChunk)
{
    std::string container = packFastq(rawChunk);
    openzl::CCtx cctx;
    cctx.setParameter(openzl::CParam::FormatVersion, ZL_MAX_FORMAT_VERSION);
    cctx.setParameter(openzl::CParam::PermissiveCompression, 1);
    cctx.setParameter(openzl::CParam::CompressionLevel, 19);
    cctx.refCompressor(compressor);
    std::string frame;
    frame.resize(openzl::compressBound(container.size()));
    size_t csize = cctx.compressSerial(frame, container);
    frame.resize(csize);
    return frame;
}

// ---------------------------------------------------------------------------
// Per-chunk model picker
// ---------------------------------------------------------------------------
// For each record-aligned chunk we run a fast redundancy probe on a SAMPLE of
// its reads and decide, independently per chunk, whether the sequence stream
// has enough cross-read repetition to be worth the slow-but-thorough big-window
// LZ, or whether fast zstd gives essentially the same ratio. Combined with a
// data-type classification (Nanopore long-read vs Illumina short-read, and
// fixed vs variable length for Illumina) this selects one of up to five trained
// models. The choice is recorded per chunk in the archive header.
//
// Redundancy probe: canonical (strand-independent) hashed minimizers. For each
// sampled read we collect the set of its minimizers; a read is "shared" if any
// of its minimizers also occurs in another sampled read. frac_shared is the
// fraction of sampled reads that are shared. High-coverage / small-genome data
// (reads re-cover the same loci) -> many shared minimizers -> high frac_shared
// -> LZ. Noisy low-coverage long reads -> mostly unique -> low -> zstd.

// Probe / classification tunables.
static constexpr size_t kProbeSampleReads = 4000; // reads sampled per chunk
static constexpr int kProbeK              = 15;   // k-mer length
static constexpr int kProbeW              = 11;   // minimizer window (k-mers)
static constexpr uint32_t kProbeScanBases = 2000; // max bases scanned per read
static constexpr size_t kProbeMaxMinimizers = 512; // cap minimizers per read
static constexpr double kFracSharedThreshold = 0.30; // > -> LZ, else zstd
static constexpr double kNanoMeanLenThreshold = 500.0; // mean bp -> Nanopore

static inline uint64_t splitmix64(uint64_t x)
{
    x += 0x9E3779B97F4A7C15ULL;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBULL;
    return x ^ (x >> 31);
}

static inline int base2bit(char c)
{
    switch (c) {
        case 'A': case 'a': return 0;
        case 'C': case 'c': return 1;
        case 'G': case 'g': return 2;
        case 'T': case 't': return 3;
        default:            return -1;
    }
}

// Sorted-unique canonical hashed minimizers of a sequence slice.
static void readMinimizers(const char* s, uint32_t len, std::vector<uint64_t>& out)
{
    out.clear();
    const int K = kProbeK, W = kProbeW;
    if (len < (uint32_t)K) {
        return;
    }
    const uint32_t scan = std::min<uint32_t>(len, kProbeScanBases);
    const uint64_t mask = (1ULL << (2 * K)) - 1;
    uint64_t fwd = 0, rev = 0;
    int valid = 0;
    // Per-position canonical k-mer hash (UINT64_MAX where no valid k-mer ends).
    std::vector<uint64_t> kh;
    kh.reserve(scan);
    for (uint32_t i = 0; i < scan; ++i) {
        int b = base2bit(s[i]);
        if (b < 0) {
            valid = 0;
            fwd = 0;
            rev = 0;
            kh.push_back(UINT64_MAX);
            continue;
        }
        fwd = ((fwd << 2) | (uint64_t)b) & mask;
        rev = (rev >> 2) | ((uint64_t)(3 - b) << (2 * (K - 1)));
        ++valid;
        if (valid >= K) {
            uint64_t canon = fwd < rev ? fwd : rev;
            kh.push_back(splitmix64(canon));
        } else {
            kh.push_back(UINT64_MAX);
        }
    }
    // Sliding-window minimum (window W) over the k-mer hashes; emit on change.
    uint64_t last = UINT64_MAX;
    if (kh.size() >= (size_t)W) {
        for (size_t i = 0; i + W <= kh.size(); ++i) {
            uint64_t m = UINT64_MAX;
            for (size_t j = i; j < i + W; ++j) {
                if (kh[j] < m) {
                    m = kh[j];
                }
            }
            if (m != UINT64_MAX && m != last) {
                out.push_back(m);
                last = m;
            }
        }
    } else {
        // Short read: use the single best k-mer hash as its minimizer.
        uint64_t m = UINT64_MAX;
        for (uint64_t h : kh) {
            if (h < m) {
                m = h;
            }
        }
        if (m != UINT64_MAX) {
            out.push_back(m);
        }
    }
    std::sort(out.begin(), out.end());
    out.erase(std::unique(out.begin(), out.end()), out.end());
    if (out.size() > kProbeMaxMinimizers) {
        out.resize(kProbeMaxMinimizers);
    }
}

// Sample up to `maxSamples` sequence lines spread across the chunk (stride
// sampling so we cover the whole chunk, not just its head).
static void collectSampleSeqs(
        const std::string& raw,
        size_t maxSamples,
        std::vector<std::pair<const char*, uint32_t>>& seqs)
{
    size_t nl = 0;
    for (char c : raw) {
        if (c == '\n') {
            ++nl;
        }
    }
    size_t nRec = nl / 4;
    if (nRec == 0) {
        nRec = 1;
    }
    size_t stride = std::max<size_t>(1, nRec / std::max<size_t>(1, maxSamples));
    const char* base = raw.data();
    size_t n = raw.size(), pos = 0, idx = 0;
    while (pos < n && seqs.size() < maxSamples) {
        size_t l0 = raw.find('\n', pos);
        if (l0 == std::string::npos) break;
        size_t s1 = l0 + 1;
        size_t l1 = raw.find('\n', s1);
        if (l1 == std::string::npos) break;
        size_t s2 = l1 + 1;
        size_t l2 = raw.find('\n', s2);
        if (l2 == std::string::npos) break;
        size_t s3 = l2 + 1;
        size_t l3 = raw.find('\n', s3);
        if (l3 == std::string::npos) l3 = n;
        if (idx % stride == 0) {
            uint32_t len = (uint32_t)(l1 - s1);
            if (len > 0 && base[s1 + len - 1] == '\r') --len;
            seqs.push_back({ base + s1, len });
        }
        ++idx;
        pos = (l3 < n) ? l3 + 1 : n;
    }
}

// Model slots. Illumina LZ has a fixed and a variable sub-model; the other
// three quadrants of {Illumina,Nanopore} x {LZ,zstd} are one model each.
enum {
    MODEL_ILL_FIXED_LZ = 0,
    MODEL_ILL_VAR_LZ   = 1,
    MODEL_ILL_ZSTD     = 2,
    MODEL_NANO_ZSTD    = 3,
    MODEL_NANO_LZ      = 4,
    MODEL_COUNT        = 5,
};
static const char* const kModelFile[MODEL_COUNT] = {
    "/fastq_fixed.zc",
    "/fastq_var.zc",
    "/fastq_illumina_zstdseq.zc",
    "/fastq_nano_zstdseq.zc",
    "/fastq_nano_lz.zc",
};
static const char* const kModelName[MODEL_COUNT] = {
    "illumina_fixed+LZ", "illumina_var+LZ", "illumina+zstd",
    "nano+zstd", "nano+LZ",
};

struct ChunkPick {
    uint8_t model;      // MODEL_*
    uint8_t routeCode;  // 0 = zstd SEQ, 1 = bigWindowLz SEQ
    SeqRoute route;
    double frac;        // frac_shared (fraction of reads sharing any minimizer)
    double dupRate;     // length-invariant: repeat density of pooled minimizers
    double meanLen;     // mean sampled read length (bp)
    bool isNano;
    bool fixedLen;
};

// Decide the model + SEQ route for one raw chunk from its read sample.
static ChunkPick pickForChunk(const std::string& raw)
{
    std::vector<std::pair<const char*, uint32_t>> seqs;
    collectSampleSeqs(raw, kProbeSampleReads, seqs);

    double sum = 0.0;
    uint32_t first = seqs.empty() ? 0 : seqs[0].second;
    bool allEqual = true;
    for (auto& p : seqs) {
        sum += p.second;
        if (p.second != first) {
            allEqual = false;
        }
    }
    double meanLen = seqs.empty() ? 0.0 : sum / (double)seqs.size();

    std::vector<std::vector<uint64_t>> perRead;
    perRead.reserve(seqs.size());
    std::vector<uint64_t> tmp;
    for (auto& p : seqs) {
        readMinimizers(p.first, p.second, tmp);
        perRead.push_back(tmp);
    }
    // frac_shared: count reads that share a minimizer with any other read.
    std::unordered_map<uint64_t, uint32_t> gc;
    gc.reserve(perRead.size() * 8);
    for (auto& v : perRead) {
        for (uint64_t m : v) {
            ++gc[m];
        }
    }
    size_t shared = 0;
    for (auto& v : perRead) {
        for (uint64_t m : v) {
            if (gc[m] >= 2) {
                ++shared;
                break;
            }
        }
    }
    double frac = perRead.empty() ? 0.0 : (double)shared / (double)perRead.size();

    // Length-invariant redundancy: fraction of pooled minimizer OCCURRENCES that
    // are repeats. Unlike frac_shared (which saturates to ~1 for long reads that
    // each carry many minimizers), this measures repetition density and is
    // comparable across short- and long-read data.
    size_t totalOcc = 0;
    for (auto& kv : gc) {
        totalOcc += kv.second;
    }
    double dupRate = totalOcc ? (double)(totalOcc - gc.size()) / (double)totalOcc
                              : 0.0;

    ChunkPick pk{};
    pk.frac     = frac;
    pk.dupRate  = dupRate;
    pk.meanLen  = meanLen;
    pk.isNano   = meanLen > kNanoMeanLenThreshold;
    pk.fixedLen = allEqual;
    bool useLz  = frac > kFracSharedThreshold;
    pk.routeCode = useLz ? 1 : 0;
    if (pk.isNano) {
        pk.model = useLz ? MODEL_NANO_LZ : MODEL_NANO_ZSTD;
        pk.route = useLz ? SeqRoute::BigLz : SeqRoute::Zstd;
    } else if (useLz) {
        pk.model = allEqual ? MODEL_ILL_FIXED_LZ : MODEL_ILL_VAR_LZ;
        pk.route = SeqRoute::BigLz;
    } else {
        pk.model = MODEL_ILL_ZSTD;
        pk.route = SeqRoute::Zstd;
    }
    return pk;
}

static const char kArchiveMagic2[8] = { 'N', 'Y', 'X', 'Z', 'C', 'H', 'K', '2' };

// Directory-of-models compress path: per-chunk probe + model/route selection.
static int cmdCompressPicker(
        const std::string& modelDir,
        const std::string& in,
        const std::string& out,
        unsigned threads,
        unsigned memBudgetMB)
{
    namespace fs = std::filesystem;
    // Load whichever models are present; a chunk that needs a missing model
    // falls back (see below), but at minimum we need one Illumina and one nano
    // model or we cannot route sensibly.
    std::vector<std::string> serialized(MODEL_COUNT);
    std::vector<bool> have(MODEL_COUNT, false);
    for (int m = 0; m < MODEL_COUNT; ++m) {
        std::string path = modelDir + kModelFile[m];
        if (fs::exists(path)) {
            serialized[m] = readAll(path);
            have[m]       = true;
        }
    }
    // Fallback chains so a pick always maps to a loaded model.
    auto resolveModel = [&](uint8_t m) -> uint8_t {
        if (have[m]) return m;
        switch (m) {
            case MODEL_NANO_LZ:   if (have[MODEL_NANO_ZSTD]) return MODEL_NANO_ZSTD; break;
            case MODEL_NANO_ZSTD: if (have[MODEL_NANO_LZ]) return MODEL_NANO_LZ; break;
            case MODEL_ILL_FIXED_LZ: if (have[MODEL_ILL_VAR_LZ]) return MODEL_ILL_VAR_LZ;
                                     if (have[MODEL_ILL_ZSTD]) return MODEL_ILL_ZSTD; break;
            case MODEL_ILL_VAR_LZ: if (have[MODEL_ILL_FIXED_LZ]) return MODEL_ILL_FIXED_LZ;
                                   if (have[MODEL_ILL_ZSTD]) return MODEL_ILL_ZSTD; break;
            case MODEL_ILL_ZSTD:  if (have[MODEL_ILL_FIXED_LZ]) return MODEL_ILL_FIXED_LZ;
                                  if (have[MODEL_ILL_VAR_LZ]) return MODEL_ILL_VAR_LZ; break;
        }
        for (int i = 0; i < MODEL_COUNT; ++i) if (have[i]) return (uint8_t)i;
        throw std::runtime_error("no models found in " + modelDir);
    };

    if (threads < 1) threads = 1;
    if (memBudgetMB < 1) memBudgetMB = 1;
    uint64_t targetBytes = (uint64_t)memBudgetMB * 1024ull * 1024ull / threads;
    const uint64_t kMinChunk = 4ull * 1024 * 1024;
    if (targetBytes < kMinChunk) targetBytes = kMinChunk;

    auto ranges = scanChunkBoundaries(in, targetBytes);
    size_t nChunks = ranges.size();
    if (nChunks == 0) {
        std::string archive(kArchiveMagic2, sizeof(kArchiveMagic2));
        putLE32(archive, 0);
        putLE32(archive, threads);
        putLE64(archive, 0);
        writeAll(out, archive);
        return 0;
    }
    uint64_t origSize = ranges.back().first + ranges.back().second;
    unsigned nWorkers = (unsigned)std::min<size_t>(threads, nChunks);

    std::vector<std::string> frames(nChunks);
    std::vector<ChunkPick> picks(nChunks);
    std::atomic<size_t> nextIdx{ 0 };

    // Dry-run: probe every chunk and report the routing distribution WITHOUT
    // compressing (fast; used to validate the picker's decisions before paying
    // for compression). No archive is written.
    const char* drEnv = std::getenv("NYX_PICKER_DRYRUN");
    const bool dryRun = (drEnv && drEnv[0] == '1');

    auto worker = [&]() {
        std::unordered_map<uint8_t, std::unique_ptr<openzl::Compressor>> cache;
        std::ifstream f(in, std::ios::binary);
        if (!f) return;
        for (;;) {
            size_t i = nextIdx.fetch_add(1);
            if (i >= nChunks) break;
            std::string raw = readFileRange(f, ranges[i].first, ranges[i].second);
            if (sortReadsEnabled()) sortReadsInChunk(raw);
            ChunkPick pk = pickForChunk(raw);
            pk.model = resolveModel(pk.model);
            if (!dryRun) {
                auto it = cache.find(pk.model);
                if (it == cache.end()) {
                    it = cache.emplace(
                                     pk.model,
                                     createCompressorFromSerialized(
                                             serialized[pk.model], {}))
                                 .first;
                }
                g_seqRouteOverride = pk.route;
                frames[i]          = compressChunkBytes(*it->second, raw);
                g_seqRouteOverride = SeqRoute::Unset;
            }
            picks[i] = pk;
        }
    };
    std::vector<std::thread> pool;
    pool.reserve(nWorkers);
    for (unsigned t = 0; t < nWorkers; ++t) pool.emplace_back(worker);
    for (auto& th : pool) th.join();

    // Archive (NYXZCHK2): per-chunk [u8 model][u8 routeCode] table after header.
    std::string archive(kArchiveMagic2, sizeof(kArchiveMagic2));
    putLE32(archive, (uint32_t)nChunks);
    putLE32(archive, threads);
    putLE64(archive, origSize);
    for (size_t i = 0; i < nChunks; ++i) {
        archive.push_back((char)picks[i].model);
        archive.push_back((char)picks[i].routeCode);
    }
    for (const auto& fr : frames) putLE64(archive, fr.size());
    size_t payload = 0;
    for (const auto& fr : frames) payload += fr.size();
    archive.reserve(archive.size() + payload);
    for (auto& fr : frames) {
        archive.append(fr);
        fr.clear();
        fr.shrink_to_fit();
    }
    if (!dryRun) {
        writeAll(out, archive);
    }

    // Report the per-chunk routing distribution (acceptance test for the
    // picker). Per-chunk lines when NYX_PICKER_LOG=1; always a final summary.
    const char* plog = std::getenv("NYX_PICKER_LOG");
    bool verbose = (plog && plog[0] == '1') || dryRun;
    size_t nLz = 0, nZ = 0, nNano = 0, nIll = 0;
    size_t perModel[MODEL_COUNT] = { 0 };
    for (size_t i = 0; i < nChunks; ++i) {
        const ChunkPick& pk = picks[i];
        (pk.routeCode ? nLz : nZ)++;
        (pk.isNano ? nNano : nIll)++;
        perModel[pk.model]++;
        if (verbose) {
            std::fprintf(
                    stderr,
                    "chunk %5zu  %-8s meanLen=%8.1f  frac_shared=%.3f  dup_rate=%.3f  ->  %-4s  model=%s\n",
                    i, pk.isNano ? "nanopore" : "illumina", pk.meanLen,
                    pk.frac, pk.dupRate, pk.routeCode ? "LZ" : "zstd",
                    kModelName[pk.model]);
        }
    }
    std::fprintf(
            stderr,
            "[picker] %zu chunks: SEQ route LZ=%zu (%.1f%%) zstd=%zu (%.1f%%) | "
            "datatype nano=%zu illumina=%zu\n",
            nChunks, nLz, 100.0 * nLz / nChunks, nZ, 100.0 * nZ / nChunks,
            nNano, nIll);
    for (int m = 0; m < MODEL_COUNT; ++m) {
        if (perModel[m]) {
            std::fprintf(stderr, "[picker]   model %-18s : %zu chunks\n",
                         kModelName[m], perModel[m]);
        }
    }
    return 0;
}

// ---------------------------------------------------------------------------
// Global read clustering (decoupled architecture, NYXZCHK3)
// ---------------------------------------------------------------------------
// Reorder ALL reads by canonical minimizer up front, then compress the reordered
// stream with the normal parallel chunker. This decouples clustering SCOPE (whole
// file) from compression PARALLELISM (many small chunks): overlapping reads become
// adjacent so the fast small-window SEQ coder captures the redundancy, while chunks
// stay small enough to fill every thread. A global permutation restores exact
// original order on decode (order-preserving, lossless).

static const char kArchiveMagic3[8] = { 'N', 'Y', 'X', 'Z', 'C', 'H', 'K', '3' };

static std::string compressBlobFrame(const std::string& blob, int level = 19); // fwd decl

// Parse whole FASTQ into clean 4-line records, compute a canonical minimizer per
// record (parallel), and return records concatenated in minimizer-sorted order.
// Fills perm[clusteredPos] = original record index. Returns empty (caller falls
// back to no reorder) unless the input is clean 4-line FASTQ ending in newline.
//
// When autoGate is true, first estimate the NET benefit: strided-sample whole
// records and compare fast-zstd size in original vs minimizer-sorted order. This
// captures both the sequence GAIN (overlaps become adjacent) and the ID-scramble
// COST (reordering sequential IDs). Returns empty (skip clustering) unless the
// sorted sample is at least ~5% smaller, so we only reorder where it pays off
// (medium/high-coverage genomic) and skip metagenomic/low-coverage/local data.
static std::string globalClusterReorder(
        const std::string& fq, std::vector<uint32_t>& perm, unsigned threads,
        bool autoGate)
{
    perm.clear();
    const size_t n = fq.size();
    if (n == 0 || fq[n - 1] != '\n') {
        return {};
    }
    struct Rec {
        uint64_t off;
        uint32_t len;
        uint32_t seqOff;
        uint32_t seqLen;
    };
    std::vector<Rec> recs;
    recs.reserve(n / 200 + 16);
    size_t pos = 0;
    while (pos < n) {
        size_t recStart = pos;
        if (fq[pos] != '@') {
            return {};
        }
        size_t l1 = fq.find('\n', pos);
        if (l1 == std::string::npos) return {};
        size_t seqStart = l1 + 1;
        size_t l2 = fq.find('\n', seqStart);
        if (l2 == std::string::npos) return {};
        uint32_t seqLen  = (uint32_t)(l2 - seqStart);
        size_t plusStart = l2 + 1;
        if (plusStart >= n || fq[plusStart] != '+') return {};
        size_t l3 = fq.find('\n', plusStart);
        if (l3 == std::string::npos) return {};
        size_t l4 = fq.find('\n', l3 + 1);
        if (l4 == std::string::npos) return {};
        size_t recEnd = l4 + 1;
        if (recEnd - recStart > 0xFFFFFFFFull) return {};
        recs.push_back({ recStart, (uint32_t)(recEnd - recStart),
                         (uint32_t)(seqStart - recStart), seqLen });
        pos = recEnd;
    }
    const size_t N = recs.size();
    if (N < 2) return {};

    std::vector<uint32_t> key(N);
    unsigned kw = std::max(1u, std::min(threads, 16u));
    {
        std::atomic<size_t> next{ 0 };
        const size_t block = 4096;
        auto worker = [&]() {
            for (;;) {
                size_t lo = next.fetch_add(block);
                if (lo >= N) break;
                size_t hi = std::min(N, lo + block);
                for (size_t r = lo; r < hi; ++r) {
                    const char* s = fq.data() + recs[r].off + recs[r].seqOff;
                    key[r] = readMinimizer(s, recs[r].seqLen, kMinimizerK);
                }
            }
        };
        std::vector<std::thread> pool;
        for (unsigned t = 0; t < kw; ++t) pool.emplace_back(worker);
        for (auto& th : pool) th.join();
    }

    (void)autoGate; // benefit gate now lives in the compress path (clusterBenefit)

    std::vector<uint32_t> order(N);
    for (size_t r = 0; r < N; ++r) order[r] = (uint32_t)r;
    std::sort(order.begin(), order.end(), [&](uint32_t a, uint32_t b) {
        if (key[a] != key[b]) return key[a] < key[b];
        return a < b;
    });
    std::string out;
    out.reserve(n);
    perm.resize(N);
    for (size_t c = 0; c < N; ++c) {
        uint32_t r = order[c];
        out.append(fq.data() + recs[r].off, recs[r].len);
        perm[c] = r;
    }
    return out;
}

// Record-aligned chunk ranges over an in-memory buffer.
static std::vector<std::pair<uint64_t, uint64_t>> scanChunkBoundariesBuf(
        const std::string& buf, uint64_t targetBytes)
{
    std::vector<std::pair<uint64_t, uint64_t>> ranges;
    const uint64_t n = buf.size();
    if (n == 0) return ranges;
    if (targetBytes < 1) targetBytes = 1;
    uint64_t chunkStart = 0, lineCount = 0;
    const char* d   = buf.data();
    const char* nl  = d;
    const char* end = d + n;
    while ((nl = (const char*)memchr(nl, '\n', (size_t)(end - nl)))) {
        uint64_t nlPos = (uint64_t)(nl - d);
        ++lineCount;
        if ((lineCount & 3u) == 0) {
            uint64_t boundary = nlPos + 1;
            if (boundary - chunkStart >= targetBytes) {
                ranges.emplace_back(chunkStart, boundary - chunkStart);
                chunkStart = boundary;
            }
        }
        ++nl;
        if (nl >= end) break;
    }
    if (chunkStart < n) ranges.emplace_back(chunkStart, n - chunkStart);
    return ranges;
}

// Compress an arbitrary byte blob into a standalone OpenZL frame (zstd graph),
// decodable with the universal DCtx path.
static std::string compressBlobFrame(const std::string& blob, int level)
{
    openzl::Compressor comp;
    openzl::unwrap(
            ZL_Compressor_selectStartingGraphID(comp.get(), ZL_GRAPH_ZSTD),
            "select zstd graph for perm blob",
            comp.get());
    openzl::CCtx cctx;
    cctx.setParameter(openzl::CParam::FormatVersion, ZL_MAX_FORMAT_VERSION);
    cctx.setParameter(openzl::CParam::CompressionLevel, level);
    cctx.refCompressor(comp);
    std::string frame;
    frame.resize(openzl::compressBound(blob.size()));
    size_t cs = cctx.compressSerial(frame, blob);
    frame.resize(cs);
    return frame;
}

// Benefit gate: STRIDE-sample records across the block and compare zstd-19 size
// of (header + full sequence, quality dropped) in original vs minimizer-sorted
// order. Full seq exposes the sequence GAIN (overlaps become adjacent); the
// header carries the ID-scramble COST; dropping quality removes order-independent
// noise/bytes. zstd-19 is needed to model the ID cost accurately. Cluster when
// the sorted sample is below `factor` x original (factor slightly >1.0 because
// the zstd-on-raw-header proxy over-weights the ID cost vs the real tokenised
// coder). Verified: cluster SRR1770413/DRR016013/DRR058063; skip SRR8899104/
// SRR062634/variable-NovaSeq. Runs once per file (on the first block).
static bool clusterBenefit(
        const std::string& serialized, const std::string& fq, unsigned threads)
{
    (void)serialized;
    (void)threads;
    const size_t n = fq.size();
    if (n == 0 || fq[n - 1] != '\n') return false;
    struct Rec { uint64_t off; uint32_t soff; uint32_t slen; };
    std::vector<Rec> recs;
    recs.reserve(n / 200 + 16);
    size_t pos = 0;
    while (pos < n) {
        size_t rs = pos;
        if (fq[pos] != '@') break;
        size_t l1 = fq.find('\n', pos);
        if (l1 == std::string::npos) break;
        size_t ss = l1 + 1;
        size_t l2 = fq.find('\n', ss);
        if (l2 == std::string::npos) break;
        uint32_t sl = (uint32_t)(l2 - ss);
        size_t ps = l2 + 1;
        if (ps >= n || fq[ps] != '+') break;
        size_t l3 = fq.find('\n', ps);
        if (l3 == std::string::npos) break;
        size_t l4 = fq.find('\n', l3 + 1);
        if (l4 == std::string::npos) break;
        recs.push_back({ rs, (uint32_t)(ss - rs), sl });
        pos = l4 + 1;
    }
    size_t Nall = recs.size();
    if (Nall < 4000) return false;
    const size_t kBudget = 16u * 1024 * 1024;
    size_t avgHS = 0, cnt = std::min<size_t>(Nall, 1000);
    for (size_t r = 0; r < cnt; ++r) avgHS += recs[r].soff + recs[r].slen;
    avgHS         = std::max<size_t>(1, avgHS / std::max<size_t>(1, cnt));
    size_t target = std::min<size_t>(80000, std::max<size_t>(4000, kBudget / avgHS));
    size_t stride = std::max<size_t>(1, Nall / target);
    std::vector<uint32_t> samp;
    for (size_t r = 0; r < Nall; r += stride) samp.push_back((uint32_t)r);
    size_t N = samp.size();
    std::vector<uint32_t> key(N), order(N);
    for (size_t i = 0; i < N; ++i) {
        uint32_t r = samp[i];
        key[i]     = readMinimizer(
                fq.data() + recs[r].off + recs[r].soff, recs[r].slen, kMinimizerK);
        order[i] = (uint32_t)i;
    }
    auto append = [&](std::string& dst, uint32_t i) {
        uint32_t r = samp[i];
        dst.append(fq.data() + recs[r].off, recs[r].soff + recs[r].slen);
    };
    std::string origCat, sortCat;
    for (uint32_t i = 0; i < N; ++i) append(origCat, i);
    std::sort(order.begin(), order.end(), [&](uint32_t a, uint32_t b) {
        if (key[a] != key[b]) return key[a] < key[b];
        return a < b;
    });
    for (uint32_t i : order) append(sortCat, i);
    // The two trial compressions are independent -> run them in parallel.
    size_t a = 0, b = 0;
    std::thread tA([&]() { a = compressBlobFrame(origCat, 19).size(); });
    b = compressBlobFrame(sortCat, 19).size();
    tA.join();
    // CONSERVATIVE threshold: cluster only on a CLEAR win (sorted >=1% smaller).
    // A near-neutral sample (ratio ~1.0) is ambiguous: it can be a real cluster
    // win whose benefit only shows at full coverage (deep E.coli, gate 1.024) OR
    // a file clustering would HURT (variable NovaSeq metagenomic, gate 1.021,
    // whose model is not cluster-trained). We cannot tell those apart from a
    // sample, so we SKIP both -- a missed cluster gain is harmless, but a false
    // cluster slows AND worsens our NovaSeq flagship (8.42->6.48, 138->217s).
    // Clear wins still cluster (SRR1770413 0.973, MiSeq E.coli DRR058063 0.981).
    double factor = 0.99;
    if (const char* gf = std::getenv("NYX_CLUSTER_GATE")) {
        double v = std::atof(gf);
        if (v > 0.5 && v < 1.2) factor = v;
    }
    bool cluster = ((double)b < factor * (double)a);
    if (const char* lg = std::getenv("NYX_CLUSTER_LOG")) {
        if (lg[0] == '1') {
            std::fprintf(stderr,
                         "[cluster-gate] sample=%zu orig=%zu sorted=%zu "
                         "ratio=%.3f factor=%.2f -> %s\n",
                         N, a, b, (a ? (double)b / a : 1.0), factor,
                         cluster ? "CLUSTER" : "skip");
        }
    }
    return cluster;
}

// Global-cluster compress path (BLOCK-WISE, memory-bounded). Returns 0 on
// success, -1 to fall back to the normal path. Processes the file in
// memMB-sized record-aligned BLOCKS (the clustering scope), reordering each
// block and compressing it as many small sub-chunks in parallel. Raw-data
// memory stays ~2x block (not 2x file); a big block (e.g. memMB=2000) still has
// enormous clustering scope. Each block is self-contained (its own permutation),
// and blocks decode back-to-back in original order.
//
// NYXZCHK3 layout:
//   [magic][u32 nBlocks][u32 threads][u64 origSize]
//   [u64 blockOff * nBlocks]                (payload offset, from payloadBase)
//   per block payload:
//     [u8 clustered][u32 nRecords][u32 nSubFrames][u64 permFrameLen]
//     [permFrame bytes]                     (present iff clustered)
//     [u64 subFrameLen * nSubFrames][subFrame bytes ...]
static int cmdCompressGlobalCluster(
        const std::string& serialized,
        const std::string& in,
        const std::string& out,
        unsigned threads,
        unsigned memBudgetMB,
        bool autoGate)
{
    if (threads < 1) threads = 1;
    if (memBudgetMB < 1) memBudgetMB = 1;

    // Block = clustering scope (bounds raw-data memory). Sub-chunks = parallel
    // compression units within a block (~2 per thread).
    uint64_t blockBytes      = (uint64_t)memBudgetMB * 1024ull * 1024ull;
    const uint64_t kMinBlock = 32ull * 1024 * 1024;
    if (blockBytes < kMinBlock) blockBytes = kMinBlock;
    uint64_t subBytes      = blockBytes / (uint64_t)std::max(1u, threads);
    const uint64_t kMinSub = 4ull * 1024 * 1024;
    if (subBytes < kMinSub) subBytes = kMinSub;

    auto blocks = scanChunkBoundaries(in, blockBytes);
    if (blocks.empty()) return -1;
    uint64_t origSize = blocks.back().first + blocks.back().second;

    std::ifstream f(in, std::ios::binary);
    if (!f) return -1;

    std::vector<std::string> blockPayloads(blocks.size());

    for (size_t bi = 0; bi < blocks.size(); ++bi) {
        std::string blockData =
                readFileRange(f, blocks[bi].first, blocks[bi].second);
        // Run the benefit gate ONCE, on the first block, as the file-level
        // decision (avoids paying the trial per block on large files).
        if (autoGate && bi == 0) {
            if (!clusterBenefit(serialized, blockData, threads)) {
                return -1; // clustering not beneficial -> normal path
            }
        }
        std::vector<uint32_t> perm;
        std::string reordered =
                globalClusterReorder(blockData, perm, threads, /*autoGate=*/false);
        bool clustered = !reordered.empty();

        const std::string& body = clustered ? reordered : blockData;
        auto ranges             = scanChunkBoundariesBuf(body, subBytes);
        size_t nSub             = ranges.size();
        std::vector<std::string> frames(nSub);
        std::atomic<size_t> ni{ 0 };
        g_globalClusterActive.store(true);
        auto worker = [&]() {
            auto comp = createCompressorFromSerialized(serialized, {});
            for (;;) {
                size_t i = ni.fetch_add(1);
                if (i >= nSub) break;
                std::string raw = body.substr(ranges[i].first, ranges[i].second);
                frames[i]       = compressChunkBytes(*comp, raw);
            }
        };
        unsigned nW =
                (unsigned)std::min<size_t>(threads, std::max<size_t>(1, nSub));
        std::vector<std::thread> pool;
        for (unsigned t = 0; t < nW; ++t) pool.emplace_back(worker);
        for (auto& th : pool) th.join();
        g_globalClusterActive.store(false);

        std::string permFrame;
        uint32_t nRecords = 0;
        if (clustered) {
            nRecords = (uint32_t)perm.size();
            std::string pb;
            pb.reserve(perm.size() * 4);
            for (uint32_t v : perm) putLE32(pb, v);
            permFrame = compressBlobFrame(pb);
        }
        blockData.clear();
        blockData.shrink_to_fit();
        reordered.clear();
        reordered.shrink_to_fit();

        std::string& pay = blockPayloads[bi];
        pay.push_back((char)(clustered ? 1 : 0));
        putLE32(pay, nRecords);
        putLE32(pay, (uint32_t)nSub);
        putLE64(pay, permFrame.size());
        pay.append(permFrame);
        for (auto& fr : frames) putLE64(pay, fr.size());
        for (auto& fr : frames) {
            pay.append(fr);
            fr.clear();
            fr.shrink_to_fit();
        }
    }

    std::string archive(kArchiveMagic3, sizeof(kArchiveMagic3));
    putLE32(archive, (uint32_t)blocks.size());
    putLE32(archive, threads);
    putLE64(archive, origSize);
    uint64_t off = 0;
    for (size_t i = 0; i < blocks.size(); ++i) {
        putLE64(archive, off);
        off += blockPayloads[i].size();
    }
    size_t total = 0;
    for (auto& pblk : blockPayloads) total += pblk.size();
    archive.reserve(archive.size() + total);
    for (auto& pblk : blockPayloads) {
        archive.append(pblk);
        pblk.clear();
        pblk.shrink_to_fit();
    }
    writeAll(out, archive);
    return 0;
}

static int cmdCompress(
        const std::string& compressorPath,
        const std::string& in,
        const std::string& out,
        unsigned threads,
        unsigned memBudgetMB)
{
    if (in == "-") {
        // stdin is not seekable; fall back to a single in-memory compression.
        std::string resolved   = resolveCompressor(compressorPath, in);
        std::string serialized = readAll(resolved);
        auto compressor        = createCompressorFromSerialized(serialized, {});
        std::string stdinData = readAll(in);
        if (sortReadsEnabled()) sortReadsInChunk(stdinData);
        std::string frame = compressChunkBytes(*compressor, stdinData);
        // Wrap the single frame in the archive format for a uniform decoder.
        std::string archive(kArchiveMagic, sizeof(kArchiveMagic));
        putLE32(archive, 1);
        putLE32(archive, 1);
        putLE64(archive, 0);
        putLE64(archive, frame.size());
        archive.append(frame);
        writeAll(out, archive);
        return 0;
    }

    // A directory of models triggers the per-chunk picker; a single .zc file
    // keeps the original whole-file single-model behaviour.
    if (std::filesystem::is_directory(compressorPath)) {
        return cmdCompressPicker(compressorPath, in, out, threads, memBudgetMB);
    }

    std::string resolved   = resolveCompressor(compressorPath, in);
    std::string serialized = readAll(resolved);

    if (threads < 1) {
        threads = 1;
    }
    if (memBudgetMB < 1) {
        memBudgetMB = 1;
    }

    // Global read clustering (NYXZCHK3): reorder all reads by minimizer, then
    // compress the reordered stream chunk-parallel with a global permutation.
    //   NYX_CLUSTER=1     -> force clustering (no benefit gate)
    //   NYX_CLUSTER=auto  -> cluster only if a cheap benefit probe says it helps
    //   NYX_CLUSTER=0/unset -> no clustering (default; unchanged behavior)
    if (const char* cl = std::getenv("NYX_CLUSTER")) {
        bool force = (cl[0] == '1');
        bool autoM = (std::strcmp(cl, "auto") == 0);
        if (force || autoM) {
            int r = cmdCompressGlobalCluster(
                    serialized, in, out, threads, memBudgetMB, /*autoGate=*/autoM);
            if (r >= 0) {
                return r;
            }
            // r < 0: not clean FASTQ or gate skipped -> normal path below.
        }
    }
    // Target input bytes per chunk = budget / threads, clamped to a sane range
    // so we neither fragment tiny chunks nor blow the budget.
    uint64_t targetBytes = (uint64_t)memBudgetMB * 1024ull * 1024ull / threads;
    const uint64_t kMinChunk = 4ull * 1024 * 1024;
    if (targetBytes < kMinChunk) {
        targetBytes = kMinChunk;
    }

    auto ranges = scanChunkBoundaries(in, targetBytes);
    size_t nChunks = ranges.size();
    if (nChunks == 0) {
        // Empty input: emit an empty archive.
        std::string archive(kArchiveMagic, sizeof(kArchiveMagic));
        putLE32(archive, 0);
        putLE32(archive, threads);
        putLE64(archive, 0);
        writeAll(out, archive);
        return 0;
    }

    uint64_t origSize = ranges.back().first + ranges.back().second;
    unsigned nWorkers = (unsigned)std::min<size_t>(threads, nChunks);

    std::vector<std::string> frames(nChunks);
    std::atomic<size_t> nextIdx{ 0 };

    auto worker = [&]() {
        // Each worker owns its compressor instance and file handle.
        auto compressor = createCompressorFromSerialized(serialized, {});
        std::ifstream f(in, std::ios::binary);
        if (!f) {
            return;
        }
        for (;;) {
            size_t i = nextIdx.fetch_add(1);
            if (i >= nChunks) {
                break;
            }
            std::string raw = readFileRange(f, ranges[i].first, ranges[i].second);
            if (sortReadsEnabled()) sortReadsInChunk(raw);
            frames[i] = compressChunkBytes(*compressor, raw);
        }
    };

    std::vector<std::thread> pool;
    pool.reserve(nWorkers);
    for (unsigned t = 0; t < nWorkers; ++t) {
        pool.emplace_back(worker);
    }
    for (auto& th : pool) {
        th.join();
    }

    // Assemble the archive.
    std::string archive(kArchiveMagic, sizeof(kArchiveMagic));
    putLE32(archive, (uint32_t)nChunks);
    putLE32(archive, threads);
    putLE64(archive, origSize);
    for (const auto& fr : frames) {
        putLE64(archive, fr.size());
    }
    // Preallocate for the payload to avoid repeated reallocation.
    size_t payload = 0;
    for (const auto& fr : frames) {
        payload += fr.size();
    }
    archive.reserve(archive.size() + payload);
    for (auto& fr : frames) {
        archive.append(fr);
        fr.clear();
        fr.shrink_to_fit();
    }
    writeAll(out, archive);
    return 0;
}

// Decode a NYXZCHK3 (globally-clustered) archive: decode the reordered stream and
// the permutation, then scatter each record back to its original position.
static int decompressGlobalCluster(const std::string& blob, const std::string& out)
{
    const uint8_t* p = (const uint8_t*)blob.data();
    size_t n         = blob.size();
    size_t pos       = sizeof(kArchiveMagic3);
    if (pos + 4 + 4 + 8 > n) {
        throw std::runtime_error("corrupt v3 header");
    }
    uint32_t nBlocks = readLE32(p + pos);
    pos += 4;
    /* threads */ readLE32(p + pos);
    pos += 4;
    /* origSize */ readLE64(p + pos);
    pos += 8;
    if (pos + (size_t)nBlocks * 8 > n) {
        throw std::runtime_error("corrupt v3 block table");
    }
    std::vector<uint64_t> blockOff(nBlocks);
    for (uint32_t i = 0; i < nBlocks; ++i) {
        blockOff[i] = readLE64(p + pos);
        pos += 8;
    }
    uint64_t payloadBase = pos;

    std::ofstream fout;
    std::ostream* os = &std::cout;
    if (out != "-") {
        fout.open(out, std::ios::binary);
        if (!fout) {
            throw std::runtime_error("cannot open output: " + out);
        }
        os = &fout;
    }
    unsigned hw = std::max(1u, std::thread::hardware_concurrency());

    // Blocks are processed in order (bounded memory); sub-frames within a block
    // decode in parallel.
    for (uint32_t bi = 0; bi < nBlocks; ++bi) {
        uint64_t bstart = payloadBase + blockOff[bi];
        if (bstart + 17 > n) {
            throw std::runtime_error("corrupt v3 block header");
        }
        const uint8_t* bp     = p + bstart;
        uint8_t clustered     = bp[0];
        uint32_t nRecords     = readLE32(bp + 1);
        uint32_t nSub         = readLE32(bp + 5);
        uint64_t permFrameLen = readLE64(bp + 9);
        uint64_t cur          = bstart + 17;
        if (cur + permFrameLen + (size_t)nSub * 8 > n) {
            throw std::runtime_error("corrupt v3 block payload");
        }
        const uint8_t* permFrameP = p + cur;
        cur += permFrameLen;
        std::vector<uint64_t> subLen(nSub), subOff(nSub);
        for (uint32_t i = 0; i < nSub; ++i) {
            subLen[i] = readLE64(p + cur);
            cur += 8;
        }
        for (uint32_t i = 0; i < nSub; ++i) {
            subOff[i] = cur;
            cur += subLen[i];
        }
        if (cur > n) {
            throw std::runtime_error("corrupt v3 sub-frames");
        }

        std::vector<std::string> parts(nSub);
        unsigned nW =
                (unsigned)std::min<size_t>(hw, std::max<uint32_t>(1u, nSub));
        std::atomic<uint32_t> nextIdx{ 0 };
        auto worker = [&]() {
            for (;;) {
                uint32_t i = nextIdx.fetch_add(1);
                if (i >= nSub) break;
                openzl::DCtx dctx;
                std::string frame((const char*)(p + subOff[i]), subLen[i]);
                std::string container = dctx.decompressSerial(frame);
                parts[i]              = unpackContainer(container);
            }
        };
        {
            std::vector<std::thread> pool;
            for (unsigned t = 0; t < nW; ++t) pool.emplace_back(worker);
            for (auto& th : pool) th.join();
        }
        std::string body;
        {
            size_t tot = 0;
            for (auto& pt : parts) tot += pt.size();
            body.reserve(tot);
            for (auto& pt : parts) {
                body.append(pt);
                pt.clear();
                pt.shrink_to_fit();
            }
        }

        if (!clustered) {
            os->write(body.data(), (std::streamsize)body.size());
            continue;
        }

        // Reordered block: decode perm, split into records, scatter to original.
        openzl::DCtx dctx;
        std::string permFrame((const char*)permFrameP, permFrameLen);
        std::string permBytes = dctx.decompressSerial(permFrame);
        if (permBytes.size() != (size_t)nRecords * 4) {
            throw std::runtime_error("v3 perm size mismatch");
        }
        std::vector<std::pair<uint64_t, uint32_t>> recIdx;
        recIdx.reserve(nRecords);
        {
            const char* d = body.data();
            size_t N      = body.size();
            size_t start  = 0;
            uint64_t lc   = 0;
            for (size_t i = 0; i < N; ++i) {
                if (d[i] == '\n') {
                    ++lc;
                    if ((lc & 3u) == 0) {
                        recIdx.emplace_back(start, (uint32_t)(i + 1 - start));
                        start = i + 1;
                    }
                }
            }
        }
        if (recIdx.size() != (size_t)nRecords) {
            throw std::runtime_error("v3 record count mismatch");
        }
        std::vector<std::pair<uint64_t, uint32_t>> byOrig(nRecords);
        for (uint32_t c = 0; c < nRecords; ++c) {
            uint32_t o = readLE32((const uint8_t*)permBytes.data() + c * 4);
            if (o >= nRecords) {
                throw std::runtime_error("v3 permutation out of range");
            }
            byOrig[o] = recIdx[c];
        }
        const char* d = body.data();
        for (uint32_t i = 0; i < nRecords; ++i) {
            os->write(d + byOrig[i].first, (std::streamsize)byOrig[i].second);
        }
    }
    return 0;
}

static int cmdDecompress(const std::string& in, const std::string& out)
{
    std::string blob = readAll(in);

    // Archive magic: v1 (NYXZCHK1) or v2 (NYXZCHK2, adds a per-chunk picker
    // route table). Neither is needed to DECODE (each frame is a self-describing
    // OpenZL frame) — v2's table is informational and skipped here.
    bool isV1 = blob.size() >= sizeof(kArchiveMagic)
            && std::memcmp(blob.data(), kArchiveMagic, sizeof(kArchiveMagic)) == 0;
    bool isV2 = blob.size() >= sizeof(kArchiveMagic2)
            && std::memcmp(blob.data(), kArchiveMagic2, sizeof(kArchiveMagic2)) == 0;
    bool isV3 = blob.size() >= sizeof(kArchiveMagic3)
            && std::memcmp(blob.data(), kArchiveMagic3, sizeof(kArchiveMagic3)) == 0;

    if (isV3) {
        return decompressGlobalCluster(blob, out);
    }

    // Legacy single-frame .nyxz (no archive magic): decode with universal path.
    if (!isV1 && !isV2) {
        openzl::DCtx dctx;
        std::string container = dctx.decompressSerial(blob);
        writeAll(out, unpackContainer(container));
        return 0;
    }

    const uint8_t* p = (const uint8_t*)blob.data();
    size_t n         = blob.size();
    size_t pos       = sizeof(kArchiveMagic);
    if (pos + 16 > n) {
        throw std::runtime_error("corrupt archive header");
    }
    uint32_t nChunks = readLE32(p + pos);
    pos += 4;
    /* threadsUsed */ readLE32(p + pos);
    pos += 4;
    /* origSize */ readLE64(p + pos);
    pos += 8;

    if (nChunks == 0) {
        writeAll(out, std::string());
        return 0;
    }

    // v2: skip the per-chunk [u8 model][u8 routeCode] table.
    if (isV2) {
        if (pos + (size_t)nChunks * 2 > n) {
            throw std::runtime_error("corrupt archive picker table");
        }
        pos += (size_t)nChunks * 2;
    }

    std::vector<uint64_t> frameLen(nChunks);
    if (pos + (size_t)nChunks * 8 > n) {
        throw std::runtime_error("corrupt archive frame table");
    }
    for (uint32_t i = 0; i < nChunks; ++i) {
        frameLen[i] = readLE64(p + pos);
        pos += 8;
    }
    std::vector<uint64_t> frameOff(nChunks);
    uint64_t off = pos;
    for (uint32_t i = 0; i < nChunks; ++i) {
        frameOff[i] = off;
        off += frameLen[i];
    }
    if (off > n) {
        throw std::runtime_error("corrupt archive payload");
    }

    // Decode chunks in parallel, but write output in order. Process in waves of
    // `nWorkers` so peak memory stays bounded.
    unsigned hw = std::max(1u, std::thread::hardware_concurrency());
    unsigned nWorkers = (unsigned)std::min<size_t>(hw, nChunks);

    std::ofstream fout;
    std::ostream* os = &std::cout;
    if (out != "-") {
        fout.open(out, std::ios::binary);
        if (!fout) {
            throw std::runtime_error("cannot open output: " + out);
        }
        os = &fout;
    }

    for (uint32_t base = 0; base < nChunks; base += nWorkers) {
        uint32_t hi = std::min<uint32_t>(base + nWorkers, nChunks);
        std::vector<std::string> outParts(hi - base);
        std::atomic<uint32_t> nextIdx{ base };
        auto worker = [&]() {
            for (;;) {
                uint32_t i = nextIdx.fetch_add(1);
                if (i >= hi) {
                    break;
                }
                openzl::DCtx dctx;
                std::string frame(blob.data() + frameOff[i], frameLen[i]);
                std::string container = dctx.decompressSerial(frame);
                outParts[i - base] = unpackContainer(container);
            }
        };
        std::vector<std::thread> pool;
        for (unsigned t = 0; t < nWorkers; ++t) {
            pool.emplace_back(worker);
        }
        for (auto& th : pool) {
            th.join();
        }
        for (auto& part : outParts) {
            os->write(part.data(), (std::streamsize)part.size());
        }
    }
    return 0;
}

// Debug helper: dump a trained compressor's serialized-CBOR structure as JSON.
// Uses Compressor::convertSerializedToJson, which decodes the raw serialized
// bytes structurally and does not require our custom function graph to be
// registered first.
static int cmdInspect(const std::string& zcPath)
{
    std::string serialized = readAll(zcPath);
    std::string json = openzl::Compressor::convertSerializedToJson(serialized);
    std::cout << json << std::endl;
    return 0;
}

static void usage()
{
    std::cerr
            << "nyxfqz - FASTQ compressor on OpenZL\n"
               "  nyxfqz pack       <in.fastq|-> <out.fqzc>\n"
               "  nyxfqz unpack     <in.fqzc>    <out.fastq|->\n"
               "  nyxfqz train      <sampleDir>  <out.zc>\n"
               "  nyxfqz compress   <trained.zc|compressors/> <in.fastq|-> <out.nyxz> [threads] [memMB]\n"
               "  nyxfqz decompress <in.nyxz>    <out.fastq|->\n"
               "  nyxfqz inspect    <trained.zc>                 (debug: dump JSON graph)\n"
               "\n"
               "  If a directory is given for compress, a per-chunk picker probes\n"
               "  each chunk's sequence redundancy (hashed minimizers) and data type\n"
               "  and selects one of the trained models (Illumina/Nanopore x LZ/zstd\n"
               "  SEQ route). Set NYX_PICKER_LOG=1 to log the per-chunk decisions.\n"
               "\n"
               "  compress splits the input into record-aligned chunks of about\n"
               "  memMB/threads MB each and compresses them in parallel, keeping\n"
               "  peak memory bounded. Defaults: threads=cores, memMB=400.\n";
}

} // namespace nyx

int main(int argc, char** argv)
{
    try {
        if (argc < 2) {
            nyx::usage();
            return 2;
        }
        std::string cmd = argv[1];
        if (cmd == "pack" && argc == 4) {
            return nyx::cmdPack(argv[2], argv[3]);
        }
        if (cmd == "unpack" && argc == 4) {
            return nyx::cmdUnpack(argv[2], argv[3]);
        }
        if (cmd == "train" && argc == 4) {
            return nyx::cmdTrain(argv[2], argv[3]);
        }
        if (cmd == "compress" && argc >= 5 && argc <= 7) {
            unsigned hw = std::max(1u, std::thread::hardware_concurrency());
            unsigned threads = (argc >= 6) ? (unsigned)std::max(1, atoi(argv[5])) : hw;
            unsigned memMB   = (argc >= 7) ? (unsigned)std::max(1, atoi(argv[6])) : 400u;
            return nyx::cmdCompress(argv[2], argv[3], argv[4], threads, memMB);
        }
        if (cmd == "decompress" && argc == 4) {
            return nyx::cmdDecompress(argv[2], argv[3]);
        }
        if (cmd == "inspect" && argc == 3) {
            return nyx::cmdInspect(argv[2]);
        }
        nyx::usage();
        return 2;
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << std::endl;
        return 1;
    }
}
