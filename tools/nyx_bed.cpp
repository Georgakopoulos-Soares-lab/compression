// nyx_bed — byte-exact, column-aware compression for BED and BED-like tables.
//
//   nyx_bed compress   [opts] <in.bed[.gz]> <out.nbed>
//   nyx_bed decompress [opts] <in.nbed>     <out.bed>
//   nyx_bed inspect    <in.nbed>
//
// Why this is not just "CSV through OpenZL"
// -----------------------------------------
// Splitting a table into one stream per column is the obvious move and, on its
// own, it is not reliably a win: measured on a ChromHMM segmentation it *loses*
// 3-5% against compressing the block whole, because it severs the cross-field
// matches a large-window LZ was finding. What pays is choosing an encoding per
// column, and in particular noticing that BED's columns are not independent:
//
//   - END is START plus a feature length, and lengths cluster hard.
//   - thickStart/thickEnd repeat START/END whenever a feature has no thick
//     region -- which is every ChromHMM segment and most peaks.
//   - itemRgb is a function of the feature name.
//
// So each column is encoded either on its own terms (dictionary, delta,
// fixed-point decimal, comma-separated integer list) or as a reference to a
// column on its left (copy, same-row delta, functional map). References point
// left only: that is what keeps the reference graph acyclic and lets the
// decoder rebuild columns in one left-to-right pass.
//
// Column width is not assumed
// ---------------------------
// The leading six columns of BED (chrom/start/end/name/score/strand) are the
// same in every dialect, but column 7 onward is dialect-specific -- it is
// thickStart in BED12 and signalValue in narrowPeak. This encoder therefore
// gives columns 1-6 named stream classes and treats everything past column 6
// uniformly, sniffing each column's type independently. Any width is accepted,
// including the 10 of narrowPeak and the 15 of gappedPeak.
//
// Every encoding is chosen by measurement
// ---------------------------------------
// Candidate encodings are built for a sample of the block's rows, compressed,
// and the smallest kept -- never selected from a guess about what the column
// "is". This codebase has got that wrong five times (VCF trained models, VCF
// annotation split, FASTQ model picker, FASTQ entropy level, and column
// splitting here), always in the same direction.
//
// Exactness
// ---------
// Every block is decoded again in-process and compared to the source bytes
// before it is accepted. A block that does not reproduce exactly is stored as a
// single verbatim stream, so the transform is exact on any input -- including
// files that are not tables at all. Rows whose field count differs from the
// block's modal width are escaped individually rather than dropping the whole
// file to a generic codec.

#include <algorithm>
#include <atomic>
#include <functional>
#include <memory>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include <sys/stat.h>

#include "nyx_stream.h"

using namespace nyx;

namespace {

constexpr char kMagic[8] = {'N','Y','X','B','E','D','\0','1'};

// Columns beyond this are still compressed, just not cross-referenced: the
// reference search is quadratic in the width and stops paying long before here.
constexpr uint32_t kMaxRefWidth = 64;

// ---------------------------------------------------------- stream classes

// Position-aware for the six universal BED columns, suffix-based beyond them.
// A class must never name a dialect, or a file whose column 7 the trainer never
// saw would have no model to try.
const char* bedClass(const std::string& n, uint8_t) {
    if (n == "@dir") return "dir";
    if (n == "@esc") return "esc";
    if (n == "@raw") return "raw";
    const size_t h = n.rfind('#');
    const std::string suf = (h == std::string::npos) ? std::string() : n.substr(h);
    if (suf == "#d" || suf == "#e" || suf == "#i" || suf == "#l" || suf == "#v") return "varint";
    if (suf == "#x") return "idx";
    if (suf == "#n" || suf == "#s") return "flag";
    if (suf == "#k" || suf == "#m") return "meta";
    // Raw text: the first three columns are worth their own classes because
    // chromosome names and coordinate text look nothing like a free-text label.
    if (n == "c0") return "chrom";
    if (n == "c1" || n == "c2") return "coord";
    return "text";
}

// ------------------------------------------------------- column encodings

enum ColEnc : uint8_t {
    CE_RAW     = 0,   // c{j}            NUL-separated text
    CE_NUM     = 1,   // c{j}#v          canonical non-negative int, varint
    CE_DELTA   = 2,   // c{j}#d          canonical int, zigzag varint vs previous row
    CE_DICT    = 3,   // c{j}#x,#k       dictionary index + key table
    CE_COPY    = 4,   // (ref)           byte-identical to column `ref`
    CE_XDELTA  = 5,   // c{j}#e          canonical int, zigzag varint vs column `ref`, same row
    CE_MAP     = 6,   // c{j}#m          a function of column `ref`
    CE_DECIMAL = 7,   // c{j}#s,#i       fixed-point decimal: per-row scale + scaled int
    CE_INTLIST = 8,   // c{j}#n,#l       comma-separated integer list
};

const char* encName(uint8_t e) {
    switch (e) {
        case CE_RAW: return "raw"; case CE_NUM: return "int";
        case CE_DELTA: return "delta"; case CE_DICT: return "dict";
        case CE_COPY: return "copy"; case CE_XDELTA: return "xdelta";
        case CE_MAP: return "map"; case CE_DECIMAL: return "decimal";
        case CE_INTLIST: return "intlist"; default: return "?";
    }
}

// Fixed-point decimal that round-trips to exactly the same text. The scale is
// the number of fraction digits, kept per value because a column may mix "1.5"
// and "1.50" and both must come back as written.
bool parse_decimal(const char* s, size_t n, int64_t& scaled, uint32_t& scale) {
    if (n == 0 || n > 20) return false;
    size_t i = 0;
    const bool neg = (s[0] == '-');
    if (neg) { i = 1; if (n == 1) return false; }
    size_t intStart = i;
    while (i < n && s[i] >= '0' && s[i] <= '9') i++;
    const size_t intLen = i - intStart;
    if (intLen == 0) return false;
    if (intLen > 1 && s[intStart] == '0') return false;      // leading zero
    size_t fracStart = 0, fracLen = 0;
    if (i < n) {
        if (s[i] != '.') return false;
        i++;
        fracStart = i;
        while (i < n && s[i] >= '0' && s[i] <= '9') i++;
        fracLen = i - fracStart;
        if (fracLen == 0 || i != n) return false;            // "1." or trailing junk
    }
    if (fracLen > 18) return false;
    uint64_t v = 0;
    for (size_t k = intStart; k < intStart + intLen; k++) {
        if (v > (UINT64_MAX - 9) / 10) return false;
        v = v * 10 + (uint64_t)(s[k] - '0');
    }
    for (size_t k = 0; k < fracLen; k++) {
        if (v > (UINT64_MAX - 9) / 10) return false;
        v = v * 10 + (uint64_t)(s[fracStart + k] - '0');
    }
    if (v > (uint64_t)INT64_MAX) return false;
    if (neg && v == 0) return false;                         // "-0" would not round-trip
    scaled = neg ? -(int64_t)v : (int64_t)v;
    scale  = (uint32_t)fracLen;
    return true;
}


// ------------------------------------------------------------ block parsing

struct Field { const char* p; uint32_t n; };

inline std::string_view view(const Field& f) { return std::string_view(f.p, f.n); }

// A block's rows as offsets into the block text. One flat array of field
// offsets rather than a vector<string> per column: a 64 MB block with 15
// columns holds ~15M fields, and a std::string apiece would cost more than the
// block itself.
struct Table {
    uint64_t nRows = 0;              // every line in the block, escapes included
    uint32_t W = 0;                  // modal field count
    std::vector<uint32_t> escIdx;    // line indices that do not have W fields
    std::vector<Field>    escTxt;    // and their text, without the terminator
    std::vector<Field>    cells;     // (nRows - escIdx.size()) * W fields
    bool endsWithNewline = true;

    size_t nData() const { return W ? cells.size() / W : 0; }
    const Field& at(size_t row, uint32_t col) const { return cells[row * W + col]; }
};

// Splits one line into fields. Returns the count; fills `out` up to `cap`.
uint32_t splitLine(const char* p, const char* e, Field* out, uint32_t cap) {
    uint32_t k = 0;
    const char* s = p;
    for (const char* q = p; q <= e; q++) {
        if (q == e || *q == '\t') {
            if (k < cap) { out[k].p = s; out[k].n = (uint32_t)(q - s); }
            k++;
            s = q + 1;
        }
    }
    return k;
}

bool parseTable(const char* text, size_t len, Table& t) {
    if (len == 0) return false;
    t.endsWithNewline = (text[len - 1] == '\n');

    // Modal width from a sample of the first lines. Using the first line alone
    // would let one stray comment decide the shape of the whole block.
    {
        std::unordered_map<uint32_t, uint32_t> hist;
        const char* p = text;
        const char* end = text + len;
        Field tmp[kMaxRefWidth + 1];
        for (int i = 0; i < 4096 && p < end; i++) {
            const char* nl = (const char*)std::memchr(p, '\n', (size_t)(end - p));
            const char* e = nl ? nl : end;
            uint32_t k = splitLine(p, e, tmp, kMaxRefWidth + 1);
            if (k >= 2) hist[k]++;
            if (!nl) break;
            p = nl + 1;
        }
        uint32_t best = 0, bestN = 0;
        for (const auto& kv : hist) if (kv.second > bestN) { bestN = kv.second; best = kv.first; }
        // A table this encoder can do nothing useful with.
        if (best < 2 || best > kMaxRefWidth) return false;
        t.W = best;
    }

    const char* p = text;
    const char* end = text + len;
    std::vector<Field> row(t.W);
    uint64_t line = 0;
    while (p < end) {
        const char* nl = (const char*)std::memchr(p, '\n', (size_t)(end - p));
        const char* e = nl ? nl : end;
        uint32_t k = splitLine(p, e, row.data(), t.W);
        if (k == t.W) {
            t.cells.insert(t.cells.end(), row.begin(), row.end());
        } else {
            t.escIdx.push_back((uint32_t)line);
            t.escTxt.push_back(Field{p, (uint32_t)(e - p)});
        }
        line++;
        if (!nl) break;
        p = nl + 1;
    }
    t.nRows = line;
    if (t.nData() == 0) return false;
    // Mostly-ragged input is not a table; let the verbatim path have it rather
    // than paying for an escape stream that is the whole block.
    if (t.escIdx.size() * 4 > t.nRows) return false;
    return true;
}

// ------------------------------------------------------- candidate building

// Per-column facts, computed once over every row so that an encoding is never
// chosen on the strength of a sample and then found invalid on row 900,000.
struct ColFacts {
    bool allU64 = true;       // canonical non-negative integers
    bool allI64 = true;       // canonical integers, possibly negative
    bool allDec = true;       // fixed-point decimals
    bool allList = true;      // comma-separated canonical integers
    uint64_t bytes = 0;       // total field bytes, used to prune copy candidates
    std::vector<int64_t>  ival;
    std::vector<int64_t>  dscaled;
    std::vector<uint32_t> dscale;
};

bool parseList(const Field& f, std::vector<uint64_t>& out, bool& trailingComma) {
    out.clear();
    if (f.n == 0) return false;
    trailingComma = (f.p[f.n - 1] == ',');
    const uint32_t lim = trailingComma ? f.n - 1 : f.n;
    if (lim == 0) return false;
    uint32_t s = 0;
    for (uint32_t i = 0; i <= lim; i++) {
        if (i == lim || f.p[i] == ',') {
            uint64_t v;
            if (!parse_canonical_u64(f.p + s, i - s, v)) return false;
            out.push_back(v);
            s = i + 1;
        }
    }
    return true;
}

void gatherFacts(const Table& t, uint32_t j, ColFacts& f) {
    const size_t n = t.nData();
    f.ival.reserve(n);
    f.dscaled.reserve(n);
    f.dscale.reserve(n);
    std::vector<uint64_t> tmp;
    bool tc;
    for (size_t i = 0; i < n; i++) {
        const Field& v = t.at(i, j);
        f.bytes += v.n;
        int64_t iv = 0;
        if (f.allI64 || f.allU64) {
            if (!parse_canonical_i64(v.p, v.n, iv)) { f.allI64 = false; f.allU64 = false; }
            else { if (iv < 0) f.allU64 = false; f.ival.push_back(iv); }
        }
        if (f.allDec) {
            int64_t sc; uint32_t s;
            if (!parse_decimal(v.p, v.n, sc, s)) f.allDec = false;
            else { f.dscaled.push_back(sc); f.dscale.push_back(s); }
        }
        if (f.allList && !parseList(v, tmp, tc)) f.allList = false;
    }
    if (!f.allI64) f.ival.clear();
    if (!f.allDec) { f.dscaled.clear(); f.dscale.clear(); }
}

// One candidate encoding: the streams it would produce, keyed by suffix.
struct Cand {
    uint8_t enc = CE_RAW;
    uint32_t ref = 0;
    std::vector<std::pair<std::string, std::string>> streams;   // suffix -> bytes
    size_t cost = SIZE_MAX;
};

// Builds a candidate's streams for rows [0, n). Called twice per chosen
// encoding: once on a sample to price it, once on the whole column to emit it.
void buildCand(const Table& t, uint32_t j, const ColFacts& f,
               const std::vector<ColFacts>* allFacts, Cand& c, size_t n) {
    c.streams.clear();
    switch (c.enc) {
        case CE_RAW: {
            std::string s;
            for (size_t i = 0; i < n; i++) {
                const Field& v = t.at(i, j);
                s.append(v.p, v.n);
                s += '\0';
            }
            c.streams.emplace_back("", std::move(s));
            break;
        }
        case CE_NUM: {
            std::string s;
            for (size_t i = 0; i < n; i++) put_varint(s, (uint64_t)f.ival[i]);
            c.streams.emplace_back("#v", std::move(s));
            break;
        }
        case CE_DELTA: {
            std::string s;
            int64_t prev = 0;
            for (size_t i = 0; i < n; i++) { put_varint(s, zigzag(f.ival[i] - prev)); prev = f.ival[i]; }
            c.streams.emplace_back("#d", std::move(s));
            break;
        }
        case CE_DICT: {
            std::string idx, keys;
            std::unordered_map<std::string_view, uint32_t> seen;
            for (size_t i = 0; i < n; i++) {
                const Field& v = t.at(i, j);
                auto it = seen.find(view(v));
                if (it == seen.end()) {
                    const uint32_t id = (uint32_t)seen.size();
                    seen.emplace(view(v), id);
                    keys.append(v.p, v.n);
                    keys += '\0';
                    put_varint(idx, id);
                } else {
                    put_varint(idx, it->second);
                }
            }
            c.streams.emplace_back("#x", std::move(idx));
            c.streams.emplace_back("#k", std::move(keys));
            break;
        }
        case CE_COPY:
            break;                                     // nothing to store
        case CE_XDELTA: {
            const ColFacts& r = (*allFacts)[c.ref];
            std::string s;
            for (size_t i = 0; i < n; i++) put_varint(s, zigzag(f.ival[i] - r.ival[i]));
            c.streams.emplace_back("#e", std::move(s));
            break;
        }
        case CE_MAP:
            // Built in chooseCol from the same pass that proves the dependency;
            // there is deliberately no second way to construct it.
            die("internal: CE_MAP is not built here");
        case CE_DECIMAL: {
            std::string sc, iv;
            for (size_t i = 0; i < n; i++) {
                put_varint(sc, f.dscale[i]);
                put_varint(iv, zigzag(f.dscaled[i]));
            }
            c.streams.emplace_back("#s", std::move(sc));
            c.streams.emplace_back("#i", std::move(iv));
            break;
        }
        case CE_INTLIST: {
            std::string cnt, vals;
            std::vector<uint64_t> tmp;
            bool tc = false;
            for (size_t i = 0; i < n; i++) {
                parseList(t.at(i, j), tmp, tc);
                put_varint(cnt, ((uint64_t)tmp.size() << 1) | (tc ? 1u : 0u));
                int64_t prev = 0;
                for (uint64_t v : tmp) { put_varint(vals, zigzag((int64_t)v - prev)); prev = (int64_t)v; }
            }
            c.streams.emplace_back("#n", std::move(cnt));
            c.streams.emplace_back("#l", std::move(vals));
            break;
        }
    }
}

size_t priceCand(Codec& cod, uint32_t j, Cand& c) {
    size_t tot = 0;
    const std::string base = "c" + std::to_string(j);
    for (auto& kv : c.streams) tot += cod.compressOne(kv.second, base + kv.first);
    return tot;
}

// Chooses the encoding for one column. Candidates are priced on a sample of
// rows, not the whole column, so the search cost stays a small fraction of the
// block; the winner is then rebuilt over every row.
Cand chooseCol(const Table& t, uint32_t j, const std::vector<ColFacts>& facts,
               Codec& cod) {
    const ColFacts& f = facts[j];
    const size_t n = t.nData();
    const size_t sample = std::min<size_t>(n, 16384);

    // An exact copy costs nothing, so there is no point pricing anything else.
    for (uint32_t k = 0; k < j; k++) {
        if (facts[k].bytes != f.bytes) continue;             // cheap reject
        bool same = true;
        for (size_t i = 0; i < n && same; i++) {
            const Field& a = t.at(i, j);
            const Field& b = t.at(i, k);
            same = (a.n == b.n) && std::memcmp(a.p, b.p, a.n) == 0;
        }
        if (same) {
            Cand c; c.enc = CE_COPY; c.ref = k; c.cost = 0;
            return c;
        }
    }

    std::vector<Cand> cands;
    std::vector<double> est;     // projected cost over the whole column
    auto add = [&](uint8_t enc, uint32_t ref = 0) {
        Cand c; c.enc = enc; c.ref = ref;
        // A map has no per-row stream at all -- its whole cost is the key table
        // -- so it must be built and priced over every row. Everything else is
        // priced on the sample and projected, or a map with many keys would
        // lose to a sampled rival that is in fact far more expensive in full.
        const bool full = (enc == CE_MAP);
        buildCand(t, j, f, &facts, c, full ? n : sample);
        c.cost = priceCand(cod, j, c);
        est.push_back(full ? (double)c.cost
                           : (double)c.cost * (double)n / (double)sample);
        cands.push_back(std::move(c));
    };

    add(CE_RAW);
    if (f.allU64) add(CE_NUM);
    if (f.allI64) add(CE_DELTA);
    if (f.allDec && !f.allI64) add(CE_DECIMAL);      // only when it buys something raw ints do not
    if (f.allList) add(CE_INTLIST);

    // Distinct-value count, capped: it decides both whether a dictionary is
    // worth building and whether a functional dependency is even possible. A
    // column with more distinct values than its candidate key cannot be a
    // function of it, so this prunes the quadratic reference search.
    size_t uniq = 0;
    {
        std::unordered_map<std::string_view, uint32_t> seen;
        const size_t cap = std::max<size_t>(n / 4 + 1, 8192);
        for (size_t i = 0; i < n && seen.size() <= cap; i++) {
            seen.emplace(view(t.at(i, j)), 0);
        }
        uniq = seen.size();
        if (uniq <= n / 4 + 1) add(CE_DICT);
    }

    // Cross-column references, left only: this is what makes the graph acyclic
    // and the decoder a single left-to-right pass.
    for (uint32_t k = 0; k < j && k < kMaxRefWidth; k++) {
        if (f.allI64 && facts[k].allI64) add(CE_XDELTA, k);
        if (uniq > 8192) continue;                   // cannot be a function of anything cheap
        // Functional dependency: does column k determine column j? The test
        // walks every row and so does building the map, so the map is emitted
        // from this same pass -- rebuilding it in buildCand doubled the cost of
        // the widest files.
        std::unordered_map<std::string_view, std::string_view> m;
        std::string ser;
        bool functional = true;
        for (size_t i = 0; i < n && functional; i++) {
            const std::string_view kv = view(t.at(i, k));
            const std::string_view vv = view(t.at(i, j));
            auto it = m.emplace(kv, vv);
            if (!it.second) { if (it.first->second != vv) functional = false; continue; }
            if (m.size() > 8192) { functional = false; break; }
            ser.append(kv); ser += '\t';
            ser.append(vv); ser += '\0';
        }
        if (functional) {
            Cand c; c.enc = CE_MAP; c.ref = k;
            c.streams.emplace_back("#m", std::move(ser));
            c.cost = priceCand(cod, j, c);
            est.push_back((double)c.cost);
            cands.push_back(std::move(c));
        }
    }

    // Sample-priced candidates are compared against a sample-priced raw, so the
    // comparison is like for like even though the absolute numbers are not the
    // full column's.
    size_t best = 0;
    for (size_t i = 1; i < cands.size(); i++)
        if (est[i] < est[best]) best = i;
    Cand win = std::move(cands[best]);
    if (win.enc != CE_MAP && win.enc != CE_COPY)
        buildCand(t, j, f, &facts, win, n);   // rebuild over every row
    return win;
}

// ------------------------------------------------------------------ encode

bool encodeBlock(const char* text, size_t len, Bundle& b, const std::vector<Codec*>& cods) {
    Table t;
    if (!parseTable(text, len, t)) return false;

    const size_t n = t.nData();
    std::vector<ColFacts> facts(t.W);
    parallelFor(t.W, cods, [&](size_t j, Codec&) { gatherFacts(t, (uint32_t)j, facts[j]); });

    // Columns are priced independently -- chooseCol reads the table and the
    // facts, never another column's decision -- so this parallelises without
    // changing a single choice, and the archive is the same at any thread count.
    std::vector<uint8_t> enc(t.W, CE_RAW), refOf(t.W, 0);
    std::vector<Cand> chosen(t.W);
    parallelFor(t.W, cods, [&](size_t j, Codec& c) {
        chosen[j] = chooseCol(t, (uint32_t)j, facts, c);
    });
    for (uint32_t j = 0; j < t.W; j++) {
        enc[j] = chosen[j].enc;
        refOf[j] = (uint8_t)chosen[j].ref;
    }

    std::string dir;
    put_varint(dir, t.nRows);
    put_varint(dir, t.W);
    dir += (char)(t.endsWithNewline ? 0 : 1);
    for (uint32_t j = 0; j < t.W; j++) {
        dir += (char)enc[j];
        if (enc[j] == CE_COPY || enc[j] == CE_XDELTA || enc[j] == CE_MAP)
            put_varint(dir, chosen[j].ref);
    }
    put_varint(dir, t.escIdx.size());
    {
        uint32_t prev = 0;
        for (size_t i = 0; i < t.escIdx.size(); i++) {
            put_varint(dir, (uint64_t)(t.escIdx[i] - prev));
            prev = t.escIdx[i];
        }
    }
    b.at("@dir") = dir;

    std::string esc;
    for (const Field& f : t.escTxt) {
        put_varint(esc, f.n);
        esc.append(f.p, f.n);
    }
    if (!esc.empty()) b.at("@esc") = esc;

    for (uint32_t j = 0; j < t.W; j++) {
        const std::string base = "c" + std::to_string(j);
        for (auto& kv : chosen[j].streams) b.at(base + kv.first) = std::move(kv.second);
    }
    (void)n;
    return true;
}

// ------------------------------------------------------------------ decode

// A decoded column: flat bytes plus one offset per row, for the same reason the
// encoder uses flat cells. Integer columns also keep their values, so a column
// that references them (same-row delta) reads the integer instead of parsing the
// text that was just formatted from it.
struct ColBuf {
    std::string data;
    std::vector<uint32_t> off;      // size nData + 1
    std::vector<int64_t> iv;        // filled for integer-valued encodings only
    const char* ptr(size_t i) const { return data.data() + off[i]; }
    uint32_t len(size_t i) const { return off[i + 1] - off[i]; }
    void push(const char* p, size_t n) { data.append(p, n); off.push_back((uint32_t)data.size()); }
    void start(size_t rows, size_t bytes) {
        off.clear(); off.reserve(rows + 1); off.push_back(0);
        data.clear(); data.reserve(bytes);
    }
};

// Integer to decimal text. The decoder formats every integer cell of a block,
// and snprintf made that the whole decompression cost: measured 0.29 of 0.33 s
// on a 53 MB broadPeak.
inline void appendInt(std::string& out, int64_t v) {
    char buf[24];
    char* e = buf + sizeof buf;
    char* q = e;
    uint64_t u = v < 0 ? (uint64_t)0 - (uint64_t)v : (uint64_t)v;
    do { *--q = char('0' + u % 10); u /= 10; } while (u);
    if (v < 0) *--q = '-';
    out.append(q, (size_t)(e - q));
}

inline void appendDecimal(std::string& out, int64_t scaled, uint32_t scale) {
    if (scale == 0) { appendInt(out, scaled); return; }
    const bool neg = scaled < 0;
    uint64_t u = neg ? (uint64_t)0 - (uint64_t)scaled : (uint64_t)scaled;
    char buf[48];
    char* e = buf + sizeof buf;
    char* q = e;
    for (uint32_t k = 0; k < scale; k++) { *--q = char('0' + u % 10); u /= 10; }
    *--q = '.';
    do { *--q = char('0' + u % 10); u /= 10; } while (u);
    if (neg) *--q = '-';
    out.append(q, (size_t)(e - q));
}

inline void runParallel(size_t n, unsigned threads, const std::function<void(size_t)>& fn) {
    const size_t k = std::min<size_t>(n, std::max(1u, threads));
    if (k <= 1) { for (size_t i = 0; i < n; i++) fn(i); return; }
    std::atomic<size_t> next{0};
    std::vector<std::thread> ts;
    for (size_t w = 0; w < k; w++)
        ts.emplace_back([&] { for (size_t i; (i = next++) < n;) fn(i); });
    for (auto& t : ts) t.join();
}

std::string decodeBlock(const std::vector<std::string>& streams,
                        const std::vector<std::string>& names, unsigned threads) {
    std::unordered_map<std::string, const std::string*> byName;
    for (size_t i = 0; i < names.size(); i++) byName.emplace(names[i], &streams[i]);
    auto need = [&](const std::string& n) -> const std::string& {
        auto it = byName.find(n);
        if (it == byName.end()) die("missing stream " + n);
        return *it->second;
    };
    auto maybe = [&](const std::string& n) -> const std::string* {
        auto it = byName.find(n);
        return it == byName.end() ? nullptr : it->second;
    };

    const std::string& dir = need("@dir");
    const char* dp = dir.data();
    const char* de = dir.data() + dir.size();
    const uint64_t nRows = get_varint(dp, de);
    const uint32_t W = (uint32_t)get_varint(dp, de);
    if (dp >= de) die("corrupt directory");
    const bool noTrailingNewline = (*dp++ != 0);
    std::vector<uint8_t> enc(W);
    std::vector<uint32_t> ref(W, 0);
    for (uint32_t j = 0; j < W; j++) {
        if (dp >= de) die("corrupt directory");
        enc[j] = (uint8_t)*dp++;
        if (enc[j] == CE_COPY || enc[j] == CE_XDELTA || enc[j] == CE_MAP) {
            ref[j] = (uint32_t)get_varint(dp, de);
            if (ref[j] >= j) die("corrupt directory: reference does not point left");
        }
    }
    const uint64_t nEsc = get_varint(dp, de);
    std::vector<uint32_t> escIdx(nEsc);
    {
        uint32_t prev = 0;
        for (uint64_t i = 0; i < nEsc; i++) { prev += (uint32_t)get_varint(dp, de); escIdx[i] = prev; }
    }
    const size_t nData = (size_t)(nRows - nEsc);

    std::vector<ColBuf> col(W);
    auto decodeCol = [&](uint32_t j) {
        const std::string base = "c" + std::to_string(j);
        ColBuf& c = col[j];
        switch (enc[j]) {
            case CE_RAW: {
                const std::string* s = maybe(base);
                c.start(nData, s ? s->size() : 0);
                const char* p = s ? s->data() : nullptr;
                const char* e = s ? p + s->size() : nullptr;
                for (size_t i = 0; i < nData; i++) {
                    const char* z = p ? (const char*)std::memchr(p, '\0', (size_t)(e - p)) : nullptr;
                    if (!z) die("truncated raw column");
                    c.push(p, (size_t)(z - p));
                    p = z + 1;
                }
                break;
            }
            case CE_NUM: case CE_DELTA: {
                const std::string& s = need(base + (enc[j] == CE_NUM ? "#v" : "#d"));
                c.start(nData, nData * 8);
                c.iv.resize(nData);
                const char* p = s.data();
                const char* e = p + s.size();
                int64_t prev = 0;
                for (size_t i = 0; i < nData; i++) {
                    int64_t v;
                    if (enc[j] == CE_NUM) v = (int64_t)get_varint(p, e);
                    else { v = prev + unzigzag(get_varint(p, e)); prev = v; }
                    c.iv[i] = v;
                    appendInt(c.data, v);
                    c.off.push_back((uint32_t)c.data.size());
                }
                break;
            }
            case CE_DICT: {
                const std::string& idx = need(base + "#x");
                const std::string& keys = need(base + "#k");
                std::vector<std::pair<const char*, uint32_t>> tab;
                {
                    const char* p = keys.data();
                    const char* e = p + keys.size();
                    while (p < e) {
                        const char* z = (const char*)std::memchr(p, '\0', (size_t)(e - p));
                        if (!z) break;
                        tab.emplace_back(p, (uint32_t)(z - p));
                        p = z + 1;
                    }
                }
                c.start(nData, nData * 4);
                const char* p = idx.data();
                const char* e = p + idx.size();
                for (size_t i = 0; i < nData; i++) {
                    const uint64_t id = get_varint(p, e);
                    if (id >= tab.size()) die("dictionary index out of range");
                    c.push(tab[id].first, tab[id].second);
                }
                break;
            }
            case CE_COPY: {
                const ColBuf& r = col[ref[j]];
                c.data = r.data;
                c.off = r.off;
                c.iv = r.iv;
                break;
            }
            case CE_XDELTA: {
                const std::string& s = need(base + "#e");
                const ColBuf& r = col[ref[j]];
                c.start(nData, nData * 8);
                c.iv.resize(nData);
                const char* p = s.data();
                const char* e = p + s.size();
                for (size_t i = 0; i < nData; i++) {
                    int64_t base_i = 0;
                    if (!r.iv.empty()) base_i = r.iv[i];
                    else if (!parse_canonical_i64(r.ptr(i), r.len(i), base_i))
                        die("xdelta base not an integer");
                    const int64_t v = base_i + unzigzag(get_varint(p, e));
                    c.iv[i] = v;
                    appendInt(c.data, v);
                    c.off.push_back((uint32_t)c.data.size());
                }
                break;
            }
            case CE_MAP: {
                const std::string& m = need(base + "#m");
                std::unordered_map<std::string_view, std::string_view> tab;
                {
                    const char* p = m.data();
                    const char* e = p + m.size();
                    while (p < e) {
                        const char* tc = (const char*)std::memchr(p, '\t', (size_t)(e - p));
                        if (!tc) break;
                        const char* z = (const char*)std::memchr(tc, '\0', (size_t)(e - tc));
                        if (!z) break;
                        tab.emplace(std::string_view(p, (size_t)(tc - p)),
                                    std::string_view(tc + 1, (size_t)(z - tc - 1)));
                        p = z + 1;
                    }
                }
                const ColBuf& r = col[ref[j]];
                c.start(nData, nData * 4);
                for (size_t i = 0; i < nData; i++) {
                    auto it = tab.find(std::string_view(r.ptr(i), r.len(i)));
                    if (it == tab.end()) die("map key not found");
                    c.push(it->second.data(), it->second.size());
                }
                break;
            }
            case CE_DECIMAL: {
                const std::string& ss = need(base + "#s");
                const std::string& is = need(base + "#i");
                const char* sp = ss.data(); const char* se = sp + ss.size();
                const char* ip = is.data(); const char* ie = ip + is.size();
                c.start(nData, nData * 8);
                for (size_t i = 0; i < nData; i++) {
                    const uint32_t sc = (uint32_t)get_varint(sp, se);
                    appendDecimal(c.data, unzigzag(get_varint(ip, ie)), sc);
                    c.off.push_back((uint32_t)c.data.size());
                }
                break;
            }
            case CE_INTLIST: {
                const std::string& cs = need(base + "#n");
                const std::string& vs = need(base + "#l");
                const char* cp = cs.data(); const char* ce = cp + cs.size();
                const char* vp = vs.data(); const char* ve = vp + vs.size();
                c.start(nData, nData * 16);
                for (size_t i = 0; i < nData; i++) {
                    const uint64_t hdr = get_varint(cp, ce);
                    const uint64_t k = hdr >> 1;
                    int64_t prev = 0;
                    for (uint64_t q = 0; q < k; q++) {
                        const int64_t v = prev + unzigzag(get_varint(vp, ve));
                        prev = v;
                        if (q) c.data += ',';
                        appendInt(c.data, v);
                    }
                    if (hdr & 1) c.data += ',';
                    c.off.push_back((uint32_t)c.data.size());
                }
                break;
            }
            default: die("unknown column encoding");
        }
    };

    // Columns reference only columns to their left, so they decode in waves:
    // every column whose reference is already decoded goes in the next wave, and
    // the columns of one wave are independent of each other.
    {
        std::vector<int> level(W, 0);
        int maxLevel = 0;
        for (uint32_t j = 0; j < W; j++) {
            if (enc[j] == CE_COPY || enc[j] == CE_XDELTA || enc[j] == CE_MAP)
                level[j] = level[ref[j]] + 1;
            maxLevel = std::max(maxLevel, level[j]);
        }
        for (int L = 0; L <= maxLevel; L++) {
            std::vector<uint32_t> wave;
            for (uint32_t j = 0; j < W; j++) if (level[j] == L) wave.push_back(j);
            runParallel(wave.size(), threads, [&](size_t k) { decodeCol(wave[k]); });
        }
    }

    // Escaped lines: random access into @esc so the emit can run in pieces.
    const std::string* escS = maybe("@esc");
    std::vector<std::pair<const char*, uint64_t>> escText(nEsc);
    {
        const char* ep = escS ? escS->data() : nullptr;
        const char* ee = escS ? ep + escS->size() : nullptr;
        for (uint64_t i = 0; i < nEsc; i++) {
            const uint64_t n = get_varint(ep, ee);
            escText[i] = {ep, n};
            ep += n;
        }
    }

    // Re-emit in line-range pieces, one per thread, then concatenate. A piece's
    // first data row is its first line minus the escapes before it.
    const size_t pieces = std::max<size_t>(1, std::min<size_t>(threads, nRows / 4096 + 1));
    std::vector<std::string> part(pieces);
    runParallel(pieces, threads, [&](size_t k) {
        const uint64_t a = nRows * k / pieces, b = nRows * (k + 1) / pieces;
        size_t e = (size_t)(std::lower_bound(escIdx.begin(), escIdx.end(), (uint32_t)a) - escIdx.begin());
        size_t row = (size_t)a - e;
        std::string& out = part[k];
        size_t approx = 0;
        for (uint32_t j = 0; j < W; j++)
            approx += col[j].data.size() / std::max<size_t>(1, pieces);
        out.reserve(approx + (b - a) * (W + 1));
        for (uint64_t line = a; line < b; line++) {
            if (e < escIdx.size() && escIdx[e] == line) {
                out.append(escText[e].first, escText[e].second);
                e++;
            } else {
                for (uint32_t j = 0; j < W; j++) {
                    out.append(col[j].ptr(row), col[j].len(row));
                    if (j + 1 < W) out += '\t';
                }
                row++;
            }
            if (line + 1 < nRows || !noTrailingNewline) out += '\n';
        }
    });
    if (pieces == 1) return std::move(part[0]);
    size_t total = 0;
    for (auto& q : part) total += q.size();
    std::string out;
    out.reserve(total);
    for (auto& q : part) out += q;
    return out;
}

// ----------------------------------------------------------- calibration

// Candidate entropy levels. OpenZL's default is 6 and it is passed to the zstd
// backend, but it is also read by the graph *selectors*, so raising it changes
// which codecs run and not merely how hard they try. Measured consequence: the
// level curve is not monotone. On a narrowPeak file level 12 and level 19 both
// produce a *larger* archive than the default, while on a cCRE registry level
// 16 is 28% smaller than the default. A constant is therefore wrong whichever
// constant is chosen, and the level has to be measured per file.
//
// 19 is not a candidate: measured on four files it lost to 16 on two of them,
// gained at most 3% on the others, and cost 3-4x the time to do it.
constexpr int kCalibLevels[] = { 0 /* OpenZL default */, 12, 16 };

// Prefer the cheapest level whose archive is within this of the smallest. Same
// rule and the same 2% as the FASTQ codec, so a marginal gain never buys a
// large speed penalty.
constexpr double kLevelSlack = 0.02;

// Calibrates on a prefix of the first block rather than the whole block: the
// ranking is what is wanted, not the sizes, and pricing three levels over a
// full 32 MB block would cost more than the compression it is optimising.
constexpr size_t kCalibBytes = 8u << 20;

struct Calibration {
    int level = 0;
    bool dual = true;     // whether compressing blocks whole is worth trying
};

// Whole-block compression is kept per file only if, on the calibration sample,
// it comes within this of the column split. It is the single most expensive
// thing the encoder does -- at level 16 it was 7.6 of 11.7 s on a 53 MB file --
// and it only ever wins where the split is close, so a file where the split
// wins clearly on the sample does not pay for it on every block.
constexpr double kDualMargin = 0.03;

Calibration calibrateLevel(const std::string& block, const std::string& modelDir,
                           bool quiet, size_t threads) {
    Calibration cal;
    size_t n = std::min(block.size(), kCalibBytes);
    if (n < block.size()) {                       // cut on a line boundary
        const size_t nl = block.rfind('\n', n);
        if (nl == std::string::npos) return cal;
        n = nl + 1;
    }
    if (n < (1u << 16)) return cal;               // too small to tell us anything

    // Each level gets an equal share of the worker Codecs, rebuilt at that
    // level; the levels are priced concurrently rather than one after another.
    const size_t L = sizeof kCalibLevels / sizeof *kCalibLevels;
    const size_t per = std::max<size_t>(1, threads / L);
    std::vector<size_t> split(L, SIZE_MAX), whole(L, SIZE_MAX);
    std::vector<std::thread> ts;
    for (size_t li = 0; li < L; li++) {
        ts.emplace_back([&, li] {
            std::vector<std::unique_ptr<Codec>> own;
            std::vector<Codec*> cs;
            for (size_t k = 0; k < per; k++) {
                own.emplace_back(new Codec(modelDir, bedClass, kCalibLevels[li]));
                cs.push_back(own.back().get());
            }
            // The whole-block price needs no column work, so it runs beside the
            // split rather than after it, on a Codec of its own.
            std::thread wt([&, li] {
                Codec wc(modelDir, bedClass, kCalibLevels[li]);
                Bundle vb;
                vb.at("@raw") = block.substr(0, n);
                whole[li] = wc.compressBundle(vb).size();
            });
            Bundle b;
            if (encodeBlock(block.data(), n, b, cs))
                split[li] = compressBundleParallel(b, cs).size();
            wt.join();
        });
    }
    for (auto& t : ts) t.join();
    if (split[0] == SIZE_MAX) return cal;

    size_t bestSize = SIZE_MAX;
    for (size_t li = 0; li < L; li++) bestSize = std::min(bestSize, split[li]);
    // Cheapest level within the slack of the best: the lowest, since cost rises
    // monotonically with the level even where ratio does not.
    size_t pick = 0;
    for (size_t li = 0; li < L; li++)
        if ((double)split[li] <= (double)bestSize * (1.0 + kLevelSlack)) { pick = li; break; }
    cal.level = kCalibLevels[pick];
    cal.dual = (double)whole[pick] <= (double)split[pick] * (1.0 + kDualMargin);

    if (!quiet) {
        std::fprintf(stderr, "nyx_bed: calibration on %.1f MB:", n / 1e6);
        for (size_t li = 0; li < L; li++)
            std::fprintf(stderr, " %s=%.0f/%.0fKB",
                         kCalibLevels[li] ? std::to_string(kCalibLevels[li]).c_str() : "def",
                         split[li] / 1e3, whole[li] / 1e3);
        std::fprintf(stderr, "  -> level %s, whole-block check %s\n",
                     cal.level ? std::to_string(cal.level).c_str() : "default",
                     cal.dual ? "on" : "off");
    }
    return cal;
}

// ---------------------------------------------------------------- driver

struct Options {
    std::string model;
    int threads = 0;
    size_t blockBytes = 32u << 20;
    size_t maxMemMB = 4000;      // 0 disables; matches fastazl's default
    bool selfcheck = true;
    bool dual = true;
    size_t dualMaxBytes = 96u << 20;
    bool rawBlocks = false;       // ablation floor: no column awareness at all
    bool verify = false;
    bool quiet = false;
    int level = 0;       // 0 = calibrate, or an explicit override
    bool calibrate = true;
};

void cmdCompress(const std::string& in, const std::string& out, Options& o) {
    Reader rd(in);
    std::string buf;
    buf.resize(1 << 22);
    std::string carry, header;
    size_t originalSize = 0;
    bool haveHeader = false;

    // The header is the run of leading track / browser / # lines. A comment
    // further down the file is not a header; it becomes an escaped row.
    auto findHeaderEnd = [](const std::string& s, size_t& consumed) -> bool {
        size_t p = 0;
        if (s.size() >= 3 && (unsigned char)s[0] == 0xEF && (unsigned char)s[1] == 0xBB
                          && (unsigned char)s[2] == 0xBF) p = 3;
        while (p < s.size()) {
            const size_t nl = s.find('\n', p);
            if (nl == std::string::npos) break;
            const bool hdr = s.compare(p, 5, "track") == 0
                          || s.compare(p, 7, "browser") == 0
                          || (p < s.size() && s[p] == '#');
            if (!hdr) { consumed = p; return true; }
            p = nl + 1;
        }
        consumed = p;
        return false;
    };

    while (!haveHeader) {
        const size_t got = rd.read(&buf[0], buf.size());
        if (got == 0) break;
        originalSize += got;
        carry.append(buf.data(), got);
        size_t consumed = 0;
        if (findHeaderEnd(carry, consumed)) {
            header.assign(carry, 0, consumed);
            carry.erase(0, consumed);
            haveHeader = true;
        } else if (carry.size() > (64u << 20)) {
            header.append(carry, 0, consumed);
            carry.erase(0, consumed);
            break;
        }
    }
    if (!haveHeader) { header += carry; carry.clear(); }

    const int threads = o.threads > 0 ? o.threads
                                      : (int)std::max(1u, std::thread::hardware_concurrency());
    if (o.maxMemMB) {
        // Per-block cost is the block text, the field-offset array, the decoded
        // copy made by the self-check and the candidate streams: measured
        // between 4x and 6x the block. Size from the conservative end.
        constexpr size_t kOverhead = 6;
        size_t b = (o.maxMemMB * 1000000ull) / ((size_t)threads * kOverhead);
        b = std::max<size_t>(b, 4u << 20);
        b = std::min<size_t>(b, 256u << 20);
        o.blockBytes = b;
        if (!o.quiet)
            std::fprintf(stderr, "nyx_bed: memory budget %zu MB over %d threads -> %.0f MB blocks\n",
                         o.maxMemMB, threads, b / 1e6);
    }

    FILE* fo = std::fopen(out.c_str(), "wb");
    if (!fo) die("cannot create " + out);

    Codec headerCodec(o.model, bedClass, o.level);
    std::string hdrComp;
    {
        Bundle hb;
        hb.at("@esc") = header;
        hdrComp = headerCodec.compressBundle(hb);
    }
    std::string fileHdr;
    fileHdr.append(kMagic, 8);
    put_u32(fileHdr, 0);
    put_u64(fileHdr, header.size());
    put_u64(fileHdr, hdrComp.size());
    writeAll(fo, fileHdr.data(), fileHdr.size());
    writeAll(fo, hdrComp.data(), hdrComp.size());


    std::vector<BlockRec> index;
    std::vector<std::string> raws(threads), comps(threads);
    bool eof = false;

    auto fillBlock = [&](std::string& dst) -> bool {
        dst.clear();
        dst.swap(carry);
        while (dst.size() < o.blockBytes && !eof) {
            const size_t got = rd.read(&buf[0], buf.size());
            if (got == 0) { eof = true; break; }
            originalSize += got;
            dst.append(buf.data(), got);
        }
        if (dst.empty()) return false;
        size_t nl = dst.rfind('\n');
        while (nl == std::string::npos && !eof) {
            const size_t got = rd.read(&buf[0], buf.size());
            if (got == 0) { eof = true; break; }
            originalSize += got;
            dst.append(buf.data(), got);
            nl = dst.rfind('\n');
        }
        if (nl != std::string::npos && nl + 1 < dst.size()) {
            carry.assign(dst, nl + 1, dst.size() - nl - 1);
            dst.resize(nl + 1);
        }
        return true;
    };

    // Calibrate the entropy level on the first block before any worker exists,
    // then hold it for the whole file. Encoder-side only: OpenZL frames carry
    // everything the decoder needs, so archives written at any level decode the
    // same way and the format is unchanged.
    bool havePending = fillBlock(raws[0]);
    if (havePending && o.calibrate && o.level == 0) {
        const Calibration cal = calibrateLevel(raws[0], o.model, o.quiet, (size_t)threads);
        o.level = cal.level;
        if (!cal.dual) o.dual = false;
    }

    std::vector<Codec*> codecs;
    for (int i = 0; i < threads; i++) codecs.push_back(new Codec(o.model, bedClass, o.level));

    while (true) {
        int n = 0;
        if (havePending) { n = 1; havePending = false; }
        for (; n < threads; n++) if (!fillBlock(raws[n])) break;
        if (n == 0) break;
        std::vector<std::thread> pool;
        std::vector<BlockRec> recs(n);
        // Fewer blocks than threads is the common case for BED -- most files
        // are one or two blocks -- so each block gets a disjoint slice of the
        // Codecs and uses them for column pricing and stream compression.
        std::vector<std::vector<Codec*>> slice(n);
        for (int w = 0; w < threads; w++) slice[w % n].push_back(codecs[w]);
        for (int i = 0; i < n; i++) {
            pool.emplace_back([&, i] {
                Bundle b;
                bool ok = !o.rawBlocks && encodeBlock(raws[i].data(), raws[i].size(), b, slice[i]);
                if (ok && o.selfcheck) {
                    std::vector<std::string> s(b.data.begin(), b.data.end());
                    if (decodeBlock(s, b.names, (unsigned)slice[i].size()) != raws[i]) ok = false;
                }
                auto verbatim = [&] {
                    Bundle vb;
                    vb.at("@raw") = raws[i];
                    return slice[i][0]->compressBundle(vb);
                };
                if (!ok) {
                    comps[i] = verbatim();
                    recs[i] = {comps[i].size(), raws[i].size(), 1, 1};
                } else {
                    comps[i] = compressBundleParallel(b, slice[i]);
                    recs[i] = {comps[i].size(), raws[i].size(), (uint32_t)b.size(), 0};
                    // Column splitting severs the cross-field matches a large
                    // window LZ finds, and on some tables that loses outright.
                    // Measured, not assumed: compress the block whole as well
                    // and keep whichever is smaller.
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

    const long idxOff = std::ftell(fo);
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
        std::fprintf(stderr, "nyx_bed: %.1f MB -> %.1f MB  (%.2fx)  blocks=%zu verbatim=%zu\n",
                     originalSize / 1e6, (double)st.st_size / 1e6,
                     st.st_size ? (double)originalSize / (double)st.st_size : 0.0,
                     index.size(), fallbacks);
    }
}

// The decoder rebuilds stream names from the directory, so it must agree with
// the encoder about them. Names are derived, never stored.
std::vector<std::string> namesFor(const std::string& dir, uint32_t nStreams) {
    const char* p = dir.data();
    const char* e = dir.data() + dir.size();
    get_varint(p, e);                                   // nRows
    const uint32_t W = (uint32_t)get_varint(p, e);
    if (p >= e) die("corrupt directory");
    p++;                                                // flags
    std::vector<uint8_t> enc(W);
    for (uint32_t j = 0; j < W; j++) {
        if (p >= e) die("corrupt directory");
        enc[j] = (uint8_t)*p++;
        if (enc[j] == CE_COPY || enc[j] == CE_XDELTA || enc[j] == CE_MAP) get_varint(p, e);
    }
    std::vector<std::string> out;
    out.push_back("@dir");
    for (uint32_t j = 0; j < W; j++) {
        const std::string b = "c" + std::to_string(j);
        switch (enc[j]) {
            case CE_RAW:     out.push_back(b); break;
            case CE_NUM:     out.push_back(b + "#v"); break;
            case CE_DELTA:   out.push_back(b + "#d"); break;
            case CE_DICT:    out.push_back(b + "#x"); out.push_back(b + "#k"); break;
            case CE_COPY:    break;
            case CE_XDELTA:  out.push_back(b + "#e"); break;
            case CE_MAP:     out.push_back(b + "#m"); break;
            case CE_DECIMAL: out.push_back(b + "#s"); out.push_back(b + "#i"); break;
            case CE_INTLIST: out.push_back(b + "#n"); out.push_back(b + "#l"); break;
            default: die("unknown column encoding in directory");
        }
    }
    // @esc sits between @dir and the columns when present; the stream count is
    // what tells us whether it is there.
    if (out.size() + 1 == nStreams) out.insert(out.begin() + 1, "@esc");
    if (out.size() != nStreams) die("stream name/count mismatch");
    return out;
}

void cmdDecompress(const std::string& in, const std::string& out, Options& o) {
    FILE* f = std::fopen(in.c_str(), "rb");
    if (!f) die("cannot open " + in);
    char magic[8];
    if (std::fread(magic, 1, 8, f) != 8 || std::memcmp(magic, kMagic, 8) != 0)
        die(in + " is not a nyx_bed archive");
    uint32_t flags; uint64_t hdrLen, hdrComp;
    if (std::fread(&flags, 4, 1, f) != 1 || std::fread(&hdrLen, 8, 1, f) != 1
        || std::fread(&hdrComp, 8, 1, f) != 1) die("truncated archive");

    std::string hc(hdrComp, '\0');
    if (hdrComp && std::fread(&hc[0], 1, hdrComp, f) != hdrComp) die("truncated header");

    if (std::fseek(f, -16, SEEK_END) != 0) die("truncated archive");
    uint64_t idxOff, origSize;
    if (std::fread(&idxOff, 8, 1, f) != 1 || std::fread(&origSize, 8, 1, f) != 1)
        die("truncated trailer");
    const long idxEnd = std::ftell(f) - 16;
    if (std::fseek(f, (long)idxOff, SEEK_SET) != 0) die("corrupt index offset");
    std::string idx((size_t)(idxEnd - (long)idxOff), '\0');
    if (!idx.empty() && std::fread(&idx[0], 1, idx.size(), f) != idx.size()) die("truncated index");

    const char* ip = idx.data();
    const char* ie = idx.data() + idx.size();
    const uint64_t nBlocks = get_varint(ip, ie);
    std::vector<BlockRec> recs(nBlocks);
    for (uint64_t i = 0; i < nBlocks; i++) {
        recs[i].compLen  = get_varint(ip, ie);
        recs[i].rawLen   = get_varint(ip, ie);
        recs[i].nStreams = (uint32_t)get_varint(ip, ie);
        if (ip >= ie) die("truncated index");
        recs[i].kind = (uint8_t)*ip++;
    }

    FILE* fo = std::fopen(out.c_str(), "wb");
    if (!fo) die("cannot create " + out);

    const int threads = o.threads > 0 ? o.threads
                                      : (int)std::max(1u, std::thread::hardware_concurrency());
    std::vector<Codec*> codecs;
    for (int i = 0; i < threads; i++) codecs.push_back(new Codec(std::string(), bedClass));

    {
        Codec hcod(std::string(), bedClass);
        if (hdrComp) {
            auto s = hcod.decompressBundle(hc.data(), hc.size(), 1);
            writeAll(fo, s[0].data(), s[0].size());
        }
    }

    if (std::fseek(f, 8 + 4 + 8 + 8 + (long)hdrComp, SEEK_SET) != 0) die("seek failed");
    std::vector<std::string> blobs(threads), outs(threads);
    size_t done = 0;
    while (done < nBlocks) {
        const int n = (int)std::min<size_t>(threads, nBlocks - done);
        for (int i = 0; i < n; i++) {
            blobs[i].assign((size_t)recs[done + i].compLen, '\0');
            if (recs[done + i].compLen
                && std::fread(&blobs[i][0], 1, blobs[i].size(), f) != blobs[i].size())
                die("truncated block");
        }
        // As in compression: a file of one or two blocks would otherwise decode
        // on one or two cores, so each block gets a slice of the threads for its
        // streams and columns. Measured before this, BED decompression ran at
        // 130% CPU on sixteen threads.
        std::vector<std::vector<Codec*>> slice(n);
        for (int w = 0; w < threads; w++) slice[w % n].push_back(codecs[w]);
        std::vector<std::thread> pool;
        for (int i = 0; i < n; i++) {
            pool.emplace_back([&, i] {
                const BlockRec& r = recs[done + i];
                auto s = decompressBundleParallel(blobs[i].data(), blobs[i].size(), r.nStreams, slice[i]);
                if (r.kind == 1) outs[i] = std::move(s[0]);
                else outs[i] = decodeBlock(s, namesFor(s[0], r.nStreams), (unsigned)slice[i].size());
            });
        }
        for (auto& t : pool) t.join();
        for (int i = 0; i < n; i++) {
            writeAll(fo, outs[i].data(), outs[i].size());
            outs[i].clear();
            outs[i].shrink_to_fit();
        }
        done += n;
    }
    std::fclose(fo);
    std::fclose(f);
    for (auto* c : codecs) delete c;
    if (!o.quiet) std::fprintf(stderr, "nyx_bed: wrote %s\n", out.c_str());
}

void cmdInspect(const std::string& in) {
    FILE* f = std::fopen(in.c_str(), "rb");
    if (!f) die("cannot open " + in);
    char magic[8];
    if (std::fread(magic, 1, 8, f) != 8 || std::memcmp(magic, kMagic, 8) != 0)
        die(in + " is not a nyx_bed archive");
    uint32_t flags; uint64_t hdrLen, hdrComp;
    if (std::fread(&flags, 4, 1, f) != 1 || std::fread(&hdrLen, 8, 1, f) != 1
        || std::fread(&hdrComp, 8, 1, f) != 1) die("truncated archive");
    if (std::fseek(f, (long)hdrComp, SEEK_CUR) != 0) die("truncated archive");
    if (std::fseek(f, -16, SEEK_END) != 0) die("truncated archive");
    uint64_t idxOff, origSize;
    if (std::fread(&idxOff, 8, 1, f) != 1 || std::fread(&origSize, 8, 1, f) != 1)
        die("truncated trailer");
    struct stat st{};
    stat(in.c_str(), &st);
    std::printf("nyx_bed archive  original %.1f MB  compressed %.1f MB  (%.2fx)  header %llu B\n",
                origSize / 1e6, (double)st.st_size / 1e6,
                st.st_size ? (double)origSize / (double)st.st_size : 0.0,
                (unsigned long long)hdrLen);
    std::fclose(f);
}

void usage() {
    std::fprintf(stderr,
        "nyx_bed — byte-exact, column-aware compression for BED and BED-like tables\n\n"
        "  nyx_bed compress   [opts] <in.bed[.gz]> <out.nbed>\n"
        "  nyx_bed decompress [opts] <in.nbed>     <out.bed>\n"
        "  nyx_bed inspect            <in.nbed>\n\n"
        "  --threads N        worker threads (default: all cores)\n"
        "  --block-mb N       block size in MB (default 32)\n"
        "  --max-mem-mb N     memory budget in MB (default 4000; 0 disables)\n"
        "  --models DIR       directory of per-class OpenZL models\n"
        "  --level N          fix the entropy level instead of calibrating\n"
        "  --no-calibrate     use OpenZL's default level, no calibration\n"
        "  --verify           decompress after compressing and compare\n"
        "  --no-selfcheck     skip the per-block decode-and-compare\n"
        "  --no-dual          do not also try compressing each block whole\n"
        "  --raw-blocks       ablation: no column awareness at all\n"
        "  --quiet\n");
}

}  // namespace

int main(int argc, char** argv) {
    progName() = "nyx_bed";
    if (argc < 2) { usage(); return 1; }
    const std::string cmd = argv[1];
    Options o;
    std::vector<std::string> pos;
    for (int i = 2; i < argc; i++) {
        const std::string a = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) die("missing value for " + a);
            return argv[++i];
        };
        if (a == "--threads") o.threads = std::atoi(next().c_str());
        else if (a == "--block-mb") o.blockBytes = (size_t)std::atoll(next().c_str()) << 20;
        else if (a == "--max-mem-mb") o.maxMemMB = (size_t)std::atoll(next().c_str());
        else if (a == "--models") o.model = next();
        else if (a == "--level") o.level = std::atoi(next().c_str());
        else if (a == "--verify") o.verify = true;
        else if (a == "--no-selfcheck") o.selfcheck = false;
        else if (a == "--no-dual") o.dual = false;
        else if (a == "--no-calibrate") o.calibrate = false;
        else if (a == "--raw-blocks") o.rawBlocks = true;
        else if (a == "--quiet") o.quiet = true;
        else if (a == "-h" || a == "--help") { usage(); return 0; }
        else pos.push_back(a);
    }
    if (cmd == "compress") {
        if (pos.size() < 2) { usage(); return 1; }
        cmdCompress(pos[0], pos[1], o);
        if (o.verify) {
            const std::string tmp = pos[1] + ".verify";
            Options d = o; d.quiet = true;
            cmdDecompress(pos[1], tmp, d);
            // Compare against the input the user actually gave us.
            FILE* a = std::fopen(pos[0].c_str(), "rb");
            FILE* b = std::fopen(tmp.c_str(), "rb");
            if (!a || !b) die("verify: cannot reopen files");
            std::string ba(1 << 20, '\0'), bb(1 << 20, '\0');
            bool same = true;
            while (same) {
                const size_t na = std::fread(&ba[0], 1, ba.size(), a);
                const size_t nb = std::fread(&bb[0], 1, bb.size(), b);
                if (na != nb || std::memcmp(ba.data(), bb.data(), na) != 0) { same = false; break; }
                if (na == 0) break;
            }
            std::fclose(a); std::fclose(b);
            std::remove(tmp.c_str());
            if (!same) die("verify FAILED: round trip is not byte-exact");
            std::fprintf(stderr, "nyx_bed: verify OK (byte-exact)\n");
        }
    } else if (cmd == "decompress") {
        if (pos.size() < 2) { usage(); return 1; }
        cmdDecompress(pos[0], pos[1], o);
    } else if (cmd == "inspect") {
        if (pos.empty()) { usage(); return 1; }
        cmdInspect(pos[0]);
    } else { usage(); return 1; }
    return 0;
}
