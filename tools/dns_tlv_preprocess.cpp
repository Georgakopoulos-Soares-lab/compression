/*
 * dns_tlv_preprocess.cpp — DNS TLV binary preprocessor for compression
 *
 * Decomposes DNS TLV binary records into typed columnar streams for
 * OpenZL compression. Domain-aware columnar layout beats generic Snappy.
 *
 * TLV record format (big-endian):
 *   [4B record_length][8B start_time][4B flags][2B client_port][1B inet_family]
 *   followed by TLV fields: [1B type_id][2B data_length][data_bytes...]
 *
 * Modes:
 *   analyze <input.tlv>              — Profile fields, show stats
 *   encode  <input.tlv> <prefix>     — Encode to columnar binary
 *   decode  <prefix> <output.tlv>    — Reconstruct original TLV
 */

#include <iostream>
#include <fstream>
#include <vector>
#include <string>
#include <algorithm>
#include <cstdint>
#include <cstring>
#include <climits>
#include <unordered_map>
#include <unordered_set>
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>

// ============================================================================
// Utilities
// ============================================================================

struct MappedFile {
    const char* data;
    size_t size;
    int fd;

    MappedFile(const std::string& path) {
        fd = open(path.c_str(), O_RDONLY);
        if (fd == -1) throw std::runtime_error("Could not open file: " + path);
        struct stat sb;
        if (fstat(fd, &sb) == -1) { close(fd); throw std::runtime_error("Could not stat file: " + path); }
        size = sb.st_size;
        if (size == 0) { data = nullptr; return; }
        data = (const char*)mmap(NULL, size, PROT_READ, MAP_PRIVATE, fd, 0);
        if (data == MAP_FAILED) { close(fd); throw std::runtime_error("mmap failed"); }
        madvise((void*)data, size, MADV_SEQUENTIAL);
    }

    ~MappedFile() {
        if (data && data != MAP_FAILED) munmap((void*)data, size);
        if (fd != -1) close(fd);
    }

    MappedFile(const MappedFile&) = delete;
    MappedFile& operator=(const MappedFile&) = delete;
};

// Endian helpers
static inline uint16_t read_be16(const uint8_t* p) { return (uint16_t(p[0]) << 8) | p[1]; }
static inline uint32_t read_be32(const uint8_t* p) { return (uint32_t(p[0]) << 24) | (uint32_t(p[1]) << 16) | (uint32_t(p[2]) << 8) | p[3]; }
static inline uint64_t read_be64(const uint8_t* p) {
    return (uint64_t(read_be32(p)) << 32) | read_be32(p + 4);
}

static inline void write_le16(uint8_t* p, uint16_t v) { p[0] = v & 0xFF; p[1] = (v >> 8) & 0xFF; }
static inline void write_le32(uint8_t* p, uint32_t v) { p[0] = v & 0xFF; p[1] = (v >> 8) & 0xFF; p[2] = (v >> 16) & 0xFF; p[3] = (v >> 24) & 0xFF; }
static inline void write_le64(uint8_t* p, uint64_t v) { write_le32(p, (uint32_t)v); write_le32(p + 4, (uint32_t)(v >> 32)); }

static inline uint16_t read_le16(const uint8_t* p) { return uint16_t(p[0]) | (uint16_t(p[1]) << 8); }
static inline uint32_t read_le32(const uint8_t* p) { return uint32_t(p[0]) | (uint32_t(p[1]) << 8) | (uint32_t(p[2]) << 16) | (uint32_t(p[3]) << 24); }
static inline uint64_t read_le64(const uint8_t* p) { return uint64_t(read_le32(p)) | (uint64_t(read_le32(p + 4)) << 32); }

static inline void write_be16(uint8_t* p, uint16_t v) { p[0] = (v >> 8) & 0xFF; p[1] = v & 0xFF; }
static inline void write_be32(uint8_t* p, uint32_t v) { p[0] = (v >> 24) & 0xFF; p[1] = (v >> 16) & 0xFF; p[2] = (v >> 8) & 0xFF; p[3] = v & 0xFF; }
static inline void write_be64(uint8_t* p, uint64_t v) { write_be32(p, (uint32_t)(v >> 32)); write_be32(p + 4, (uint32_t)v); }

// ============================================================================
// Data Structures
// ============================================================================

static const size_t RECORD_LENGTH_PREFIX = 4;
static const size_t RECORD_HEADER_INNER = 8 + 4 + 2 + 1; // start_time + flags + port + inet = 15
static const size_t RECORD_HEADER_SIZE = RECORD_LENGTH_PREFIX + RECORD_HEADER_INNER; // 19 bytes total
static const size_t TLV_FIELD_HEADER_SIZE = 1 + 2; // type + length

struct TLVField {
    uint8_t  type_id;
    uint16_t data_length;
    const uint8_t* data_ptr; // pointer into mmap'd input (only valid during parsing)
};

struct RecordHeader {
    uint32_t record_length;
    uint64_t start_time;
    uint32_t flags;
    uint16_t client_port;
    uint8_t  inet_family;
};

struct ParsedRecord {
    RecordHeader header;
    std::vector<TLVField> fields;
};

// ============================================================================
// Parsing
// ============================================================================

static bool parse_records(const uint8_t* data, size_t size, std::vector<ParsedRecord>& records) {
    size_t offset = 0;
    while (offset + 4 <= size) {
        uint32_t record_length = read_be32(data + offset);
        // record_length INCLUDES the 4-byte length prefix itself
        if (record_length < RECORD_HEADER_SIZE || offset + record_length > size) break;

        const uint8_t* rec = data + offset + RECORD_LENGTH_PREFIX;
        size_t rec_data_size = record_length - RECORD_LENGTH_PREFIX;

        ParsedRecord pr;
        pr.header.record_length = record_length;
        pr.header.start_time = read_be64(rec);
        pr.header.flags = read_be32(rec + 8);
        pr.header.client_port = read_be16(rec + 12);
        pr.header.inet_family = rec[14];

        // Parse TLV fields after the fixed header
        size_t foff = RECORD_HEADER_INNER;
        while (foff + TLV_FIELD_HEADER_SIZE <= rec_data_size) {
            TLVField f;
            f.type_id = rec[foff];
            f.data_length = read_be16(rec + foff + 1);
            f.data_ptr = rec + foff + TLV_FIELD_HEADER_SIZE;
            if (foff + TLV_FIELD_HEADER_SIZE + f.data_length > rec_data_size) break;
            pr.fields.push_back(f);
            foff += TLV_FIELD_HEADER_SIZE + f.data_length;
        }

        records.push_back(std::move(pr));
        offset += record_length; // advance by full record_length (includes prefix)
    }
    return !records.empty();
}

// ============================================================================
// Analyze Mode
// ============================================================================

struct FieldStats {
    uint32_t count = 0;
    uint16_t min_len = UINT16_MAX;
    uint16_t max_len = 0;
    uint64_t total_bytes = 0;
    bool     all_same_len = true;
    bool     all_same_value = true;
    std::vector<uint8_t> first_value;
};

static void do_analyze(const std::string& input_path) {
    MappedFile mf(input_path);
    std::vector<ParsedRecord> records;
    parse_records((const uint8_t*)mf.data, mf.size, records);

    uint32_t num_records = records.size();
    std::cerr << "File: " << input_path << "\n";
    std::cerr << "File size: " << mf.size << " bytes\n";
    std::cerr << "Records: " << num_records << "\n";

    if (num_records == 0) return;

    // Header field analysis
    uint64_t min_time = UINT64_MAX, max_time = 0;
    uint32_t flags_unique = 0;
    std::unordered_set<uint32_t> flags_set;
    std::unordered_set<uint16_t> port_set;
    std::unordered_set<uint8_t> inet_set;

    for (auto& r : records) {
        if (r.header.start_time < min_time) min_time = r.header.start_time;
        if (r.header.start_time > max_time) max_time = r.header.start_time;
        flags_set.insert(r.header.flags);
        port_set.insert(r.header.client_port);
        inet_set.insert(r.header.inet_family);
    }

    std::cerr << "\n=== Fixed Header Fields ===\n";
    std::cerr << "  start_time: min=" << min_time << " max=" << max_time
              << " range=" << (max_time - min_time) << "\n";
    std::cerr << "  flags: " << flags_set.size() << " unique values";
    if (flags_set.size() <= 5) {
        std::cerr << " [";
        bool first = true;
        for (auto v : flags_set) { if (!first) std::cerr << ","; std::cerr << v; first = false; }
        std::cerr << "]";
    }
    std::cerr << "\n";
    std::cerr << "  client_port: " << port_set.size() << " unique values\n";
    std::cerr << "  inet_family: " << inet_set.size() << " unique values";
    if (inet_set.size() <= 10) {
        std::cerr << " [";
        bool first = true;
        for (auto v : inet_set) { if (!first) std::cerr << ","; std::cerr << (int)v; first = false; }
        std::cerr << "]";
    }
    std::cerr << "\n";

    // TLV field analysis
    std::unordered_map<uint8_t, FieldStats> stats;
    std::unordered_map<uint8_t, uint32_t> fields_per_record_min, fields_per_record_max;

    for (auto& r : records) {
        std::unordered_map<uint8_t, uint32_t> field_counts;
        for (auto& f : r.fields) {
            field_counts[f.type_id]++;
            auto& s = stats[f.type_id];
            s.count++;
            if (f.data_length < s.min_len) s.min_len = f.data_length;
            if (f.data_length > s.max_len) s.max_len = f.data_length;
            s.total_bytes += f.data_length;

            if (s.min_len != s.max_len) s.all_same_len = false;

            if (s.count == 1) {
                s.first_value.assign(f.data_ptr, f.data_ptr + f.data_length);
            } else if (s.all_same_value) {
                if (f.data_length != s.first_value.size() ||
                    memcmp(f.data_ptr, s.first_value.data(), f.data_length) != 0) {
                    s.all_same_value = false;
                }
            }
        }

        for (auto& [tid, cnt] : field_counts) {
            auto it_min = fields_per_record_min.find(tid);
            if (it_min == fields_per_record_min.end()) fields_per_record_min[tid] = cnt;
            else if (cnt < it_min->second) it_min->second = cnt;

            auto it_max = fields_per_record_max.find(tid);
            if (it_max == fields_per_record_max.end()) fields_per_record_max[tid] = cnt;
            else if (cnt > it_max->second) it_max->second = cnt;
        }

        // Fields not present in this record get min=0
        for (auto& [tid, _] : stats) {
            if (field_counts.find(tid) == field_counts.end()) {
                fields_per_record_min[tid] = 0;
            }
        }
    }

    static const char* field_names[] = {
        "ENDTIME", "DNS_MESSAGE", "CLIENT_ADDRESS", "SERVER_ADDRESS",
        "SERVER_PORT", "VIEW", "ZONE", "QUERY_NAME", "QTYPE", "RCODE",
        "QUERY_FLAGS", "CLIENT_FLAGS", "DEVICE_NAME", "POLICY_DOMAIN",
        "POLICY_RULE", "POLICY_TAGS", "POLICY_ACTION", "POLICY_MATCH_LIST",
        "POLICY_RPZ_ACTION", "POLICY_HIT", "SERVER_FLAGS", "DEVICE_ID",
        "CLIENT_ID", "NUMERIC_ID", "CORE_DOMAIN", "COUNT",
        "QUERY_ID", "TARGET_ADDRESS", "MAC_ADDRESS", "DNS_RESPONSE",
        "DEVICE_DOMAIN", "DOMAIN_CATEGORY", "SITE_ADDRESS", "SITE_ID",
        "MATCHED_DOMAIN", "NORMALIZED_QUERY_NAME", "TCP_RTT", "RESOLVED_ADDRESS"
    };
    static const int NUM_FIELD_NAMES = sizeof(field_names) / sizeof(field_names[0]);

    std::cerr << "\n=== TLV Field Types ===\n";
    std::cerr << "Type  Name                      Count   Pres%   MinLen  MaxLen  TotalKB  Const  PerRec\n";
    std::cerr << "----  ------------------------  ------  -----  ------  ------  -------  -----  ------\n";

    // Sort by type_id
    std::vector<uint8_t> sorted_types;
    for (auto& [tid, _] : stats) sorted_types.push_back(tid);
    std::sort(sorted_types.begin(), sorted_types.end());

    for (auto tid : sorted_types) {
        auto& s = stats[tid];
        const char* name = (tid < NUM_FIELD_NAMES) ? field_names[tid] : "UNKNOWN";
        double pres_pct = 100.0 * s.count / num_records;
        uint32_t min_per = fields_per_record_min.count(tid) ? fields_per_record_min[tid] : 0;
        uint32_t max_per = fields_per_record_max.count(tid) ? fields_per_record_max[tid] : 0;

        fprintf(stderr, "%4d  %-24s  %6u  %5.1f%%  %6u  %6u  %7.1f  %-5s  %u-%u\n",
                tid, name, s.count, pres_pct, s.min_len, s.max_len,
                s.total_bytes / 1024.0,
                s.all_same_value ? "YES" : "no",
                min_per, max_per);
    }

    // Field order analysis
    std::cerr << "\n=== Field Order ===\n";
    uint32_t min_fields = UINT32_MAX, max_fields = 0;
    uint64_t total_fields = 0;
    for (auto& r : records) {
        uint32_t n = r.fields.size();
        if (n < min_fields) min_fields = n;
        if (n > max_fields) max_fields = n;
        total_fields += n;
    }
    std::cerr << "  Fields per record: min=" << min_fields << " max=" << max_fields
              << " avg=" << (total_fields / num_records) << "\n";

    // Check if endtime is delta from start_time
    if (stats.count(0) && stats[0].count > 0) {
        std::cerr << "\n=== Endtime Analysis ===\n";
        int64_t min_delta = INT64_MAX, max_delta = INT64_MIN;
        for (auto& r : records) {
            for (auto& f : r.fields) {
                if (f.type_id == 0 && f.data_length == 4) {
                    uint32_t endtime = read_be32(f.data_ptr);
                    int64_t delta = (int64_t)endtime - (int64_t)(r.header.start_time / 1000000);
                    if (delta < min_delta) min_delta = delta;
                    if (delta > max_delta) max_delta = delta;
                }
            }
        }
        std::cerr << "  endtime - (start_time/1e6): min=" << min_delta << " max=" << max_delta << "\n";
        // Also try raw endtime values
        uint32_t min_et = UINT32_MAX, max_et = 0;
        for (auto& r : records) {
            for (auto& f : r.fields) {
                if (f.type_id == 0 && f.data_length == 4) {
                    uint32_t et = read_be32(f.data_ptr);
                    if (et < min_et) min_et = et;
                    if (et > max_et) max_et = et;
                }
            }
        }
        std::cerr << "  raw endtime: min=" << min_et << " max=" << max_et
                  << " range=" << (max_et - min_et) << "\n";
    }

    // Summary
    uint64_t total_tlv_data = 0;
    uint64_t total_tlv_headers = 0;
    for (auto& [tid, s] : stats) {
        total_tlv_data += s.total_bytes;
        total_tlv_headers += (uint64_t)s.count * TLV_FIELD_HEADER_SIZE;
    }
    uint64_t total_rec_headers = (uint64_t)num_records * (RECORD_HEADER_SIZE - 4); // minus 4B length prefix
    uint64_t total_length_prefixes = (uint64_t)num_records * 4;

    std::cerr << "\n=== Size Breakdown ===\n";
    std::cerr << "  Record length prefixes: " << total_length_prefixes << " bytes\n";
    std::cerr << "  Record headers (excl length): " << total_rec_headers << " bytes\n";
    std::cerr << "  TLV field headers (type+len): " << total_tlv_headers << " bytes\n";
    std::cerr << "  TLV field data: " << total_tlv_data << " bytes\n";
    std::cerr << "  Total: " << mf.size << " bytes\n";
}

// ============================================================================
// Column Classification
// ============================================================================

// A column can be one of:
//   FIXED   — present in every record, same data_length, stored as a flat array
//   VARIABLE — variable-length or optional, stored with presence bitmap + offsets + data
//   CONSTANT — every occurrence has the same value, stored once in metadata
//   DROPPED  — field not present in any record (no storage needed)
enum ColumnClass {
    COL_FIXED,
    COL_VARIABLE,
    COL_CONSTANT,
    COL_DROPPED
};

struct ColumnInfo {
    uint8_t type_id;
    ColumnClass cls;
    uint16_t fixed_len;       // for FIXED columns
    std::vector<uint8_t> constant_value; // for CONSTANT columns
};

// Classify fields based on parsed records
static std::vector<ColumnInfo> classify_columns(
    const std::vector<ParsedRecord>& records,
    uint32_t num_records)
{
    // Gather stats
    struct FStats {
        uint32_t count = 0;
        uint16_t min_len = UINT16_MAX, max_len = 0;
        bool all_same_value = true;
        std::vector<uint8_t> first_value;
    };
    std::unordered_map<uint8_t, FStats> fstats;

    for (auto& r : records) {
        for (auto& f : r.fields) {
            auto& s = fstats[f.type_id];
            s.count++;
            if (f.data_length < s.min_len) s.min_len = f.data_length;
            if (f.data_length > s.max_len) s.max_len = f.data_length;
            if (s.count == 1) {
                s.first_value.assign(f.data_ptr, f.data_ptr + f.data_length);
            } else if (s.all_same_value) {
                if (f.data_length != s.first_value.size() ||
                    memcmp(f.data_ptr, s.first_value.data(), f.data_length) != 0) {
                    s.all_same_value = false;
                }
            }
        }
    }

    std::vector<ColumnInfo> cols;
    // Sort by type_id for deterministic order
    std::vector<uint8_t> types;
    for (auto& [tid, _] : fstats) types.push_back(tid);
    std::sort(types.begin(), types.end());

    for (auto tid : types) {
        auto& s = fstats[tid];
        ColumnInfo ci;
        ci.type_id = tid;

        if (s.count == 0) {
            ci.cls = COL_DROPPED;
            ci.fixed_len = 0;
        } else if (s.all_same_value && s.count == num_records) {
            ci.cls = COL_CONSTANT;
            ci.fixed_len = s.min_len;
            ci.constant_value = s.first_value;
        } else if (s.count == num_records && s.min_len == s.max_len) {
            ci.cls = COL_FIXED;
            ci.fixed_len = s.min_len;
        } else {
            ci.cls = COL_VARIABLE;
            ci.fixed_len = 0;
        }

        cols.push_back(ci);
    }

    return cols;
}

// ============================================================================
// Encode Mode
// ============================================================================

static const char MAGIC_BIN[] = "DTLV0001";
static const char MAGIC_META[] = "DTMETA01";

/*
 * Binary output layout (.dtlv.bin):
 *
 *   [8B magic "DTLV0001"]
 *   [4B LE num_records]
 *
 *   --- Section A: Fixed header columns ---
 *   [8B LE base_timestamp]
 *   [4B LE time_deltas[N]]            (start_time - base as uint32 micros / 1e6 seconds)
 *   [2B LE client_port[N]]
 *   [1B    inet_family[N]]
 *
 *   --- Section B: Fixed-size always-present TLV columns ---
 *   For each FIXED column (sorted by type_id):
 *     [fixed_len * N bytes]
 *
 *   --- Section C: Variable-length / optional TLV columns ---
 *   [4B LE num_var_columns]
 *   For each VARIABLE column:
 *     [1B type_id]
 *     [4B LE presence_bitmap_size]     (ceil(N/8))
 *     [presence_bitmap bytes]
 *     [4B LE num_present]
 *     [4B LE offsets[num_present + 1]] (prefix sums)
 *     [data bytes]
 *
 *   --- Section D: Field order map ---
 *   [1B field_counts[N]]
 *   [4B LE total_order_bytes]
 *   [field_order bytes: type_ids concatenated for all records]
 *
 * Metadata sidecar (.dtlv.meta):
 *   [8B magic "DTMETA01"]
 *   [4B LE num_records]
 *   [8B LE base_timestamp]
 *   [4B LE flags_value]               (constant flags value — assumed constant)
 *   [1B flags_is_constant]            (1 if constant, 0 if stored as column)
 *   [4B LE num_constant_fields]
 *   For each constant field:
 *     [1B type_id]
 *     [2B LE data_length]
 *     [data bytes]
 *   [4B LE num_fixed_columns]
 *   For each fixed column:
 *     [1B type_id]
 *     [2B LE fixed_len]
 *   [4B LE num_var_columns]
 *   For each variable column:
 *     [1B type_id]
 *   [4B 0xDEADBEEF]                   (end marker)
 */

static int do_encode(const std::string& input_path, const std::string& prefix) {
    MappedFile mf(input_path);
    std::vector<ParsedRecord> records;
    parse_records((const uint8_t*)mf.data, mf.size, records);

    uint32_t N = records.size();
    if (N == 0) {
        std::cerr << "No records found in " << input_path << "\n";
        return 1;
    }

    // Classify columns
    auto cols = classify_columns(records, N);

    // Determine if flags is constant
    bool flags_constant = true;
    uint32_t flags_value = records[0].header.flags;
    for (size_t i = 1; i < N; i++) {
        if (records[i].header.flags != flags_value) { flags_constant = false; break; }
    }

    // Base timestamp = minimum start_time
    uint64_t base_timestamp = records[0].header.start_time;
    for (auto& r : records) {
        if (r.header.start_time < base_timestamp) base_timestamp = r.header.start_time;
    }

    // Separate column lists
    std::vector<ColumnInfo*> fixed_cols, var_cols, const_cols;
    for (auto& c : cols) {
        switch (c.cls) {
            case COL_FIXED:    fixed_cols.push_back(&c); break;
            case COL_VARIABLE: var_cols.push_back(&c); break;
            case COL_CONSTANT: const_cols.push_back(&c); break;
            case COL_DROPPED:  break;
        }
    }

    // Build lookup: type_id -> column data for each record
    // For fixed columns: flat array per type
    // For variable columns: per-record presence + data

    // === Write binary output ===
    std::string bin_path = prefix + ".dtlv.bin";
    std::ofstream fbin(bin_path, std::ios::binary);
    if (!fbin) { std::cerr << "Cannot open " << bin_path << "\n"; return 1; }

    // Magic + num_records
    fbin.write(MAGIC_BIN, 8);
    uint8_t buf[8];
    write_le32(buf, N);
    fbin.write((char*)buf, 4);

    // Section A: Fixed header columns
    write_le64(buf, base_timestamp);
    fbin.write((char*)buf, 8);

    // Original record_length values (needed for bit-exact reconstruction)
    for (auto& r : records) {
        write_le32(buf, r.header.record_length);
        fbin.write((char*)buf, 4);
    }

    // Time deltas
    for (auto& r : records) {
        uint64_t raw_delta = r.header.start_time - base_timestamp;
        if (raw_delta > UINT32_MAX) {
            std::cerr << "ERROR: time delta overflow (delta=" << raw_delta << ")\n";
            return 1;
        }
        write_le32(buf, (uint32_t)raw_delta);
        fbin.write((char*)buf, 4);
    }

    // Client port
    for (auto& r : records) {
        write_le16(buf, r.header.client_port);
        fbin.write((char*)buf, 2);
    }

    // Inet family
    for (auto& r : records) {
        buf[0] = r.header.inet_family;
        fbin.write((char*)buf, 1);
    }

    // Flags (only if not constant)
    if (!flags_constant) {
        for (auto& r : records) {
            write_le32(buf, r.header.flags);
            fbin.write((char*)buf, 4);
        }
    }

    // Sections B+C combined: Fixed + variable TLV columns (one size-prefixed blob)
    std::vector<uint8_t> columns_blob;

    // Section B: Fixed-size columns
    {
        uint8_t t[4]; write_le32(t, (uint32_t)fixed_cols.size());
        columns_blob.insert(columns_blob.end(), t, t + 4);
    }
    for (auto* col : fixed_cols) {
        columns_blob.push_back(col->type_id);
        uint32_t col_total = (uint32_t)col->fixed_len * N;
        { uint8_t t[4]; write_le32(t, col_total); columns_blob.insert(columns_blob.end(), t, t + 4); }
        for (auto& r : records) {
            bool found = false;
            for (auto& f : r.fields) {
                if (f.type_id == col->type_id) {
                    columns_blob.insert(columns_blob.end(), f.data_ptr, f.data_ptr + col->fixed_len);
                    found = true;
                    break;
                }
            }
            if (!found) {
                columns_blob.resize(columns_blob.size() + col->fixed_len, 0);
            }
        }
    }

    // Section C: Variable-length / optional TLV columns
    // Buffer Section C into a vector so we can write its total size first
    std::vector<uint8_t> section_c;

    // num_var_columns
    {
        uint8_t t[4];
        write_le32(t, (uint32_t)var_cols.size());
        section_c.insert(section_c.end(), t, t + 4);
    }

    for (auto* col : var_cols) {
        // Type ID
        section_c.push_back(col->type_id);

        // Build presence bitmap and collect data
        uint32_t bitmap_size = (N + 7) / 8;
        std::vector<uint8_t> bitmap(bitmap_size, 0);
        std::vector<const uint8_t*> data_ptrs;
        std::vector<uint16_t> data_lens;

        for (uint32_t i = 0; i < N; i++) {
            for (auto& f : records[i].fields) {
                if (f.type_id == col->type_id) {
                    bitmap[i / 8] |= (1 << (i % 8));
                    data_ptrs.push_back(f.data_ptr);
                    data_lens.push_back(f.data_length);
                    break;
                }
            }
        }

        uint32_t num_present = data_ptrs.size();

        // bitmap_size + bitmap
        {
            uint8_t t[4];
            write_le32(t, bitmap_size);
            section_c.insert(section_c.end(), t, t + 4);
        }
        section_c.insert(section_c.end(), bitmap.begin(), bitmap.end());

        // num_present
        {
            uint8_t t[4];
            write_le32(t, num_present);
            section_c.insert(section_c.end(), t, t + 4);
        }

        // offsets_size + offsets
        uint32_t offsets_size = (num_present + 1) * 4;
        {
            uint8_t t[4];
            write_le32(t, offsets_size);
            section_c.insert(section_c.end(), t, t + 4);
        }
        uint32_t running_offset = 0;
        for (uint32_t j = 0; j <= num_present; j++) {
            uint8_t t[4];
            write_le32(t, running_offset);
            section_c.insert(section_c.end(), t, t + 4);
            if (j < num_present) running_offset += data_lens[j];
        }

        // data_size + data
        {
            uint8_t t[4];
            write_le32(t, running_offset);
            section_c.insert(section_c.end(), t, t + 4);
        }
        for (uint32_t j = 0; j < num_present; j++) {
            section_c.insert(section_c.end(), data_ptrs[j], data_ptrs[j] + data_lens[j]);
        }
    }

    // Append Section C to columns_blob
    columns_blob.insert(columns_blob.end(), section_c.begin(), section_c.end());

    // Write columns_size + columns_blob
    write_le32(buf, (uint32_t)columns_blob.size());
    fbin.write((char*)buf, 4);
    fbin.write((const char*)columns_blob.data(), columns_blob.size());

    // Section D: Field order map
    // field_counts: how many TLV fields per record
    for (auto& r : records) {
        if (r.fields.size() > 255) {
            std::cerr << "ERROR: record has " << r.fields.size() << " fields (max 255)\n";
            return 1;
        }
        buf[0] = (uint8_t)r.fields.size();
        fbin.write((char*)buf, 1);
    }

    // field_order: concatenated type_ids
    uint32_t total_order_bytes = 0;
    for (auto& r : records) total_order_bytes += r.fields.size();
    write_le32(buf, total_order_bytes);
    fbin.write((char*)buf, 4);

    for (auto& r : records) {
        for (auto& f : r.fields) {
            buf[0] = f.type_id;
            fbin.write((char*)buf, 1);
        }
    }

    fbin.close();

    // === Write metadata sidecar ===
    std::string meta_path = prefix + ".dtlv.meta";
    std::ofstream fmeta(meta_path, std::ios::binary);
    if (!fmeta) { std::cerr << "Cannot open " << meta_path << "\n"; return 1; }

    fmeta.write(MAGIC_META, 8);

    write_le32(buf, N);
    fmeta.write((char*)buf, 4);

    write_le64(buf, base_timestamp);
    fmeta.write((char*)buf, 8);

    // Flags
    write_le32(buf, flags_value);
    fmeta.write((char*)buf, 4);
    buf[0] = flags_constant ? 1 : 0;
    fmeta.write((char*)buf, 1);

    // Constant fields
    write_le32(buf, (uint32_t)const_cols.size());
    fmeta.write((char*)buf, 4);
    for (auto* col : const_cols) {
        buf[0] = col->type_id;
        fmeta.write((char*)buf, 1);
        write_le16(buf, (uint16_t)col->constant_value.size());
        fmeta.write((char*)buf, 2);
        fmeta.write((const char*)col->constant_value.data(), col->constant_value.size());
    }

    // Fixed columns directory
    write_le32(buf, (uint32_t)fixed_cols.size());
    fmeta.write((char*)buf, 4);
    for (auto* col : fixed_cols) {
        buf[0] = col->type_id;
        fmeta.write((char*)buf, 1);
        write_le16(buf, col->fixed_len);
        fmeta.write((char*)buf, 2);
    }

    // Variable columns directory
    write_le32(buf, (uint32_t)var_cols.size());
    fmeta.write((char*)buf, 4);
    for (auto* col : var_cols) {
        buf[0] = col->type_id;
        fmeta.write((char*)buf, 1);
    }

    // End marker
    write_le32(buf, 0xDEADBEEF);
    fmeta.write((char*)buf, 4);
    fmeta.close();

    // Report
    std::cerr << "Encoded " << N << " records\n";
    std::cerr << "  Fixed columns: " << fixed_cols.size() << "\n";
    std::cerr << "  Variable columns: " << var_cols.size() << "\n";
    std::cerr << "  Constant columns: " << const_cols.size() << "\n";

    // File sizes
    struct stat sb;
    stat(bin_path.c_str(), &sb);
    std::cerr << "  " << bin_path << ": " << sb.st_size << " bytes\n";
    stat(meta_path.c_str(), &sb);
    std::cerr << "  " << meta_path << ": " << sb.st_size << " bytes\n";
    std::cerr << "  Original: " << mf.size << " bytes\n";
    return 0;
}

// ============================================================================
// Decode Mode
// ============================================================================

static int do_decode(const std::string& prefix, const std::string& output_path) {
    // Read metadata
    std::string meta_path = prefix + ".dtlv.meta";
    std::ifstream fmeta(meta_path, std::ios::binary);
    if (!fmeta) { std::cerr << "Cannot open " << meta_path << "\n"; return 1; }

    char magic_buf[8];
    fmeta.read(magic_buf, 8);
    if (memcmp(magic_buf, MAGIC_META, 8) != 0) {
        std::cerr << "Bad meta magic\n"; return 1;
    }

    uint8_t rbuf[8];
    fmeta.read((char*)rbuf, 4); uint32_t N = read_le32(rbuf);
    fmeta.read((char*)rbuf, 8); uint64_t base_timestamp = read_le64(rbuf);
    fmeta.read((char*)rbuf, 4); uint32_t flags_value = read_le32(rbuf);
    fmeta.read((char*)rbuf, 1); bool flags_constant = (rbuf[0] != 0);

    // Constant fields
    fmeta.read((char*)rbuf, 4); uint32_t num_const = read_le32(rbuf);
    struct ConstField { uint8_t type_id; std::vector<uint8_t> data; };
    std::vector<ConstField> const_fields(num_const);
    for (uint32_t i = 0; i < num_const; i++) {
        fmeta.read((char*)rbuf, 1); const_fields[i].type_id = rbuf[0];
        fmeta.read((char*)rbuf, 2); uint16_t dlen = read_le16(rbuf);
        const_fields[i].data.resize(dlen);
        fmeta.read((char*)const_fields[i].data.data(), dlen);
    }

    // Fixed columns
    fmeta.read((char*)rbuf, 4); uint32_t num_fixed = read_le32(rbuf);
    struct FixedCol { uint8_t type_id; uint16_t fixed_len; };
    std::vector<FixedCol> fixed_col_dir(num_fixed);
    for (uint32_t i = 0; i < num_fixed; i++) {
        fmeta.read((char*)rbuf, 1); fixed_col_dir[i].type_id = rbuf[0];
        fmeta.read((char*)rbuf, 2); fixed_col_dir[i].fixed_len = read_le16(rbuf);
    }

    // Variable columns
    fmeta.read((char*)rbuf, 4); uint32_t num_var_meta = read_le32(rbuf);
    std::vector<uint8_t> var_type_ids(num_var_meta);
    for (uint32_t i = 0; i < num_var_meta; i++) {
        fmeta.read((char*)rbuf, 1); var_type_ids[i] = rbuf[0];
    }

    fmeta.read((char*)rbuf, 4);
    if (read_le32(rbuf) != 0xDEADBEEF) {
        std::cerr << "Corrupt metadata: missing end marker\n"; return 1;
    }
    fmeta.close();

    // Read binary
    std::string bin_path = prefix + ".dtlv.bin";
    MappedFile mbin(bin_path);
    const uint8_t* p = (const uint8_t*)mbin.data;
    size_t off = 0;

    // Magic
    if (memcmp(p, MAGIC_BIN, 8) != 0) { std::cerr << "Bad bin magic\n"; return 1; }
    off += 8;

    uint32_t bin_N = read_le32(p + off); off += 4;
    if (bin_N != N) {
        std::cerr << "Record count mismatch: meta=" << N << " bin=" << bin_N << "\n"; return 1;
    }

    // Section A
    uint64_t bin_base = read_le64(p + off); off += 8;
    if (bin_base != base_timestamp) {
        std::cerr << "Base timestamp mismatch\n"; return 1;
    }

    const uint8_t* rec_lengths_p = p + off; off += 4 * N;
    const uint8_t* time_deltas_p = p + off; off += 4 * N;
    const uint8_t* client_port_p = p + off; off += 2 * N;
    const uint8_t* inet_family_p = p + off; off += 1 * N;

    const uint8_t* flags_p = nullptr;
    if (!flags_constant) {
        flags_p = p + off; off += 4 * N;
    }

    // Sections B+C: Combined columns blob (size-prefixed)
    uint32_t columns_blob_size = read_le32(p + off); off += 4;
    size_t columns_blob_start = off;

    // Section B within blob: Fixed columns
    uint32_t num_fixed_bin = read_le32(p + off); off += 4;
    if (num_fixed_bin != num_fixed) {
        std::cerr << "Fixed column count mismatch: meta=" << num_fixed << " bin=" << num_fixed_bin << "\n";
        return 1;
    }

    struct FixedColData { uint8_t type_id; uint16_t fixed_len; const uint8_t* data; };
    std::vector<FixedColData> fixed_data(num_fixed);
    for (uint32_t i = 0; i < num_fixed; i++) {
        uint8_t tid = p[off]; off += 1;
        uint32_t col_total = read_le32(p + off); off += 4;
        fixed_data[i].type_id = tid;
        fixed_data[i].fixed_len = fixed_col_dir[i].fixed_len;
        fixed_data[i].data = p + off;
        off += col_total;
    }

    // Section C within blob: Variable columns
    uint32_t num_var_bin = read_le32(p + off); off += 4;
    if (num_var_bin != num_var_meta) {
        std::cerr << "Variable column count mismatch\n"; return 1;
    }

    struct VarColData {
        uint8_t type_id;
        uint32_t bitmap_size;
        const uint8_t* bitmap;
        uint32_t num_present;
        const uint8_t* offsets; // (num_present+1) LE uint32_t
        const uint8_t* data;
    };
    std::vector<VarColData> var_data(num_var_bin);

    for (uint32_t i = 0; i < num_var_bin; i++) {
        var_data[i].type_id = p[off]; off += 1;
        var_data[i].bitmap_size = read_le32(p + off); off += 4;
        var_data[i].bitmap = p + off; off += var_data[i].bitmap_size;
        var_data[i].num_present = read_le32(p + off); off += 4;
        uint32_t offsets_size = read_le32(p + off); off += 4; // SDDL size field
        var_data[i].offsets = p + off; off += offsets_size;
        uint32_t total_data = read_le32(p + off); off += 4; // SDDL size field
        var_data[i].data = p + off;
        off += total_data;
    }

    // Section D: Field order
    const uint8_t* field_counts_p = p + off; off += N;
    uint32_t total_order = read_le32(p + off); off += 4;
    const uint8_t* field_order_p = p + off; off += total_order;

    // Build lookup maps
    std::unordered_map<uint8_t, size_t> fixed_map; // type_id -> index in fixed_data
    for (size_t i = 0; i < fixed_data.size(); i++) fixed_map[fixed_data[i].type_id] = i;

    std::unordered_map<uint8_t, size_t> var_map; // type_id -> index in var_data
    for (size_t i = 0; i < var_data.size(); i++) var_map[var_data[i].type_id] = i;

    std::unordered_map<uint8_t, size_t> const_map; // type_id -> index in const_fields
    for (size_t i = 0; i < const_fields.size(); i++) const_map[const_fields[i].type_id] = i;

    // Track variable column presence counters (how many present records seen so far per var col)
    std::vector<uint32_t> var_present_idx(num_var_bin, 0);

    // Reconstruct
    std::ofstream fout(output_path, std::ios::binary);
    if (!fout) { std::cerr << "Cannot open " << output_path << "\n"; return 1; }

    size_t order_offset = 0;

    for (uint32_t i = 0; i < N; i++) {
        // Reconstruct header
        uint64_t start_time = base_timestamp + read_le32(time_deltas_p + 4 * i);
        uint32_t flags = flags_constant ? flags_value : read_le32(flags_p + 4 * i);
        uint16_t client_port = read_le16(client_port_p + 2 * i);
        uint8_t inet_family = inet_family_p[i];

        uint8_t field_count = field_counts_p[i];

        // First, gather all TLV field data for this record to compute record_length
        struct FieldData {
            uint8_t type_id;
            uint16_t data_len;
            const uint8_t* data;
        };
        std::vector<FieldData> rec_fields;

        for (uint8_t fi = 0; fi < field_count; fi++) {
            uint8_t tid = field_order_p[order_offset + fi];
            FieldData fd;
            fd.type_id = tid;

            auto it_fixed = fixed_map.find(tid);
            if (it_fixed != fixed_map.end()) {
                auto& fc = fixed_data[it_fixed->second];
                fd.data_len = fc.fixed_len;
                fd.data = fc.data + (size_t)fc.fixed_len * i;
                rec_fields.push_back(fd);
                continue;
            }

            auto it_var = var_map.find(tid);
            if (it_var != var_map.end()) {
                auto& vc = var_data[it_var->second];
                // Check bitmap
                bool present = (vc.bitmap[i / 8] >> (i % 8)) & 1;
                if (present) {
                    uint32_t pidx = var_present_idx[it_var->second];
                    uint32_t data_start = read_le32(vc.offsets + 4 * pidx);
                    uint32_t data_end = read_le32(vc.offsets + 4 * (pidx + 1));
                    fd.data_len = data_end - data_start;
                    fd.data = vc.data + data_start;
                    rec_fields.push_back(fd);
                }
                // Note: var_present_idx is advanced below after all fields processed
                continue;
            }

            auto it_const = const_map.find(tid);
            if (it_const != const_map.end()) {
                auto& cc = const_fields[it_const->second];
                fd.data_len = cc.data.size();
                fd.data = cc.data.data();
                rec_fields.push_back(fd);
                continue;
            }

            // Should not reach here
            std::cerr << "Unknown field type " << (int)tid << " at record " << i << "\n";
        }

        // Advance var_present_idx for this record
        // We need to check which var columns had this record present
        for (size_t vi = 0; vi < var_data.size(); vi++) {
            bool present = (var_data[vi].bitmap[i / 8] >> (i % 8)) & 1;
            if (present) var_present_idx[vi]++;
        }

        order_offset += field_count;

        // Use the original record_length (includes 4-byte prefix) for bit-exact output
        uint32_t record_length = read_le32(rec_lengths_p + 4 * i);

        // Write record
        uint8_t hdr[19];
        write_be32(hdr, record_length);
        write_be64(hdr + 4, start_time);
        write_be32(hdr + 12, flags);
        write_be16(hdr + 16, client_port);
        hdr[18] = inet_family;
        fout.write((char*)hdr, 19);

        // Write TLV fields
        for (auto& fd : rec_fields) {
            uint8_t fhdr[3];
            fhdr[0] = fd.type_id;
            write_be16(fhdr + 1, fd.data_len);
            fout.write((char*)fhdr, 3);
            fout.write((const char*)fd.data, fd.data_len);
        }
    }

    fout.close();
    std::cerr << "Decoded " << N << " records to " << output_path << "\n";
    return 0;
}

// ============================================================================
// Main
// ============================================================================

int main(int argc, char** argv) {
    if (argc < 3) {
        std::cerr << "Usage:\n"
                  << "  dns_tlv_preprocess analyze <input.tlv>\n"
                  << "  dns_tlv_preprocess encode  <input.tlv> <output_prefix>\n"
                  << "  dns_tlv_preprocess decode  <prefix> <output.tlv>\n";
        return 1;
    }

    std::string mode = argv[1];

    if (mode == "analyze") {
        do_analyze(argv[2]);
    } else if (mode == "encode") {
        if (argc < 4) { std::cerr << "encode requires <input.tlv> <output_prefix>\n"; return 1; }
        return do_encode(argv[2], argv[3]);
    } else if (mode == "decode") {
        if (argc < 4) { std::cerr << "decode requires <prefix> <output.tlv>\n"; return 1; }
        return do_decode(argv[2], argv[3]);
    } else {
        std::cerr << "Unknown mode: " << mode << "\n";
        return 1;
    }

    return 0;
}
