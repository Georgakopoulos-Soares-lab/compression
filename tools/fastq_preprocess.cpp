/*
 * fastq_preprocess.cpp — FASTQ preprocessor for compression (parallel)
 *
 * Converts FASTQ (4 lines per record) into a tab-delimited format that
 * OpenZL's CSV profiler can compress efficiently.
 *
 * Transforms applied:
 *   1. Drop "+" separator lines (always reconstructable)
 *   2. Parse Illumina headers into structured fields
 *   3. Drop constant fields (prefix, instrument if single)
 *   4. Dictionary-encode low-cardinality fields (run, flowcell, lane, tile)
 *   5. Sequence and quality kept as separate columns
 *
 * Output: .meta (binary sidecar) + .tsv (tab-delimited for OpenZL)
 *
 * Usage:
 *   fastq_preprocess encode  <input.fastq> <output_prefix> [threads]
 *   fastq_preprocess decode  <output_prefix> <output.fastq> [threads]
 */

#include <iostream>
#include <fstream>
#include <sstream>
#include <vector>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <algorithm>
#include <cstdint>
#include <cstring>
#include <cassert>
#include <thread>
#include <atomic>
#include <chrono>

// For mmap
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>

static const char MAGIC[] = "FQPP01";

// ---- Lightweight header parsing (no allocations for dict lookup) ----
struct ParsedHeader {
    const char* prefix_start;   int prefix_len;
    int64_t     read_num;
    const char* instr_start;    int instr_len;
    const char* run_start;      int run_len;
    const char* fc_start;       int fc_len;
    const char* lane_start;     int lane_len;
    const char* tile_start;     int tile_len;
    const char* x_start;        int x_len;
    const char* y_start;        int y_len;
    bool parsed;
};

static ParsedHeader parse_header_fast(const char* line, int len) {
    ParsedHeader h{};
    h.parsed = false;
    if (len < 3 || line[0] != '@') return h;

    int sp = -1;
    for (int i = 1; i < len; i++) {
        if (line[i] == ' ') { sp = i; break; }
    }
    if (sp < 0) return h;

    int dot = -1;
    for (int i = sp - 1; i >= 1; i--) {
        if (line[i] == '.') { dot = i; break; }
    }
    if (dot < 0) return h;

    h.prefix_start = line + 1;
    h.prefix_len = dot - 1;

    h.read_num = 0;
    for (int i = dot + 1; i < sp; i++) {
        if (line[i] < '0' || line[i] > '9') return h;
        h.read_num = h.read_num * 10 + (line[i] - '0');
    }

    const char* info = line + sp + 1;
    int info_len = len - sp - 1;

    int colons[6];
    int ncol = 0;
    for (int i = 0; i < info_len && ncol < 6; i++) {
        if (info[i] == ':') colons[ncol++] = i;
    }
    if (ncol < 6) return h;

    h.instr_start = info;           h.instr_len = colons[0];
    h.run_start = info+colons[0]+1; h.run_len = colons[1]-colons[0]-1;
    h.fc_start = info+colons[1]+1;  h.fc_len = colons[2]-colons[1]-1;
    h.lane_start = info+colons[2]+1;h.lane_len = colons[3]-colons[2]-1;
    h.tile_start = info+colons[3]+1;h.tile_len = colons[4]-colons[3]-1;
    h.x_start = info+colons[4]+1;   h.x_len = colons[5]-colons[4]-1;
    h.y_start = info+colons[5]+1;   h.y_len = info_len-colons[5]-1;

    h.parsed = true;
    return h;
}

static inline std::string sv(const char* s, int len) { return std::string(s, len); }

int main(int argc, char** argv) {
    if (argc < 4) {
        std::cerr << "Usage: fastq_preprocess <encode|decode> <input> <output> [threads]\n";
        return 1;
    }

    std::string mode = argv[1];
    std::string input_path = argv[2];
    std::string output_path = argv[3];
    int nthreads = (argc > 4) ? std::atoi(argv[4]) : std::thread::hardware_concurrency();
    if (nthreads < 1) nthreads = 1;

    if (mode == "encode") {
        auto t0 = std::chrono::steady_clock::now();

        // ---- mmap input ----
        int fd = open(input_path.c_str(), O_RDONLY);
        if (fd < 0) { std::cerr << "Cannot open " << input_path << "\n"; return 1; }
        struct stat st;
        fstat(fd, &st);
        size_t file_size = st.st_size;
        const char* data = (const char*)mmap(nullptr, file_size, PROT_READ,
                                             MAP_PRIVATE | MAP_POPULATE, fd, 0);
        if (data == MAP_FAILED) { std::cerr << "mmap failed\n"; return 1; }
        madvise((void*)data, file_size, MADV_SEQUENTIAL);

        // ---- Find line starts (parallel) ----
        // Each thread scans its byte-range and records newline positions
        std::vector<std::vector<size_t>> thread_newlines(nthreads);
        {
            size_t byte_chunk = (file_size + nthreads - 1) / nthreads;
            std::vector<std::thread> threads;
            for (int t = 0; t < nthreads; t++) {
                size_t blo = t * byte_chunk;
                size_t bhi = std::min(blo + byte_chunk, file_size);
                threads.emplace_back([&, t, blo, bhi]() {
                    auto& nl = thread_newlines[t];
                    nl.reserve((bhi - blo) / 40);
                    for (size_t i = blo; i < bhi; i++) {
                        if (data[i] == '\n' && i + 1 < file_size)
                            nl.push_back(i + 1);
                    }
                });
            }
            for (auto& t : threads) t.join();
        }

        // Merge newline positions
        size_t total_newlines = 1; // for position 0
        for (auto& v : thread_newlines) total_newlines += v.size();

        std::vector<size_t> line_starts;
        line_starts.reserve(total_newlines);
        line_starts.push_back(0);
        for (auto& v : thread_newlines) {
            line_starts.insert(line_starts.end(), v.begin(), v.end());
            v.clear(); v.shrink_to_fit(); // free memory
        }
        thread_newlines.clear();

        size_t nlines = line_starts.size();
        size_t nrecs = nlines / 4;
        std::cerr << "Read " << nrecs << " records (" << nlines << " lines) using "
                  << nthreads << " threads\n";

        if (nrecs == 0) { std::cerr << "No records found\n"; return 1; }

        // Helper to get a line by index
        auto get_line = [&](size_t idx, int& len) -> const char* {
            size_t start = line_starts[idx];
            size_t end = (idx + 1 < line_starts.size())
                         ? line_starts[idx + 1] - 1 : file_size;
            while (end > start && (data[end-1] == '\r' || data[end-1] == '\n')) end--;
            len = (int)(end - start);
            return data + start;
        };

        // ---- PASS 1: Parse all headers in parallel ----
        std::vector<ParsedHeader> headers(nrecs);
        {
            std::vector<std::thread> threads;
            size_t chunk = (nrecs + nthreads - 1) / nthreads;
            for (int t = 0; t < nthreads; t++) {
                size_t lo = t * chunk;
                size_t hi = std::min(lo + chunk, nrecs);
                threads.emplace_back([&, lo, hi]() {
                    for (size_t i = lo; i < hi; i++) {
                        int len;
                        const char* hdr = get_line(i * 4, len);
                        headers[i] = parse_header_fast(hdr, len);
                    }
                });
            }
            for (auto& t : threads) t.join();
        }

        // ---- Analyze fields ----
        size_t parsed_count = 0;
        for (size_t i = 0; i < nrecs; i++) {
            if (headers[i].parsed) parsed_count++;
        }
        bool all_parsed = (parsed_count == nrecs);
        std::cerr << "Parsed headers: " << parsed_count << "/" << nrecs
                  << (all_parsed ? " (all)" : " (SOME UNPARSED)") << "\n";

        std::string constant_prefix, constant_instrument;
        bool prefix_constant = true, instrument_constant = true, read_num_sequential = true;
        std::unordered_set<std::string> run_vals, fc_vals, lane_vals, tile_vals;

        if (all_parsed) {
            auto& h0 = headers[0];
            constant_prefix = sv(h0.prefix_start, h0.prefix_len);
            constant_instrument = sv(h0.instr_start, h0.instr_len);

            for (size_t i = 0; i < nrecs; i++) {
                auto& h = headers[i];
                if (sv(h.prefix_start, h.prefix_len) != constant_prefix) prefix_constant = false;
                if (sv(h.instr_start, h.instr_len) != constant_instrument) instrument_constant = false;
                if ((int64_t)(i + 1) != h.read_num) read_num_sequential = false;
                run_vals.insert(sv(h.run_start, h.run_len));
                fc_vals.insert(sv(h.fc_start, h.fc_len));
                lane_vals.insert(sv(h.lane_start, h.lane_len));
                tile_vals.insert(sv(h.tile_start, h.tile_len));
            }

            std::cerr << "  Prefix: " << (prefix_constant ? "constant (" + constant_prefix + ")" : "varies") << "\n"
                      << "  Instrument: " << (instrument_constant ? "constant (" + constant_instrument + ")" : "varies") << "\n"
                      << "  Read numbers: " << (read_num_sequential ? "sequential (1..N)" : "non-sequential") << "\n"
                      << "  Runs: " << run_vals.size() << ", Flowcells: " << fc_vals.size()
                      << ", Lanes: " << lane_vals.size() << ", Tiles: " << tile_vals.size() << "\n";
        }

        // Build dictionaries
        auto build_dict = [](const std::unordered_set<std::string>& vals)
            -> std::pair<std::vector<std::string>, std::unordered_map<std::string, int>> {
            std::vector<std::string> sv(vals.begin(), vals.end());
            std::sort(sv.begin(), sv.end());
            std::unordered_map<std::string, int> m;
            for (size_t i = 0; i < sv.size(); i++) m[sv[i]] = (int)i;
            return {sv, m};
        };

        auto [run_dict, run_map] = build_dict(run_vals);
        auto [fc_dict, fc_map] = build_dict(fc_vals);
        auto [lane_dict, lane_map] = build_dict(lane_vals);
        auto [tile_dict, tile_map] = build_dict(tile_vals);

        // ---- Write meta ----
        std::string meta_path = output_path + ".meta";
        std::string tsv_path = output_path + ".tsv";

        {
            std::ofstream fmeta(meta_path, std::ios::binary);
            fmeta.write(MAGIC, 6);
            int32_t nr = (int32_t)nrecs;
            fmeta.write((char*)&nr, 4);

            uint8_t flags = 0;
            if (all_parsed) flags |= 1;
            if (prefix_constant) flags |= 2;
            if (instrument_constant) flags |= 4;
            if (read_num_sequential) flags |= 8;
            fmeta.write((char*)&flags, 1);

            auto write_str = [&](const std::string& s) {
                int16_t len = (int16_t)s.size();
                fmeta.write((char*)&len, 2);
                fmeta.write(s.c_str(), len);
            };
            if (prefix_constant) write_str(constant_prefix);
            if (instrument_constant) write_str(constant_instrument);

            auto write_dict_fn = [&](const std::vector<std::string>& dict) {
                int32_t dsize = (int32_t)dict.size();
                fmeta.write((char*)&dsize, 4);
                for (auto& s : dict) write_str(s);
            };
            if (all_parsed) {
                write_dict_fn(run_dict);
                write_dict_fn(fc_dict);
                write_dict_fn(lane_dict);
                write_dict_fn(tile_dict);
            }

            uint32_t marker = 0xDEADBEEF;
            fmeta.write((char*)&marker, 4);
            fmeta.close();
            std::cerr << "Meta written: " << meta_path << "\n";
        }

        // ---- PASS 2: Build TSV in parallel (per-thread buffers) ----
        std::vector<std::string> buffers(nthreads);
        {
            std::vector<std::thread> threads;
            size_t chunk = (nrecs + nthreads - 1) / nthreads;
            for (int t = 0; t < nthreads; t++) {
                size_t lo = t * chunk;
                size_t hi = std::min(lo + chunk, nrecs);
                threads.emplace_back([&, t, lo, hi]() {
                    std::string& buf = buffers[t];
                    buf.reserve((hi - lo) * 120);

                    for (size_t i = lo; i < hi; i++) {
                        int seq_len, qual_len;
                        const char* seq = get_line(i * 4 + 1, seq_len);
                        const char* qual = get_line(i * 4 + 3, qual_len);

                        if (!all_parsed) {
                            int hlen;
                            const char* hdr = get_line(i * 4, hlen);
                            buf.append(hdr + 1, hlen - 1);
                            buf += '\t';
                            buf.append(seq, seq_len);
                            buf += '\t';
                            buf.append(qual, qual_len);
                            buf += '\n';
                        } else {
                            auto& h = headers[i];
                            auto it_r = run_map.find(sv(h.run_start, h.run_len));
                            buf += std::to_string(it_r->second);
                            buf += '\t';
                            auto it_f = fc_map.find(sv(h.fc_start, h.fc_len));
                            buf += std::to_string(it_f->second);
                            buf += '\t';
                            auto it_l = lane_map.find(sv(h.lane_start, h.lane_len));
                            buf += std::to_string(it_l->second);
                            buf += '\t';
                            auto it_t = tile_map.find(sv(h.tile_start, h.tile_len));
                            buf += std::to_string(it_t->second);
                            buf += '\t';
                            buf.append(h.x_start, h.x_len);
                            buf += '\t';
                            buf.append(h.y_start, h.y_len);
                            buf += '\t';
                            buf.append(seq, seq_len);
                            buf += '\t';
                            buf.append(qual, qual_len);
                            buf += '\n';
                        }
                    }
                });
            }
            for (auto& t : threads) t.join();
        }

        // ---- Write all buffers sequentially ----
        {
            int out_fd = open(tsv_path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
            if (out_fd < 0) { std::cerr << "Cannot open " << tsv_path << "\n"; return 1; }
            for (auto& buf : buffers) {
                if (!buf.empty()) {
                    const char* p = buf.data();
                    size_t remaining = buf.size();
                    while (remaining > 0) {
                        ssize_t w = write(out_fd, p, remaining);
                        if (w <= 0) { std::cerr << "Write error\n"; return 1; }
                        p += w; remaining -= w;
                    }
                }
            }
            close(out_fd);
        }

        munmap((void*)data, file_size);
        close(fd);

        auto t1 = std::chrono::steady_clock::now();
        double elapsed = std::chrono::duration<double>(t1 - t0).count();
        std::cerr << "TSV written: " << tsv_path << " (" << nrecs << " records, "
                  << std::fixed << elapsed << "s)\n";

    } else if (mode == "decode") {
        // ---- DECODE (mmap + parallel) ----
        std::string meta_path = input_path + ".meta";
        std::string tsv_path = input_path + ".tsv";

        std::ifstream fmeta(meta_path, std::ios::binary);
        if (!fmeta) { std::cerr << "Cannot open " << meta_path << "\n"; return 1; }

        char magic[7] = {};
        fmeta.read(magic, 6);
        if (std::string(magic) != MAGIC) { std::cerr << "Bad magic\n"; return 1; }

        int32_t nrecs;
        fmeta.read((char*)&nrecs, 4);

        uint8_t flags;
        fmeta.read((char*)&flags, 1);
        bool all_parsed = flags & 1;
        bool prefix_constant = flags & 2;
        bool instrument_constant = flags & 4;
        bool read_num_sequential = flags & 8;

        auto read_str = [&]() -> std::string {
            int16_t len; fmeta.read((char*)&len, 2);
            std::string s(len, '\0'); fmeta.read(&s[0], len);
            return s;
        };

        std::string constant_prefix, constant_instrument;
        if (prefix_constant) constant_prefix = read_str();
        if (instrument_constant) constant_instrument = read_str();

        auto read_dict = [&]() -> std::vector<std::string> {
            int32_t dsize; fmeta.read((char*)&dsize, 4);
            std::vector<std::string> dict(dsize);
            for (int i = 0; i < dsize; i++) dict[i] = read_str();
            return dict;
        };

        std::vector<std::string> run_dict, fc_dict, lane_dict, tile_dict;
        if (all_parsed) {
            run_dict = read_dict(); fc_dict = read_dict();
            lane_dict = read_dict(); tile_dict = read_dict();
        }

        uint32_t marker;
        fmeta.read((char*)&marker, 4);
        assert(marker == 0xDEADBEEF);
        fmeta.close();

        // mmap TSV
        int fd = open(tsv_path.c_str(), O_RDONLY);
        if (fd < 0) { std::cerr << "Cannot open " << tsv_path << "\n"; return 1; }
        struct stat fst;
        fstat(fd, &fst);
        size_t file_size = fst.st_size;
        const char* data = (const char*)mmap(nullptr, file_size, PROT_READ,
                                             MAP_PRIVATE | MAP_POPULATE, fd, 0);
        if (data == MAP_FAILED) { std::cerr << "mmap failed\n"; return 1; }

        // Find line starts (parallel)
        std::vector<std::vector<size_t>> tnl(nthreads);
        {
            size_t bc = (file_size + nthreads - 1) / nthreads;
            std::vector<std::thread> threads;
            for (int t = 0; t < nthreads; t++) {
                size_t blo = t * bc, bhi = std::min(blo + bc, file_size);
                threads.emplace_back([&, t, blo, bhi]() {
                    tnl[t].reserve((bhi - blo) / 40);
                    for (size_t i = blo; i < bhi; i++)
                        if (data[i] == '\n' && i + 1 < file_size) tnl[t].push_back(i + 1);
                });
            }
            for (auto& t : threads) t.join();
        }
        std::vector<size_t> line_starts;
        line_starts.push_back(0);
        for (auto& v : tnl) { line_starts.insert(line_starts.end(), v.begin(), v.end()); v.clear(); }
        tnl.clear();

        auto get_line = [&](size_t idx, int& len) -> const char* {
            size_t start = line_starts[idx];
            size_t end = (idx + 1 < line_starts.size()) ? line_starts[idx + 1] - 1 : file_size;
            while (end > start && (data[end-1] == '\r' || data[end-1] == '\n')) end--;
            len = (int)(end - start);
            return data + start;
        };

        struct FieldSlice { const char* s; int len; };
        auto split_tabs = [](const char* line, int len, FieldSlice* out, int mx) -> int {
            int nf = 0, start = 0;
            for (int i = 0; i <= len && nf < mx; i++) {
                if (i == len || line[i] == '\t') { out[nf++] = {line + start, i - start}; start = i + 1; }
            }
            return nf;
        };

        // Parallel decode
        size_t nlines_actual = std::min(line_starts.size(), (size_t)nrecs);
        std::vector<std::string> buffers(nthreads);
        {
            std::vector<std::thread> threads;
            size_t chunk = (nlines_actual + nthreads - 1) / nthreads;
            for (int t = 0; t < nthreads; t++) {
                size_t lo = t * chunk, hi = std::min(lo + chunk, nlines_actual);
                threads.emplace_back([&, t, lo, hi]() {
                    std::string& buf = buffers[t];
                    buf.reserve((hi - lo) * 160);
                    for (size_t i = lo; i < hi; i++) {
                        int len; const char* line = get_line(i, len);
                        int64_t read_num = (int64_t)(i + 1);
                        FieldSlice fields[10];
                        int nf = split_tabs(line, len, fields, 10);
                        (void)nf;

                        if (!all_parsed) {
                            buf += '@'; buf.append(fields[0].s, fields[0].len); buf += '\n';
                            buf.append(fields[1].s, fields[1].len); buf += '\n';
                            buf += "+\n";
                            buf.append(fields[2].s, fields[2].len); buf += '\n';
                        } else {
                            auto to_int = [](const char* s, int l) { int v=0; for(int i=0;i<l;i++) v=v*10+(s[i]-'0'); return v; };
                            int rid = to_int(fields[0].s, fields[0].len);
                            int fid = to_int(fields[1].s, fields[1].len);
                            int lid = to_int(fields[2].s, fields[2].len);
                            int tid = to_int(fields[3].s, fields[3].len);

                            buf += '@';
                            if (prefix_constant) buf += constant_prefix;
                            buf += '.'; buf += std::to_string(read_num); buf += ' ';
                            if (instrument_constant) buf += constant_instrument;
                            buf += ':'; buf += run_dict[rid];
                            buf += ':'; buf += fc_dict[fid];
                            buf += ':'; buf += lane_dict[lid];
                            buf += ':'; buf += tile_dict[tid];
                            buf += ':'; buf.append(fields[4].s, fields[4].len);
                            buf += ':'; buf.append(fields[5].s, fields[5].len);
                            buf += '\n';
                            buf.append(fields[6].s, fields[6].len); buf += '\n';
                            buf += "+\n";
                            buf.append(fields[7].s, fields[7].len); buf += '\n';
                        }
                    }
                });
            }
            for (auto& t : threads) t.join();
        }

        int out_fd = open(output_path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
        for (auto& buf : buffers) {
            const char* p = buf.data(); size_t rem = buf.size();
            while (rem > 0) { ssize_t w = write(out_fd, p, rem); p += w; rem -= w; }
        }
        close(out_fd);
        munmap((void*)data, file_size);
        close(fd);
        std::cerr << "Decoded " << nrecs << " records\n";
    }

    return 0;
}
