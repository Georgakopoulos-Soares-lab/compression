// fasta_postprocess — inverse of the FAV5 encoder in biocompress_preprocessor
// (process_fasta_packed_chunk). Reconstructs the exact original FASTA bytes.
//
//   fasta_postprocess <chunks_dir> <out.fasta> [--suffix .fasta_packed.bin]
//   fasta_postprocess --decode-one <chunk.bin> <out.fasta>
//
// <chunks_dir> is scanned for files ending in the suffix (default
// ".fasta_packed.bin"); they are decoded in lexicographic name order and
// concatenated. Each chunk was cut on a record boundary by the encoder, so the
// concatenation is byte-identical to the input FASTA.

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <filesystem>
#include <iostream>
#include <string>
#include <vector>

namespace fs = std::filesystem;

namespace {

[[noreturn]] void die(const std::string& m) { std::cerr << "Error: " << m << "\n"; std::exit(1); }

std::vector<uint8_t> read_file(const fs::path& p) {
    std::ifstream f(p, std::ios::binary);
    if (!f) die("cannot read " + p.string());
    return std::vector<uint8_t>((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
}

struct Reader {
    const uint8_t* p; const uint8_t* end;
    Reader(const std::vector<uint8_t>& v) : p(v.data()), end(v.data() + v.size()) {}
    void need(size_t n) { if (static_cast<size_t>(end - p) < n) die("FAV5 chunk truncated"); }
    template <class T> T u() { need(sizeof(T)); T v = 0; for (size_t i = 0; i < sizeof(T); i++) v |= static_cast<T>(p[i]) << (8 * i); p += sizeof(T); return v; }
    const uint8_t* take(size_t n) { need(n); const uint8_t* r = p; p += n; return r; }
};

// Decode one FAV5 chunk into exact original bytes, appended to `out`.
void decode_chunk(const std::vector<uint8_t>& buf, std::ostream& os) {
    Reader r(buf);
    const uint8_t* magic = r.take(4);
    if (std::memcmp(magic, "FAV5", 4) != 0) die("not a FAV5 chunk (bad magic)");
    uint8_t flags = r.u<uint8_t>();
    r.take(3);
    bool ends_nl = flags & 1u;

    uint32_t preamble_len = r.u<uint32_t>();
    uint32_t num_records  = r.u<uint32_t>();
    uint64_t n_seqpos     = r.u<uint64_t>();
    uint64_t n_base       = r.u<uint64_t>();
    uint64_t n_caseruns   = r.u<uint64_t>();
    uint64_t n_excruns    = r.u<uint64_t>();
    uint64_t n_linelens   = r.u<uint64_t>();
    uint64_t hdr_bytes    = r.u<uint64_t>();

    const uint8_t* preamble = r.take(preamble_len);

    std::vector<uint32_t> hdr_lens(num_records), rec_nlines(num_records);
    for (auto& v : hdr_lens)   v = r.u<uint32_t>();
    for (auto& v : rec_nlines) v = r.u<uint32_t>();
    std::vector<uint32_t> line_lens(n_linelens);
    for (auto& v : line_lens)  v = r.u<uint32_t>();
    std::vector<uint64_t> case_runs(n_caseruns);
    for (auto& v : case_runs)  v = r.u<uint64_t>();
    std::vector<uint64_t> exc_gaps(n_excruns), exc_lens(n_excruns);
    for (auto& v : exc_gaps)   v = r.u<uint64_t>();
    for (auto& v : exc_lens)   v = r.u<uint64_t>();
    const uint8_t* exc_bytes  = r.take(n_excruns);
    const uint8_t* packed2bit = r.take((n_base + 3) / 4);
    const uint8_t* headers    = r.take(hdr_bytes);

    if (num_records == 0) {                       // whole range was preamble
        os.write(reinterpret_cast<const char*>(preamble), preamble_len);
        return;
    }

    // rebuild the concatenated sequence
    std::string seq;
    seq.reserve(n_seqpos);
    static const char B[4] = {'A', 'C', 'G', 'T'};
    size_t base_i = 0;
    // exception-run cursor
    size_t exc_i = 0; uint64_t exc_cur = 0, exc_rem = 0;
    if (n_excruns) { exc_cur = exc_gaps[0]; exc_rem = exc_lens[0]; }
    // case RLE cursor (first run = uppercase; a leading 0 run flips immediately)
    size_t run_i = 0; bool lower = false; uint64_t run_left = n_caseruns ? case_runs[0] : n_base;
    while (run_left == 0 && run_i + 1 < n_caseruns) { lower = !lower; run_left = case_runs[++run_i]; }

    for (uint64_t pos = 0; pos < n_seqpos; ++pos) {
        if (exc_rem > 0 && pos == exc_cur) {
            seq.push_back(static_cast<char>(exc_bytes[exc_i]));
            ++exc_cur;
            if (--exc_rem == 0 && ++exc_i < n_excruns) { exc_cur = pos + 1 + exc_gaps[exc_i]; exc_rem = exc_lens[exc_i]; }
            continue;
        }
        char c = B[(packed2bit[base_i >> 2] >> (2 * (base_i & 3))) & 3];
        if (lower) c = static_cast<char>(c + 32);
        seq.push_back(c);
        ++base_i;
        if (--run_left == 0) {
            while (run_i + 1 < n_caseruns) { lower = !lower; run_left = case_runs[++run_i]; if (run_left) break; }
        }
    }
    if (base_i != n_base) die("FAV5: base count mismatch");

    // stream the records straight to os (one deferred '\n' so a final line with
    // no trailing newline in the original can be reproduced without buffering).
    os.write(reinterpret_cast<const char*>(preamble), preamble_len);
    bool pending_nl = false;
    auto emit = [&](const char* p, size_t n) {
        if (pending_nl) { os.put('\n'); pending_nl = false; }
        os.write(p, n);
    };
    size_t hoff = 0, loff = 0, soff = 0;
    for (uint32_t rec = 0; rec < num_records; ++rec) {
        char gt = '>'; emit(&gt, 1);
        emit(reinterpret_cast<const char*>(headers + hoff), hdr_lens[rec]);
        hoff += hdr_lens[rec];
        pending_nl = true;
        for (uint32_t k = 0; k < rec_nlines[rec]; ++k) {
            uint32_t ll = line_lens[loff++];
            emit(seq.data() + soff, ll);
            soff += ll;
            pending_nl = true;
        }
    }
    if (pending_nl && ends_nl) os.put('\n');   // suppressed iff original had no final newline
}

} // namespace

int main(int argc, char** argv) {
    if (argc >= 4 && std::string(argv[1]) == "--decode-one") {
        std::ofstream f(argv[3], std::ios::binary);
        if (!f) die(std::string("cannot write ") + argv[3]);
        decode_chunk(read_file(argv[2]), f);
        return 0;
    }
    if (argc < 3) {
        std::cerr << "Usage: " << argv[0] << " <chunks_dir> <out.fasta> [--suffix .fasta_packed.bin]\n"
                  << "       " << argv[0] << " --decode-one <chunk.bin> <out.fasta>\n";
        return 1;
    }
    fs::path dir = argv[1], outp = argv[2];
    std::string suffix = ".fasta_packed.bin";
    for (int i = 3; i + 1 < argc; i++) if (std::string(argv[i]) == "--suffix") suffix = argv[i + 1];

    std::vector<fs::path> chunks;
    for (auto& e : fs::directory_iterator(dir)) {
        auto n = e.path().filename().string();
        if (n.size() >= suffix.size() && n.compare(n.size() - suffix.size(), suffix.size(), suffix) == 0)
            chunks.push_back(e.path());
    }
    if (chunks.empty()) die("no *" + suffix + " files in " + dir.string());
    std::sort(chunks.begin(), chunks.end());

    std::ofstream f(outp, std::ios::binary);
    if (!f) die("cannot write " + outp.string());
    std::vector<char> iobuf(1 << 22);
    f.rdbuf()->pubsetbuf(iobuf.data(), iobuf.size());
    for (auto& c : chunks) decode_chunk(read_file(c), f);   // one chunk in RAM at a time
    f.flush();
    std::cerr << "fasta_postprocess: " << chunks.size() << " chunks -> " << outp
              << " (" << static_cast<long long>(f.tellp()) << " bytes)\n";
    return 0;
}
