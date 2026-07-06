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

#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>

static const char MAGIC[] = "FQPP02"; // Bumped magic version for new flags

// DNA Base Mappings
inline int map_base(char c) {
    switch(c) {
        case 'A': case 'a': return 0;
        case 'C': case 'c': return 1;
        case 'G': case 'g': return 2;
        case 'T': case 't': return 3;
        default: return 4; // N or unknown
    }
}
inline char unmap_base(int v) { return "ACGTN"[v]; }

// Strict mapping for 3-bit (ATCG only)
inline int map_base_strict(char c) {
    switch(c) {
        case 'A': case 'a': return 0;
        case 'C': case 'c': return 1;
        case 'G': case 'g': return 2;
        case 'T': case 't': return 3;
        default: return -1;
    }
}

// Base64 Alphabet for TSV-safe 3-bit packing
static const char b64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-";
static int b64_rev[256];
void init_b64() {
    for (int i = 0; i < 256; i++) b64_rev[i] = -1;
    for (int i = 0; i < 64; i++) b64_rev[(uint8_t)b64[i]] = i;
}

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
    const char* pair_start;     int pair_len;
    bool parsed;
};

static ParsedHeader parse_header_fast(const char* line, int len) {
    ParsedHeader h{};
    h.parsed = false;
    if (len < 3 || line[0] != '@') return h;

    int sp = -1, dot = -1;
    for (int i = 1; i < len; i++) if (line[i] == ' ') { sp = i; break; }
    if (sp < 0) return h;
    for (int i = sp - 1; i >= 1; i--) if (line[i] == '.') { dot = i; break; }
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

    int colons[6]; int ncol = 0;
    for (int i = 0; i < info_len && ncol < 6; i++) if (info[i] == ':') colons[ncol++] = i;
    if (ncol < 6) return h;

    h.instr_start = info;           h.instr_len = colons[0];
    h.run_start = info+colons[0]+1; h.run_len = colons[1]-colons[0]-1;
    h.fc_start = info+colons[1]+1;  h.fc_len = colons[2]-colons[1]-1;
    h.lane_start = info+colons[2]+1;h.lane_len = colons[3]-colons[2]-1;
    h.tile_start = info+colons[3]+1;h.tile_len = colons[4]-colons[3]-1;
    h.x_start = info+colons[4]+1;   h.x_len = colons[5]-colons[4]-1;
    h.y_start = info+colons[5]+1;   h.y_len = info_len-colons[5]-1;

    h.pair_start = nullptr; h.pair_len = 0;
    for (int i = 0; i < h.y_len; i++) {
        if (h.y_start[i] == '/') {
            h.pair_start = h.y_start + i;
            h.pair_len = h.y_len - i;
            h.y_len = i;
            break;
        }
    }
    h.parsed = true;
    return h;
}

static inline std::string sv(const char* s, int len) { return std::string(s, len); }

int main(int argc, char** argv) {
    if (argc < 4) {
        std::cerr << "Usage: fastq_preprocess <encode|decode> <input> <output> [threads] [--pack-4bit | --pack-3bit]\n";
        return 1;
    }

    init_b64();
    std::string mode = argv[1];
    std::string input_path = argv[2];
    std::string output_path = argv[3];
    int nthreads = 1;
    bool pack_4bit = false;
    bool pack_3bit = false;

    for(int i = 4; i < argc; i++) {
        std::string arg = argv[i];
        if (arg == "--pack-4bit") pack_4bit = true;
        else if (arg == "--pack-3bit") pack_3bit = true;
        else nthreads = std::atoi(argv[i]);
    }
    if (nthreads < 1) nthreads = 1;
    if (pack_3bit && pack_4bit) {
        std::cerr << "Cannot use both --pack-4bit and --pack-3bit.\n"; return 1;
    }

    if (mode == "encode") {
        int fd = open(input_path.c_str(), O_RDONLY);
        if (fd < 0) { std::cerr << "Cannot open " << input_path << "\n"; return 1; }
        struct stat st; fstat(fd, &st);
        size_t file_size = st.st_size;
        const char* data = (const char*)mmap(nullptr, file_size, PROT_READ, MAP_PRIVATE | MAP_POPULATE, fd, 0);
        madvise((void*)data, file_size, MADV_SEQUENTIAL);

        std::vector<std::vector<size_t>> thread_newlines(nthreads);
        size_t byte_chunk = (file_size + nthreads - 1) / nthreads;
        std::vector<std::thread> threads;
        for (int t = 0; t < nthreads; t++) {
            size_t blo = t * byte_chunk, bhi = std::min(blo + byte_chunk, file_size);
            threads.emplace_back([&, t, blo, bhi]() {
                auto& nl = thread_newlines[t];
                nl.reserve((bhi - blo) / 40);
                for (size_t i = blo; i < bhi; i++) if (data[i] == '\n' && i + 1 < file_size) nl.push_back(i + 1);
            });
        }
        for (auto& t : threads) t.join();

        std::vector<size_t> line_starts; line_starts.push_back(0);
        for (auto& v : thread_newlines) { line_starts.insert(line_starts.end(), v.begin(), v.end()); v.clear(); }
        
        size_t nrecs = line_starts.size() / 4;
        if (nrecs == 0) return 1;

        auto get_line = [&](size_t idx, int& len) -> const char* {
            size_t start = line_starts[idx], end = (idx + 1 < line_starts.size()) ? line_starts[idx + 1] - 1 : file_size;
            while (end > start && (data[end-1] == '\r' || data[end-1] == '\n')) end--;
            len = (int)(end - start);
            return data + start;
        };

        std::vector<ParsedHeader> headers(nrecs);
        threads.clear();
        size_t chunk = (nrecs + nthreads - 1) / nthreads;
        for (int t = 0; t < nthreads; t++) {
            size_t lo = t * chunk, hi = std::min(lo + chunk, nrecs);
            threads.emplace_back([&, lo, hi]() {
                for (size_t i = lo; i < hi; i++) {
                    int len; const char* hdr = get_line(i * 4, len);
                    headers[i] = parse_header_fast(hdr, len);
                }
            });
        }
        for (auto& t : threads) t.join();

        size_t parsed_count = 0;
        for (size_t i = 0; i < nrecs; i++) if (headers[i].parsed) parsed_count++;
        
        // GUARDRAIL: Reject heavily non-Illumina files (e.g. SRR1770413_1)
        double parse_ratio = (double)parsed_count / nrecs;
        if (parse_ratio < 0.5) {
            std::cerr << "[ERROR] Only " << (parse_ratio * 100) << "% of reads matched Illumina formats.\n";
            std::cerr << "Non-Illumina or unrecognized FASTQ format detected. Aborting.\n";
            return 1;
        }

        // ENCODE-TIME VERIFICATION: Ensure strict 1:1 lossless state.
        bool all_parsed = (parsed_count == nrecs);
        if (all_parsed) {
            for (size_t i = 0; i < nrecs; i++) {
                // 1. Check for standard sequential read numbering
                if (headers[i].read_num != (int64_t)(i + 1)) { all_parsed = false; break; }
                
                // 2. Check strict '+' separator (no extra characters)
                int plus_len; const char* plus = get_line(i * 4 + 2, plus_len);
                if (plus_len != 1 || plus[0] != '+') { all_parsed = false; break; }

                // 3. Base strictness validation
                if (pack_4bit || pack_3bit) {
                    int seq_len; const char* seq = get_line(i * 4 + 1, seq_len);
                    for (int j = 0; j < seq_len; j++) {
                        if (pack_4bit && map_base(seq[j]) == 4 && seq[j] != 'N' && seq[j] != 'n') {
                            all_parsed = false; break; // Ambiguous non-N found
                        }
                        if (pack_3bit && map_base_strict(seq[j]) == -1) {
                            all_parsed = false; break; // Found N or ambiguous base
                        }
                    }
                }
                if (!all_parsed) break;
            }
        }

        std::string constant_prefix, constant_instrument, constant_pair_suffix;
        bool prefix_constant = true, instrument_constant = true, pair_suffix_constant = true, has_pair_suffix = false;
        std::unordered_set<std::string> run_vals, fc_vals, lane_vals, tile_vals;

        if (all_parsed) {
            auto& h0 = headers[0];
            constant_prefix = sv(h0.prefix_start, h0.prefix_len);
            constant_instrument = sv(h0.instr_start, h0.instr_len);
            if (h0.pair_len > 0) { has_pair_suffix = true; constant_pair_suffix = sv(h0.pair_start, h0.pair_len); }
            for (size_t i = 0; i < nrecs; i++) {
                auto& h = headers[i];
                if (sv(h.prefix_start, h.prefix_len) != constant_prefix) prefix_constant = false;
                if (sv(h.instr_start, h.instr_len) != constant_instrument) instrument_constant = false;
                run_vals.insert(sv(h.run_start, h.run_len)); fc_vals.insert(sv(h.fc_start, h.fc_len));
                lane_vals.insert(sv(h.lane_start, h.lane_len)); tile_vals.insert(sv(h.tile_start, h.tile_len));
                if (has_pair_suffix) {
                    if (h.pair_len == 0 || sv(h.pair_start, h.pair_len) != constant_pair_suffix) pair_suffix_constant = false;
                } else if (h.pair_len > 0) { has_pair_suffix = true; pair_suffix_constant = false; }
            }
        }

        auto build_dict = [](const std::unordered_set<std::string>& vals) -> std::pair<std::vector<std::string>, std::unordered_map<std::string, int>> {
            std::vector<std::string> sv(vals.begin(), vals.end()); std::sort(sv.begin(), sv.end());
            std::unordered_map<std::string, int> m; for (size_t i = 0; i < sv.size(); i++) m[sv[i]] = (int)i;
            return {sv, m};
        };
        auto [run_dict, run_map] = build_dict(run_vals);
        auto [fc_dict, fc_map] = build_dict(fc_vals);
        auto [lane_dict, lane_map] = build_dict(lane_vals);
        auto [tile_dict, tile_map] = build_dict(tile_vals);

        std::string meta_path = output_path + ".meta";
        std::ofstream fmeta(meta_path, std::ios::binary);
        fmeta.write(MAGIC, 6);
        int32_t nr = (int32_t)nrecs; fmeta.write((char*)&nr, 4);

        uint8_t flags = 0;
        if (all_parsed) flags |= 1;
        if (prefix_constant) flags |= 2;
        if (instrument_constant) flags |= 4;
        if (has_pair_suffix && pair_suffix_constant) flags |= 16;
        if (pack_4bit) flags |= 32; 
        if (pack_3bit) flags |= 64; 
        fmeta.write((char*)&flags, 1);

        auto write_str = [&](const std::string& s) {
            int16_t len = (int16_t)s.size(); fmeta.write((char*)&len, 2); fmeta.write(s.c_str(), len);
        };
        if (prefix_constant) write_str(constant_prefix);
        if (instrument_constant) write_str(constant_instrument);
        if (has_pair_suffix && pair_suffix_constant) write_str(constant_pair_suffix);

        auto write_dict_fn = [&](const std::vector<std::string>& dict) {
            int32_t dsize = (int32_t)dict.size(); fmeta.write((char*)&dsize, 4);
            for (auto& s : dict) write_str(s);
        };
        if (all_parsed) { write_dict_fn(run_dict); write_dict_fn(fc_dict); write_dict_fn(lane_dict); write_dict_fn(tile_dict); }
        
        uint32_t marker = 0xDEADBEEF; fmeta.write((char*)&marker, 4); fmeta.close();

        std::string tsv_path = output_path + ".tsv";
        std::vector<std::string> buffers(nthreads);
        threads.clear();
        for (int t = 0; t < nthreads; t++) {
            size_t lo = t * chunk, hi = std::min(lo + chunk, nrecs);
            threads.emplace_back([&, t, lo, hi]() {
                std::string& buf = buffers[t]; buf.reserve((hi - lo) * 120);
                for (size_t i = lo; i < hi; i++) {
                    int seq_len, qual_len;
                    const char* seq = get_line(i * 4 + 1, seq_len);
                    const char* qual = get_line(i * 4 + 3, qual_len);

                    auto append_seq = [&]() {
                        int packed_len = 0;
                        if (pack_4bit && all_parsed) {
                            for (int j = 0; j < seq_len; j += 2) {
                                int b1 = map_base(seq[j]);
                                int b2 = (j + 1 < seq_len) ? map_base(seq[j + 1]) : 0;
                                buf += (char)('A' + b1 * 5 + b2);
                                packed_len++;
                            }
                        } else if (pack_3bit && all_parsed) {
                            for (int j = 0; j < seq_len; j += 3) {
                                int b1 = map_base_strict(seq[j]);
                                int b2 = (j + 1 < seq_len) ? map_base_strict(seq[j + 1]) : 0;
                                int b3 = (j + 2 < seq_len) ? map_base_strict(seq[j + 2]) : 0;
                                buf += b64[(b1 << 4) | (b2 << 2) | b3];
                                packed_len++;
                            }
                        } else {
                            buf.append(seq, seq_len);
                            packed_len = seq_len; // raw length
                        }
                        
                        // FIX: Pad chunk length for OpenZL convert_serial_to_num_be16 node
                        if ((pack_4bit || pack_3bit) && all_parsed && packed_len % 2 != 0) {
                            buf += 'A';
                        }
                    };

                    if (!all_parsed) {
                        int hlen; const char* hdr = get_line(i * 4, hlen);
                        buf.append(hdr + 1, hlen - 1); buf += '\t';
                        append_seq(); buf += '\t';
                        
                        // Must also capture exact '+' line in raw mode to ensure perfect 1:1
                        int plus_len; const char* plus = get_line(i * 4 + 2, plus_len);
                        buf.append(plus, plus_len); buf += '\t';
                        buf.append(qual, qual_len); buf += '\n';
                    } else {
                        auto& h = headers[i];
                        buf += std::to_string(run_map.find(sv(h.run_start, h.run_len))->second); buf += '\t';
                        buf += std::to_string(fc_map.find(sv(h.fc_start, h.fc_len))->second); buf += '\t';
                        buf += std::to_string(lane_map.find(sv(h.lane_start, h.lane_len))->second); buf += '\t';
                        buf += std::to_string(tile_map.find(sv(h.tile_start, h.tile_len))->second); buf += '\t';
                        buf.append(h.x_start, h.x_len); buf += '\t';
                        buf.append(h.y_start, h.y_len); buf += '\t';
                        append_seq(); buf += '\t';
                        buf.append(qual, qual_len); buf += '\n';
                    }
                }
            });
        }
        for (auto& t : threads) t.join();

        int out_fd = open(tsv_path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
        for (auto& buf : buffers) {
            const char* p = buf.data(); size_t remaining = buf.size();
            while (remaining > 0) { ssize_t w = write(out_fd, p, remaining); p += w; remaining -= w; }
        }
        close(out_fd); munmap((void*)data, file_size); close(fd);
    } else if (mode == "decode") {
        std::string meta_path = input_path + ".meta", tsv_path = input_path + ".tsv";
        std::ifstream fmeta(meta_path, std::ios::binary);
        char magic[7] = {}; fmeta.read(magic, 6);
        int32_t nrecs; fmeta.read((char*)&nrecs, 4);

        uint8_t flags; fmeta.read((char*)&flags, 1);
        bool all_parsed = flags & 1, prefix_constant = flags & 2, instrument_constant = flags & 4;
        bool has_pair_suffix = flags & 16;
        bool pack_4bit = flags & 32;
        bool pack_3bit = flags & 64;

        auto read_str = [&]() -> std::string { int16_t len; fmeta.read((char*)&len, 2); std::string s(len, '\0'); fmeta.read(&s[0], len); return s; };
        std::string constant_prefix, constant_instrument, constant_pair_suffix;
        if (prefix_constant) constant_prefix = read_str();
        if (instrument_constant) constant_instrument = read_str();
        if (has_pair_suffix) constant_pair_suffix = read_str();

        auto read_dict = [&]() -> std::vector<std::string> {
            int32_t dsize; fmeta.read((char*)&dsize, 4);
            std::vector<std::string> dict(dsize); for (int i = 0; i < dsize; i++) dict[i] = read_str();
            return dict;
        };
        std::vector<std::string> run_dict, fc_dict, lane_dict, tile_dict;
        if (all_parsed) { run_dict = read_dict(); fc_dict = read_dict(); lane_dict = read_dict(); tile_dict = read_dict(); }

        uint32_t marker; fmeta.read((char*)&marker, 4); fmeta.close();

        int fd = open(tsv_path.c_str(), O_RDONLY);
        struct stat fst; fstat(fd, &fst);
        size_t file_size = fst.st_size;
        const char* data = (const char*)mmap(nullptr, file_size, PROT_READ, MAP_PRIVATE | MAP_POPULATE, fd, 0);

        std::vector<std::vector<size_t>> tnl(nthreads);
        size_t bc = (file_size + nthreads - 1) / nthreads;
        std::vector<std::thread> threads;
        for (int t = 0; t < nthreads; t++) {
            size_t blo = t * bc, bhi = std::min(blo + bc, file_size);
            threads.emplace_back([&, t, blo, bhi]() {
                tnl[t].reserve((bhi - blo) / 40);
                for (size_t i = blo; i < bhi; i++) if (data[i] == '\n' && i + 1 < file_size) tnl[t].push_back(i + 1);
            });
        }
        for (auto& t : threads) t.join();

        std::vector<size_t> line_starts; line_starts.push_back(0);
        for (auto& v : tnl) { line_starts.insert(line_starts.end(), v.begin(), v.end()); v.clear(); }

        auto get_line = [&](size_t idx, int& len) -> const char* {
            size_t start = line_starts[idx], end = (idx + 1 < line_starts.size()) ? line_starts[idx + 1] - 1 : file_size;
            while (end > start && (data[end-1] == '\r' || data[end-1] == '\n')) end--;
            len = (int)(end - start); return data + start;
        };

        struct FieldSlice { const char* s; int len; };
        auto split_tabs = [](const char* line, int len, FieldSlice* out, int mx) -> int {
            int nf = 0, start = 0;
            for (int i = 0; i <= len && nf < mx; i++) {
                if (i == len || line[i] == '\t') { out[nf++] = {line + start, i - start}; start = i + 1; }
            }
            return nf;
        };

        size_t nlines_actual = std::min(line_starts.size(), (size_t)nrecs);
        std::vector<std::string> buffers(nthreads);
        threads.clear();
        size_t chunk = (nlines_actual + nthreads - 1) / nthreads;
        for (int t = 0; t < nthreads; t++) {
            size_t lo = t * chunk, hi = std::min(lo + chunk, nlines_actual);
            threads.emplace_back([&, t, lo, hi]() {
                std::string& buf = buffers[t]; buf.reserve((hi - lo) * 160);
                for (size_t i = lo; i < hi; i++) {
                    int len; const char* line = get_line(i, len);
                    FieldSlice fields[10]; int num_fields = split_tabs(line, len, fields, 10);

                    auto decode_seq = [&](const FieldSlice& seq_fs, const FieldSlice& qual_fs) {
                        if (pack_4bit && all_parsed) {
                            int target_len = qual_fs.len; 
                            int bases_decoded = 0;
                            // Target bounds prevent dummy odd-byte pads from being parsed
                            for (int j = 0; j < seq_fs.len && bases_decoded < target_len; j++) {
                                int val = seq_fs.s[j] - 'A';
                                buf += unmap_base(val / 5); bases_decoded++;
                                if (bases_decoded < target_len) {
                                    buf += unmap_base(val % 5); bases_decoded++;
                                }
                            }
                        } else if (pack_3bit && all_parsed) {
                            int target_len = qual_fs.len;
                            int bases_decoded = 0;
                            for (int j = 0; j < seq_fs.len && bases_decoded < target_len; j++) {
                                int val = b64_rev[(uint8_t)seq_fs.s[j]];
                                buf += unmap_base((val >> 4) & 3); bases_decoded++;
                                if (bases_decoded < target_len) { buf += unmap_base((val >> 2) & 3); bases_decoded++; }
                                if (bases_decoded < target_len) { buf += unmap_base(val & 3); bases_decoded++; }
                            }
                        } else {
                            buf.append(seq_fs.s, seq_fs.len);
                        }
                    };

                    if (!all_parsed) {
                        // Raw mode explicitly restored for 1:1 identical md5 sums
                        buf += '@'; buf.append(fields[0].s, fields[0].len); buf += '\n';
                        decode_seq(fields[1], fields[3]); buf += '\n';
                        buf.append(fields[2].s, fields[2].len); buf += '\n';
                        buf.append(fields[3].s, fields[3].len); buf += '\n';
                    } else {
                        auto to_int = [](const char* s, int l) { int v=0; for(int i=0;i<l;i++) v=v*10+(s[i]-'0'); return v; };
                        buf += '@'; if (prefix_constant) buf += constant_prefix;
                        buf += '.'; buf += std::to_string(i + 1); buf += ' ';
                        if (instrument_constant) buf += constant_instrument;
                        buf += ':'; buf += run_dict[to_int(fields[0].s, fields[0].len)];
                        buf += ':'; buf += fc_dict[to_int(fields[1].s, fields[1].len)];
                        buf += ':'; buf += lane_dict[to_int(fields[2].s, fields[2].len)];
                        buf += ':'; buf += tile_dict[to_int(fields[3].s, fields[3].len)];
                        buf += ':'; buf.append(fields[4].s, fields[4].len);
                        buf += ':'; buf.append(fields[5].s, fields[5].len);
                        if (has_pair_suffix) buf += constant_pair_suffix; buf += '\n';
                        
                        decode_seq(fields[6], fields[7]); buf += "\n+\n";
                        buf.append(fields[7].s, fields[7].len); buf += '\n';
                    }
                }
            });
        }
        for (auto& t : threads) t.join();

        int out_fd = open(output_path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
        for (auto& buf : buffers) {
            const char* p = buf.data(); size_t rem = buf.size();
            while (rem > 0) { ssize_t w = write(out_fd, p, rem); p += w; rem -= w; }
        }
        close(out_fd); munmap((void*)data, file_size); close(fd);
    }
    return 0;
}
