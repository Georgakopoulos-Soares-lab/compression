/*
 * bed_preprocess.cpp — Generic BED/table preprocessor for compression
 *
 * Makes BED-like tab-delimited genomic files more compressible by:
 *   1. Delta-encoding sorted integer coordinate columns (start → deltas)
 *   2. Replacing end with span (end - start) when end column detected
 *   3. Dropping fully-redundant columns (e.g. genoLeft = -(chromLen - end))
 *   4. Dictionary-encoding low-cardinality string columns as integers
 *
 * The preprocessor auto-detects column types and applies transforms.
 * It writes a small binary header (.meta) so decode can reconstruct exactly,
 * and a pure TSV body (.tsv) for the CSV profiler.
 *
 * Usage:
 *   bed_preprocess encode  <input.tsv> <output_prefix> [--group-col N]
 *     → produces <output_prefix>.meta and <output_prefix>.tsv
 *   bed_preprocess decode  <output_prefix> <output.tsv>
 *     → reads <output_prefix>.meta and <output_prefix>.tsv, writes original
 *
 * --group-col N : column index (0-based) that groups/sorts the data
 *                 (e.g. chromosome column). Delta resets at group boundaries.
 *                 If not specified, auto-detected as first string column
 *                 that looks like a chrom.
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
#include <climits>

static const char MAGIC[] = "BEDPP01";

struct ColInfo {
    bool is_numeric;       // all values parse as int64
    bool is_string;
    int64_t min_val, max_val;
    size_t cardinality;    // distinct string values (sampled)
    bool delta_candidate;  // sorted integers within groups?
    bool span_candidate;   // = prev_col + span?
    bool drop;             // redundant column
    int transform;         // 0=raw, 1=delta, 2=span, 3=dict, 4=drop
};

// Parse a line into tab-separated fields
static void split_tabs(const std::string& line, std::vector<std::string>& out) {
    out.clear();
    size_t start = 0;
    for (size_t i = 0; i <= line.size(); i++) {
        if (i == line.size() || line[i] == '\t') {
            out.push_back(line.substr(start, i - start));
            start = i + 1;
        }
    }
}

static bool try_parse_int(const std::string& s, int64_t& val) {
    if (s.empty()) return false;
    char* end;
    val = strtoll(s.c_str(), &end, 10);
    return *end == '\0';
}

int main(int argc, char** argv) {
    if (argc < 4) {
        std::cerr << "Usage: bed_preprocess encode|decode <input> <output_prefix|output.tsv> [--group-col N]\n";
        return 1;
    }

    std::string mode = argv[1];
    std::string input_path = argv[2];
    std::string output_path = argv[3];

    int group_col = -1; // auto-detect
    for (int i = 4; i < argc; i++) {
        if (std::string(argv[i]) == "--group-col" && i + 1 < argc) {
            group_col = std::stoi(argv[++i]);
        }
    }

    if (mode == "encode") {
        // ---- PASS 1: Read all lines, analyze columns ----
        std::ifstream fin(input_path);
        if (!fin) { std::cerr << "Cannot open " << input_path << "\n"; return 1; }

        std::vector<std::vector<std::string>> rows;
        std::string line;
        int ncols = 0;

        while (std::getline(fin, line)) {
            if (line.empty()) continue;
            std::vector<std::string> fields;
            split_tabs(line, fields);
            if (ncols == 0) ncols = (int)fields.size();
            rows.push_back(std::move(fields));
        }
        fin.close();

        size_t nrows = rows.size();
        std::cerr << "Read " << nrows << " rows, " << ncols << " cols\n";

        // Analyze columns
        std::vector<ColInfo> cols(ncols);
        std::vector<std::unordered_set<std::string>> distinct_vals(ncols);

        for (int c = 0; c < ncols; c++) {
            cols[c].is_numeric = true;
            cols[c].is_string = false;
            cols[c].min_val = INT64_MAX;
            cols[c].max_val = INT64_MIN;
            cols[c].delta_candidate = false;
            cols[c].span_candidate = false;
            cols[c].drop = false;
            cols[c].transform = 0;
        }

        // Analyze all rows for column statistics
        size_t sample_n = nrows;  // Use all rows for analysis
        for (size_t r = 0; r < sample_n; r++) {
            for (int c = 0; c < ncols; c++) {
                int64_t v;
                if (try_parse_int(rows[r][c], v)) {
                    if (v < cols[c].min_val) cols[c].min_val = v;
                    if (v > cols[c].max_val) cols[c].max_val = v;
                } else {
                    cols[c].is_numeric = false;
                    cols[c].is_string = true;
                }
                if (distinct_vals[c].size() < 100000) {
                    distinct_vals[c].insert(rows[r][c]);
                }
            }
        }
        for (int c = 0; c < ncols; c++) {
            cols[c].cardinality = distinct_vals[c].size();
        }

        // Auto-detect group column (chrom-like: string, low cardinality, contains "chr")
        if (group_col < 0) {
            for (int c = 0; c < ncols; c++) {
                if (cols[c].is_string && cols[c].cardinality < 10000) {
                    // Check if values look like chromosomes
                    bool has_chr = false;
                    for (auto& v : distinct_vals[c]) {
                        if (v.find("chr") != std::string::npos) { has_chr = true; break; }
                    }
                    if (has_chr) { group_col = c; break; }
                }
            }
        }
        std::cerr << "Group column: " << group_col << "\n";

        // Detect delta candidates: numeric, nearly-sorted within groups
        // Allow up to 0.1% violations (tiny reversals common in genomic data)
        for (int c = 0; c < ncols; c++) {
            if (!cols[c].is_numeric) continue;
            size_t violations = 0;
            size_t comparisons = 0;
            std::string prev_group = "";
            int64_t prev_val = INT64_MIN;
            for (size_t r = 0; r < sample_n; r++) {
                std::string grp = (group_col >= 0) ? rows[r][group_col] : "";
                int64_t v;
                try_parse_int(rows[r][c], v);
                if (grp != prev_group) {
                    prev_group = grp;
                    prev_val = v;
                } else {
                    comparisons++;
                    if (v < prev_val) violations++;
                    prev_val = v;
                }
            }
            double viol_rate = (comparisons > 0) ? (double)violations / comparisons : 1.0;
            if (viol_rate < 0.001 && cols[c].max_val - cols[c].min_val > 10000) {
                cols[c].delta_candidate = true;
                if (violations > 0) {
                    std::cerr << "  col " << c << ": nearly-sorted (" << violations
                              << " violations / " << comparisons << " = "
                              << (viol_rate * 100) << "%), will delta-encode\n";
                }
            }
        }

        // Detect span candidates: col[c] = col[c-1] + small_positive for each row
        for (int c = 1; c < ncols; c++) {
            if (!cols[c].is_numeric || !cols[c-1].is_numeric) continue;
            if (!cols[c-1].delta_candidate) continue;
            bool is_span = true;
            for (size_t r = 0; r < sample_n && is_span; r++) {
                int64_t v, vp;
                try_parse_int(rows[r][c], v);
                try_parse_int(rows[r][c-1], vp);
                int64_t diff = v - vp;
                if (diff < 0 || diff > 1000000) is_span = false;
            }
            if (is_span) {
                cols[c].span_candidate = true;
            }
        }

        // Detect droppable columns: column that = -(constant - col[c-1])
        // i.e. col[c] + col[c-1] = constant within each group
        for (int c = 1; c < ncols; c++) {
            if (!cols[c].is_numeric) continue;
            // Check if col[c] + some other col = constant within group
            for (int c2 = 0; c2 < c; c2++) {
                if (!cols[c2].is_numeric) continue;
                bool is_redundant = true;
                std::unordered_map<std::string, int64_t> group_sums;
                for (size_t r = 0; r < sample_n && is_redundant; r++) {
                    std::string grp = (group_col >= 0) ? rows[r][group_col] : "";
                    int64_t va, vb;
                    try_parse_int(rows[r][c], va);
                    try_parse_int(rows[r][c2], vb);
                    int64_t sum = va + vb;
                    auto it = group_sums.find(grp);
                    if (it == group_sums.end()) {
                        group_sums[grp] = sum;
                    } else if (it->second != sum) {
                        is_redundant = false;
                    }
                }
                if (is_redundant && group_sums.size() > 0) {
                    cols[c].drop = true;
                    std::cerr << "Col " << c << " is redundant (sum with col " << c2 << " = const per group)\n";
                    break;
                }
            }
        }

        // Assign transforms
        int first_delta_col = -1;
        for (int c = 0; c < ncols; c++) {
            if (cols[c].drop) {
                cols[c].transform = 4; // drop
            } else if (cols[c].span_candidate) {
                cols[c].transform = 2; // span
            } else if (cols[c].delta_candidate) {
                cols[c].transform = 1; // delta
                if (first_delta_col < 0) first_delta_col = c;
            } else if ((cols[c].is_string && cols[c].cardinality <= 20000) ||
                       (!cols[c].is_string && cols[c].cardinality <= 50)) {
                cols[c].transform = 3; // dict
            } else {
                cols[c].transform = 0; // raw
            }
        }

        // Print analysis
        for (int c = 0; c < ncols; c++) {
            const char* tname[] = {"raw", "delta", "span", "dict", "drop"};
            std::cerr << "  col[" << c << "]: "
                      << (cols[c].is_numeric ? "int" : "str")
                      << " card=" << cols[c].cardinality
                      << " transform=" << tname[cols[c].transform]
                      << "\n";
        }

        // Build dictionaries for dict-encoded columns
        std::vector<std::vector<std::string>> dictionaries(ncols);
        std::vector<std::unordered_map<std::string, int>> dict_maps(ncols);
        for (int c = 0; c < ncols; c++) {
            if (cols[c].transform != 3) continue;
            std::vector<std::string> vals(distinct_vals[c].begin(), distinct_vals[c].end());
            // Check full data for any values not in sample
            for (size_t r = sample_n; r < nrows; r++) {
                if (distinct_vals[c].find(rows[r][c]) == distinct_vals[c].end()) {
                    vals.push_back(rows[r][c]);
                    distinct_vals[c].insert(rows[r][c]);
                }
            }
            // Sort: numeric columns by value, string columns alphabetically
            if (cols[c].is_numeric) {
                std::sort(vals.begin(), vals.end(), [](const std::string& a, const std::string& b) {
                    return std::stoll(a) < std::stoll(b);
                });
            } else {
                std::sort(vals.begin(), vals.end());
            }
            dictionaries[c] = vals;
            for (size_t i = 0; i < vals.size(); i++) {
                dict_maps[c][vals[i]] = (int)i;
            }
        }

        // ---- PASS 2: Write output (meta + tsv) ----
        std::string meta_path = output_path + ".meta";
        std::string tsv_path = output_path + ".tsv";

        std::ofstream fmeta(meta_path, std::ios::binary);
        if (!fmeta) { std::cerr << "Cannot open " << meta_path << "\n"; return 1; }
        std::ofstream ftsv(tsv_path);
        if (!ftsv) { std::cerr << "Cannot open " << tsv_path << "\n"; return 1; }

        // Write binary header to .meta
        fmeta.write(MAGIC, 7);
        int32_t nc = ncols;
        int32_t gc = group_col;
        int32_t nr = (int32_t)nrows;
        fmeta.write((char*)&nc, 4);
        fmeta.write((char*)&gc, 4);
        fmeta.write((char*)&nr, 4);

        // Write per-column transform info
        for (int c = 0; c < ncols; c++) {
            uint8_t t = (uint8_t)cols[c].transform;
            fmeta.write((char*)&t, 1);
        }

        // Write dictionaries
        for (int c = 0; c < ncols; c++) {
            if (cols[c].transform != 3) continue;
            int32_t dsize = (int32_t)dictionaries[c].size();
            fmeta.write((char*)&dsize, 4);
            for (auto& s : dictionaries[c]) {
                int16_t len = (int16_t)s.size();
                fmeta.write((char*)&len, 2);
                fmeta.write(s.c_str(), len);
            }
        }

        // Write group->chromLen mapping for redundant column reconstruction
        // (store the constant sums per group for dropped columns)
        int n_dropped = 0;
        for (int c = 0; c < ncols; c++) if (cols[c].transform == 4) n_dropped++;
        fmeta.write((char*)&n_dropped, 4);

        // For each dropped column, store its partner and the per-group sums
        for (int c = 0; c < ncols; c++) {
            if (cols[c].transform != 4) continue;
            // Find partner
            int partner = -1;
            for (int c2 = 0; c2 < c; c2++) {
                if (!cols[c2].is_numeric) continue;
                bool ok = true;
                std::unordered_map<std::string, int64_t> gsums;
                for (size_t r = 0; r < nrows && ok; r++) {
                    std::string grp = (group_col >= 0) ? rows[r][group_col] : "";
                    int64_t va, vb;
                    try_parse_int(rows[r][c], va);
                    try_parse_int(rows[r][c2], vb);
                    int64_t sum = va + vb;
                    auto it = gsums.find(grp);
                    if (it == gsums.end()) gsums[grp] = sum;
                    else if (it->second != sum) ok = false;
                }
                if (ok) { partner = c2; break; }
            }
            // Could not find partner in full data, keep raw
            if (partner < 0) {
                // fallback: write col_idx=-1 meaning "no drop"
                int32_t ci = -1;
                fmeta.write((char*)&ci, 4);
                continue;
            }
            int32_t ci = c;
            int32_t pi = partner;
            fmeta.write((char*)&ci, 4);
            fmeta.write((char*)&pi, 4);
            // Collect per-group sums
            std::unordered_map<std::string, int64_t> gsums;
            for (size_t r = 0; r < nrows; r++) {
                std::string grp = (group_col >= 0) ? rows[r][group_col] : "";
                int64_t va, vb;
                try_parse_int(rows[r][c], va);
                try_parse_int(rows[r][partner], vb);
                gsums[grp] = va + vb;
            }
            int32_t ng = (int32_t)gsums.size();
            fmeta.write((char*)&ng, 4);
            for (auto& kv : gsums) {
                int16_t klen = (int16_t)kv.first.size();
                fmeta.write((char*)&klen, 2);
                fmeta.write(kv.first.c_str(), klen);
                int64_t sv = kv.second;
                fmeta.write((char*)&sv, 8);
            }
        }

        // Mark end of meta
        uint32_t header_end_marker = 0xDEADBEEF;
        fmeta.write((char*)&header_end_marker, 4);
        fmeta.close();

        // Write transformed TSV data
        std::string prev_group;
        std::vector<int64_t> prev_vals(ncols, 0);

        for (size_t r = 0; r < nrows; r++) {
            std::string grp = (group_col >= 0) ? rows[r][group_col] : "";
            if (grp != prev_group) {
                prev_group = grp;
                std::fill(prev_vals.begin(), prev_vals.end(), 0);
            }

            bool first = true;
            for (int c = 0; c < ncols; c++) {
                if (cols[c].transform == 4) continue; // dropped

                if (!first) ftsv.put('\t');
                first = false;

                int64_t v;
                switch (cols[c].transform) {
                    case 0: // raw
                        ftsv << rows[r][c];
                        break;
                    case 1: // delta
                        try_parse_int(rows[r][c], v);
                        ftsv << (v - prev_vals[c]);
                        prev_vals[c] = v;
                        break;
                    case 2: // span (end - start = end - prev_col_original)
                    {
                        int64_t end_v;
                        try_parse_int(rows[r][c], end_v);
                        int64_t start_v;
                        try_parse_int(rows[r][c-1], start_v);
                        ftsv << (end_v - start_v);
                        break;
                    }
                    case 3: // dict
                        ftsv << dict_maps[c][rows[r][c]];
                        break;
                }
            }
            ftsv.put('\n');
        }

        ftsv.close();
        std::cerr << "Done encoding " << nrows << " rows\n";

    } else if (mode == "decode") {
        // ---- DECODE ----
        // input_path is the prefix; read .meta and .tsv
        std::string meta_path = input_path + ".meta";
        std::string tsv_path = input_path + ".tsv";

        std::ifstream fmeta(meta_path, std::ios::binary);
        if (!fmeta) { std::cerr << "Cannot open " << meta_path << "\n"; return 1; }

        char magic[8] = {};
        fmeta.read(magic, 7);
        if (std::string(magic) != MAGIC) {
            std::cerr << "Bad magic: " << magic << "\n"; return 1;
        }

        int32_t ncols, group_col_v, nrows_v;
        fmeta.read((char*)&ncols, 4);
        fmeta.read((char*)&group_col_v, 4);
        fmeta.read((char*)&nrows_v, 4);

        std::vector<uint8_t> transforms(ncols);
        fmeta.read((char*)transforms.data(), ncols);

        // Read dictionaries
        std::vector<std::vector<std::string>> dictionaries(ncols);
        for (int c = 0; c < ncols; c++) {
            if (transforms[c] != 3) continue;
            int32_t dsize;
            fmeta.read((char*)&dsize, 4);
            dictionaries[c].resize(dsize);
            for (int i = 0; i < dsize; i++) {
                int16_t len;
                fmeta.read((char*)&len, 2);
                dictionaries[c][i].resize(len);
                fmeta.read(&dictionaries[c][i][0], len);
            }
        }

        // Read dropped column info
        int32_t n_dropped;
        fmeta.read((char*)&n_dropped, 4);

        struct DroppedCol {
            int col_idx;
            int partner_idx;
            std::unordered_map<std::string, int64_t> group_sums;
        };
        std::vector<DroppedCol> dropped_cols;

        for (int d = 0; d < n_dropped; d++) {
            DroppedCol dc;
            int32_t ci;
            fmeta.read((char*)&ci, 4);
            dc.col_idx = ci;
            if (ci < 0) {
                dropped_cols.push_back(dc);
                continue;
            }
            int32_t pi;
            fmeta.read((char*)&pi, 4);
            dc.partner_idx = pi;
            int32_t ng;
            fmeta.read((char*)&ng, 4);
            for (int g = 0; g < ng; g++) {
                int16_t klen;
                fmeta.read((char*)&klen, 2);
                std::string key(klen, '\0');
                fmeta.read(&key[0], klen);
                int64_t sv;
                fmeta.read((char*)&sv, 8);
                dc.group_sums[key] = sv;
            }
            dropped_cols.push_back(dc);
        }

        uint32_t marker;
        fmeta.read((char*)&marker, 4);
        assert(marker == 0xDEADBEEF);
        fmeta.close();

        // Read transformed TSV lines
        std::ifstream ftsv(tsv_path);
        if (!ftsv) { std::cerr << "Cannot open " << tsv_path << "\n"; return 1; }
        std::ofstream fout(output_path);
        if (!fout) { std::cerr << "Cannot open output\n"; return 1; }

        std::string prev_group;
        std::vector<int64_t> accum(ncols, 0);

        std::string line;
        while (std::getline(ftsv, line)) {
            if (line.empty()) continue;

            std::vector<std::string> fields;
            split_tabs(line, fields);

            // Map fields back to full columns
            // fields only contain non-dropped columns
            std::vector<std::string> full_row(ncols);
            int fi = 0;
            for (int c = 0; c < ncols; c++) {
                if (transforms[c] == 4) continue;
                full_row[c] = fields[fi++];
            }

            // Determine group
            std::string grp;
            // The group col is either raw or dict-encoded
            if (group_col_v >= 0) {
                if (transforms[group_col_v] == 3) {
                    int idx = std::stoi(full_row[group_col_v]);
                    grp = dictionaries[group_col_v][idx];
                } else {
                    grp = full_row[group_col_v];
                }
            }
            if (grp != prev_group) {
                prev_group = grp;
                std::fill(accum.begin(), accum.end(), 0);
            }

            // Reconstruct original values
            std::vector<std::string> out_row(ncols);
            for (int c = 0; c < ncols; c++) {
                if (transforms[c] == 4) continue;

                switch (transforms[c]) {
                    case 0: // raw
                        out_row[c] = full_row[c];
                        break;
                    case 1: { // delta → accumulate
                        int64_t delta = std::stoll(full_row[c]);
                        accum[c] += delta;
                        out_row[c] = std::to_string(accum[c]);
                        break;
                    }
                    case 2: { // span → add to previous col
                        int64_t span = std::stoll(full_row[c]);
                        int64_t start_v = std::stoll(out_row[c-1]);
                        out_row[c] = std::to_string(start_v + span);
                        break;
                    }
                    case 3: { // dict → lookup
                        int idx = std::stoi(full_row[c]);
                        out_row[c] = dictionaries[c][idx];
                        break;
                    }
                }
            }

            // Reconstruct dropped columns
            for (auto& dc : dropped_cols) {
                if (dc.col_idx < 0) continue;
                int64_t partner_val = std::stoll(out_row[dc.partner_idx]);
                int64_t sum = dc.group_sums[grp];
                out_row[dc.col_idx] = std::to_string(sum - partner_val);
            }

            // Output
            for (int c = 0; c < ncols; c++) {
                if (c > 0) fout.put('\t');
                fout << out_row[c];
            }
            fout.put('\n');
        }

        fout.close();
        std::cerr << "Done decoding\n";
    } else {
        std::cerr << "Unknown mode: " << mode << "\n";
        return 1;
    }

    return 0;
}
