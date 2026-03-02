// vcf_preprocessor.cpp
//
// Converts a VCF (Variant Call Format) file into columnar binary chunks
// suitable for OpenZL schema-driven compression training and evaluation.
//
// Usage:
//   vcf_preprocessor <input.vcf> <output_dir> <num_threads>
//
// Output: <output_dir>/chunk_XXXXX.vcf_columnar.bin
//
// Binary layout (matches schemas/vcf_columnar.sddl):
//   Byte[4]           magic "VCF1"
//   U32LE             num_variants
//   U32LE             ref_total    (total payload bytes for REF strings)
//   U32LE             alt_total    (total payload bytes for ALT strings)
//   U32LE             info_total   (total payload bytes for INFO strings)
//   U8[num_variants]  chrom_ids    (0=chr1..21=chr22, 22=chrX, 23=chrY,
//                                   24=chrM/chrMT, 25=other)
//   U32LE[num]        positions    (POS field, 1-based)
//   U8[num_variants]  qual_present (1 if QUAL is a number, 0 if '.')
//   F32LE[num]        quals        (QUAL as float32; 0.0 when qual_present=0)
//   U8[num_variants]  filter_pass  (0=PASS, 1=filtered, 2='.')
//   U32LE[num]        ref_lens     (byte length of each REF string)
//   U32LE[num]        alt_lens     (byte length of each ALT string)
//   U32LE[num]        info_lens    (byte length of each INFO string)
//   Byte[ref_total]   ref_payload  (concatenated REF strings, no null)
//   Byte[alt_total]   alt_payload  (concatenated ALT strings, no null)
//   Byte[info_total]  info_payload (concatenated INFO strings, no null)

#include <algorithm>
#include <atomic>
#include <cassert>
#include <cmath>
#include <cstring>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/mman.h>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>
#include <vector>

namespace fs = std::filesystem;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static void write_u8(std::ofstream &out, uint8_t v)
{
    out.write(reinterpret_cast<const char *>(&v), 1);
}

static void write_u32le(std::ofstream &out, uint32_t v)
{
    uint8_t b[4] = {
        static_cast<uint8_t>(v),
        static_cast<uint8_t>(v >> 8),
        static_cast<uint8_t>(v >> 16),
        static_cast<uint8_t>(v >> 24),
    };
    out.write(reinterpret_cast<const char *>(b), 4);
}

static void write_f32le(std::ofstream &out, float v)
{
    uint32_t bits;
    std::memcpy(&bits, &v, 4);
    write_u32le(out, bits);
}

// ---------------------------------------------------------------------------
// Chromosome name → uint8 index
// ---------------------------------------------------------------------------

static uint8_t chrom_to_id(const std::string &chrom)
{
    // strip optional "chr" prefix
    const std::string &s = chrom;
    std::string name = s;
    if (name.size() > 3 && name.substr(0, 3) == "chr")
    {
        name = name.substr(3);
    }
    if (name == "X")
        return 22;
    if (name == "Y")
        return 23;
    if (name == "M" || name == "MT")
        return 24;
    try
    {
        unsigned long n = std::stoul(name);
        if (n >= 1 && n <= 22)
            return static_cast<uint8_t>(n - 1);
    }
    catch (...)
    {
    }
    return 25; // other
}

// ---------------------------------------------------------------------------
// Mmap helper
// ---------------------------------------------------------------------------

struct MappedFile
{
    const char *data = nullptr;
    size_t size = 0;
    int fd = -1;

    explicit MappedFile(const std::string &path)
    {
        fd = open(path.c_str(), O_RDONLY);
        if (fd == -1)
            throw std::runtime_error("Cannot open: " + path);
        struct stat sb;
        if (fstat(fd, &sb) == -1)
        {
            close(fd);
            throw std::runtime_error("Cannot stat: " + path);
        }
        size = static_cast<size_t>(sb.st_size);
        if (size == 0)
        {
            data = nullptr;
            return;
        }
        data = static_cast<const char *>(
            mmap(nullptr, size, PROT_READ, MAP_PRIVATE, fd, 0));
        if (data == MAP_FAILED)
        {
            close(fd);
            throw std::runtime_error("mmap failed for: " + path);
        }
        madvise(const_cast<char *>(data), size, MADV_SEQUENTIAL);
    }

    ~MappedFile()
    {
        if (data && data != MAP_FAILED)
            munmap(const_cast<char *>(data), size);
        if (fd != -1)
            close(fd);
    }

    // Non-copyable
    MappedFile(const MappedFile &) = delete;
    MappedFile &operator=(const MappedFile &) = delete;
};

// ---------------------------------------------------------------------------
// VCF line parser – returns false when past data region
// ---------------------------------------------------------------------------

struct VcfRecord
{
    uint8_t chrom_id;
    uint32_t pos;
    uint8_t qual_present;
    float qual;
    uint8_t filter_pass;
    std::string ref;
    std::string alt;
    std::string info;
};

// Split a tab-delimited line, filling exactly `n` fields (extras ignored).
static bool split_tabs(const char *line, size_t len, std::vector<std::string> &fields, int n)
{
    fields.clear();
    const char *p = line;
    const char *end = line + len;
    while (p < end && static_cast<int>(fields.size()) < n)
    {
        const char *tab = static_cast<const char *>(memchr(p, '\t', end - p));
        if (!tab)
        {
            fields.emplace_back(p, end - p);
            ++p;
            break;
        }
        fields.emplace_back(p, tab - p);
        p = tab + 1;
    }
    return static_cast<int>(fields.size()) >= n;
}

static bool parse_vcf_line(const char *line, size_t len, VcfRecord &rec)
{
    std::vector<std::string> f;
    if (!split_tabs(line, len, f, 8))
        return false; // need at least 8 columns

    // CHROM (col 0)
    rec.chrom_id = chrom_to_id(f[0]);

    // POS (col 1)
    try
    {
        rec.pos = static_cast<uint32_t>(std::stoul(f[1]));
    }
    catch (...)
    {
        rec.pos = 0;
    }

    // REF (col 3)
    rec.ref = std::move(f[3]);

    // ALT (col 4)
    rec.alt = std::move(f[4]);

    // QUAL (col 5)
    if (f[5] == "." || f[5].empty())
    {
        rec.qual_present = 0;
        rec.qual = 0.0f;
    }
    else
    {
        try
        {
            rec.qual = std::stof(f[5]);
            rec.qual_present = 1;
        }
        catch (...)
        {
            rec.qual_present = 0;
            rec.qual = 0.0f;
        }
    }

    // FILTER (col 6)
    if (f[6] == "PASS")
    {
        rec.filter_pass = 0;
    }
    else if (f[6] == ".")
    {
        rec.filter_pass = 2;
    }
    else
    {
        rec.filter_pass = 1;
    }

    // INFO (col 7)
    rec.info = std::move(f[7]);

    return true;
}

// ---------------------------------------------------------------------------
// Chunk writer
// ---------------------------------------------------------------------------

struct Chunk
{
    std::vector<uint8_t> chrom_ids;
    std::vector<uint32_t> positions;
    std::vector<uint8_t> qual_present;
    std::vector<float> quals;
    std::vector<uint8_t> filter_pass;
    std::vector<uint32_t> ref_lens;
    std::vector<uint32_t> alt_lens;
    std::vector<uint32_t> info_lens;
    std::string ref_payload;
    std::string alt_payload;
    std::string info_payload;

    void push(const VcfRecord &rec)
    {
        chrom_ids.push_back(rec.chrom_id);
        positions.push_back(rec.pos);
        qual_present.push_back(rec.qual_present);
        quals.push_back(rec.qual);
        filter_pass.push_back(rec.filter_pass);
        ref_lens.push_back(static_cast<uint32_t>(rec.ref.size()));
        alt_lens.push_back(static_cast<uint32_t>(rec.alt.size()));
        info_lens.push_back(static_cast<uint32_t>(rec.info.size()));
        ref_payload += rec.ref;
        alt_payload += rec.alt;
        info_payload += rec.info;
    }

    size_t num_variants() const { return chrom_ids.size(); }

    // Estimated serialised size in bytes (used for chunking decisions)
    size_t estimated_bytes() const
    {
        size_t n = num_variants();
        if (n == 0)
            return 0;
        return 4       // magic
               + 5 * 4 // num_variants + 3 totals
               + n     // chrom_ids
               + n * 4 // positions
               + n     // qual_present
               + n * 4 // quals
               + n     // filter_pass
               + n * 4 // ref_lens
               + n * 4 // alt_lens
               + n * 4 // info_lens
               + ref_payload.size() + alt_payload.size() + info_payload.size();
    }

    void clear()
    {
        chrom_ids.clear();
        positions.clear();
        qual_present.clear();
        quals.clear();
        filter_pass.clear();
        ref_lens.clear();
        alt_lens.clear();
        info_lens.clear();
        ref_payload.clear();
        alt_payload.clear();
        info_payload.clear();
    }
};

static bool write_chunk(const Chunk &c, const std::string &path)
{
    size_t n = c.num_variants();
    if (n == 0)
        return true;

    std::ofstream out(path, std::ios::binary);
    if (!out.is_open())
    {
        std::cerr << "Error: cannot open for write: " << path << "\n";
        return false;
    }

    // magic
    out.write("VCF1", 4);

    // header counts
    write_u32le(out, static_cast<uint32_t>(n));
    write_u32le(out, static_cast<uint32_t>(c.ref_payload.size()));
    write_u32le(out, static_cast<uint32_t>(c.alt_payload.size()));
    write_u32le(out, static_cast<uint32_t>(c.info_payload.size()));

    // columnar arrays
    out.write(reinterpret_cast<const char *>(c.chrom_ids.data()), n);
    for (size_t i = 0; i < n; ++i)
        write_u32le(out, c.positions[i]);
    out.write(reinterpret_cast<const char *>(c.qual_present.data()), n);
    for (size_t i = 0; i < n; ++i)
        write_f32le(out, c.quals[i]);
    out.write(reinterpret_cast<const char *>(c.filter_pass.data()), n);
    for (size_t i = 0; i < n; ++i)
        write_u32le(out, c.ref_lens[i]);
    for (size_t i = 0; i < n; ++i)
        write_u32le(out, c.alt_lens[i]);
    for (size_t i = 0; i < n; ++i)
        write_u32le(out, c.info_lens[i]);

    // payload blobs
    if (!c.ref_payload.empty())
        out.write(c.ref_payload.data(), static_cast<std::streamsize>(c.ref_payload.size()));
    if (!c.alt_payload.empty())
        out.write(c.alt_payload.data(), static_cast<std::streamsize>(c.alt_payload.size()));
    if (!c.info_payload.empty())
        out.write(c.info_payload.data(), static_cast<std::streamsize>(c.info_payload.size()));

    out.close();
    return out.good();
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

int main(int argc, char *argv[])
{
    if (argc < 4)
    {
        std::cerr << "Usage: " << argv[0]
                  << " <input.vcf> <output_dir> <num_threads>\n"
                  << "  Reads VCF and writes columnar VCF1 chunks to output_dir.\n";
        return 1;
    }

    const std::string input_path = argv[1];
    const std::string output_dir = argv[2];
    const int num_threads = std::max(1, std::stoi(argv[3]));
    const size_t chunk_bytes = 300UL * 1024UL * 1024UL; // 300 MiB cap (matches biocompress_preprocessor VCF cap)

    fs::create_directories(output_dir);

    // Memory-map the input file
    MappedFile mf(input_path);
    if (!mf.data || mf.size == 0)
    {
        std::cerr << "Error: empty or unreadable input: " << input_path << "\n";
        return 1;
    }

    const char *file_end = mf.data + mf.size;

    // Skip VCF header lines to locate start of data records
    const char *data_start = mf.data;
    while (data_start < file_end)
    {
        if (*data_start != '#')
            break;
        const char *nl = static_cast<const char *>(memchr(data_start, '\n', file_end - data_start));
        data_start = nl ? nl + 1 : file_end;
    }

    if (data_start >= file_end)
    {
        std::cout << "Parsed 0 variants into 0 chunk(s) in " << output_dir << "\n";
        return 0;
    }

    const size_t data_size = static_cast<size_t>(file_end - data_start);

    // Calculate chunk count: at least num_threads, and at least enough so
    // each chunk is <= chunk_bytes. This mirrors biocompress_preprocessor.
    const size_t size_based_chunks = (data_size + chunk_bytes - 1) / chunk_bytes;
    const size_t chunk_count = std::max(
        static_cast<size_t>(num_threads),
        std::max(size_based_chunks, static_cast<size_t>(1)));

    const size_t target_chunk_size = (data_size + chunk_count - 1) / chunk_count;

    // Find split points at VCF record boundaries (beginning of a data line)
    std::vector<const char *> split_points;
    split_points.reserve(chunk_count + 1);
    split_points.push_back(data_start);

    for (size_t i = 1; i < chunk_count; ++i)
    {
        const char *target = data_start + i * target_chunk_size;
        if (target >= file_end)
        {
            split_points.push_back(file_end);
            continue;
        }
        // Walk forward to the next newline to align to a record boundary
        const char *cursor = target;
        while (cursor < file_end && *(cursor - 1) != '\n')
            ++cursor;
        // Extra safety: skip any stray comment lines
        while (cursor < file_end && *cursor == '#')
        {
            const char *nl = static_cast<const char *>(memchr(cursor, '\n', file_end - cursor));
            cursor = nl ? nl + 1 : file_end;
        }
        split_points.push_back(cursor);
    }
    split_points.push_back(file_end);

    // Process each segment in parallel — one segment produces one output chunk
    const size_t total_segments = split_points.size() - 1;
    const size_t worker_count = std::max(
        static_cast<size_t>(1),
        std::min(total_segments, static_cast<size_t>(num_threads)));

    std::atomic<size_t> next_segment{0};
    std::atomic<size_t> total_variants{0};
    std::atomic<size_t> total_chunks_written{0};
    std::atomic<size_t> total_skipped{0};

    auto process_segment = [&](size_t seg_idx)
    {
        const char *seg_start = split_points[seg_idx];
        const char *seg_end = split_points[seg_idx + 1];
        if (seg_start >= seg_end)
            return;

        Chunk cur;
        size_t seg_skipped = 0;

        const char *p = seg_start;
        while (p < seg_end)
        {
            const char *nl = static_cast<const char *>(memchr(p, '\n', seg_end - p));
            const char *line_end = nl ? nl : seg_end;
            size_t len = static_cast<size_t>(line_end - p);

            if (len > 0 && p[0] == '#')
            {
                p = nl ? nl + 1 : seg_end;
                continue;
            }
            if (len == 0)
            {
                p = nl ? nl + 1 : seg_end;
                continue;
            }

            VcfRecord rec;
            if (!parse_vcf_line(p, len, rec))
            {
                ++seg_skipped;
                p = nl ? nl + 1 : seg_end;
                continue;
            }

            cur.push(rec);
            p = nl ? nl + 1 : seg_end;
        }

        if (cur.num_variants() == 0)
            return;

        std::ostringstream ss;
        ss << output_dir << "/chunk_"
           << std::setfill('0') << std::setw(5) << seg_idx
           << ".vcf_columnar.bin";

        if (!write_chunk(cur, ss.str()))
        {
            std::cerr << "Error: failed to write chunk " << seg_idx << "\n";
            return;
        }

        total_variants += cur.num_variants();
        total_chunks_written += 1;
        total_skipped += seg_skipped;
    };

    // Launch worker threads (work-stealing via atomic counter)
    std::vector<std::thread> threads;
    threads.reserve(worker_count);
    for (size_t t = 0; t < worker_count; ++t)
    {
        threads.emplace_back([&]()
                             {
            while (true)
            {
                size_t idx = next_segment.fetch_add(1);
                if (idx >= total_segments) break;
                process_segment(idx);
            } });
    }
    for (auto &t : threads)
        t.join();

    std::cout << "Parsed " << total_variants.load() << " variants into "
              << total_chunks_written.load() << " chunk(s) in " << output_dir << "\n";
    if (total_skipped.load() > 0)
    {
        std::cout << "Skipped (parse errors): " << total_skipped.load() << " lines\n";
    }
    return 0;
}
