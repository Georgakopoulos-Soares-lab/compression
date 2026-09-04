// harc_decode.cpp
//
// Decodes a chunk_NNNNN.harc_packed.bin (HRC2 format, see harc_preprocessor.cpp)
// back into per-record base sequences, in ORIGINAL genome order, and writes
// them as unwrapped FASTA (one line per sequence, no line-wrapping) so a
// simple text/header-keyed diff can validate correctness.
//
// This validates the HARC encode/decode round trip itself (reordering +
// delta encoding), independent of whatever happens to it afterwards
// (OpenZL compress/decompress, or a generic compressor) -- exactly the same
// spirit as the existing pipeline's `cmp` check on FAV4 .bin round trips,
// just done at the sequence level here because HARC's on-disk layout is not
// a byte-identical passthrough of the input the way FAV4's is.
//
// Usage:
//   harc_decode <input.harc_packed.bin> <output.fasta>

#include "harc_common.h"
#include <cstdio>
#include <iostream>

int main(int argc, char** argv) {
    if (argc < 3) {
        std::fprintf(stderr, "usage: %s <input.harc_packed.bin> <output.fasta>\n", argv[0]);
        return 1;
    }
    std::string in_path = argv[1];
    std::string out_path = argv[2];

    try {
        std::vector<uint8_t> buf = read_whole_file(in_path);
        ByteReader r(buf.data(), buf.size());

        char magic[5] = {0};
        std::memcpy(magic, r.bytes(4), 4);
        if (std::strcmp(magic, "HRC2") != 0) throw std::runtime_error("bad magic in " + in_path + " (got " + magic + ")");

        uint32_t num_records = r.u32();
        std::vector<uint32_t> hdr_offsets(num_records + 1);
        for (auto& v : hdr_offsets) v = r.u32();
        uint32_t hdr_total = r.u32(), hdr_pad = r.u32();
        const uint8_t* headers = r.bytes(hdr_total + hdr_pad);

        uint32_t read_len = r.u32();
        (void)read_len;
        uint32_t num_reads = r.u32();
        std::vector<uint32_t> rec_read_offsets(num_records + 1);
        for (auto& v : rec_read_offsets) v = r.u32();
        std::vector<uint32_t> read_lengths(num_reads);
        for (auto& v : read_lengths) v = r.u32();
        std::vector<uint32_t> order_to_orig(num_reads);
        for (auto& v : order_to_orig) v = r.u32();

        const uint8_t* flags = num_reads ? r.bytes(num_reads) : nullptr;
        std::vector<uint16_t> ov_start_cur(num_reads), ov_start_prev(num_reads), ov_len_col(num_reads), mismatch_counts(num_reads);
        for (auto& v : ov_start_cur) v = r.u16();
        for (auto& v : ov_start_prev) v = r.u16();
        for (auto& v : ov_len_col) v = r.u16();
        for (auto& v : mismatch_counts) v = r.u16();

        uint32_t raw_pool_total = r.u32(), raw_pool_pad = r.u32();
        uint32_t prefix_pool_total = r.u32(), prefix_pool_pad = r.u32();
        uint32_t suffix_pool_total = r.u32(), suffix_pool_pad = r.u32();
        uint32_t mpos_total = r.u32(), mpos_pad = r.u32();
        uint32_t mbase_total = r.u32(), mbase_pad = r.u32();

        const uint8_t* raw_pool = r.bytes(raw_pool_total + raw_pool_pad);
        const uint8_t* prefix_pool = r.bytes(prefix_pool_total + prefix_pool_pad);
        const uint8_t* suffix_pool = r.bytes(suffix_pool_total + suffix_pool_pad);
        const uint8_t* mpos_bytes = r.bytes(mpos_total + mpos_pad);
        const uint8_t* mbase_bytes = r.bytes(mbase_total + mbase_pad);

        // ---- reconstruct reads in REORDERED order, tracking prev for deltas ----
        std::vector<std::string> read_seq_by_orig(num_reads);
        size_t raw_cursor = 0, prefix_cursor = 0, suffix_cursor = 0;
        size_t mpos_cursor = 0, mbase_cursor = 0; // mbase_cursor is a BYTE offset: each read's mismatch bases
                                                   // restart nibble-alignment at a fresh byte, same as raw/prefix/suffix pools
        std::string prev;
        for (uint32_t oi = 0; oi < num_reads; oi++) {
            uint32_t orig_idx = order_to_orig[oi];
            uint32_t len = read_lengths[orig_idx];
            std::string cur;
            if (flags[oi] == 0) {
                size_t packed_len = (len + 1) / 2;
                cur = unpack_bases(raw_pool + raw_cursor, len);
                raw_cursor += packed_len;
            } else {
                uint32_t osc = ov_start_cur[oi], osp = ov_start_prev[oi], ovl = ov_len_col[oi];
                if (osp + ovl > prev.size()) throw std::runtime_error("overlap out of range decoding " + in_path);
                uint32_t suffix_begin = osc + ovl;
                uint32_t prefix_len = osc, suffix_len = len - suffix_begin;

                cur.resize(len);
                // prefix (literal)
                std::string pfx = unpack_bases(prefix_pool + prefix_cursor, prefix_len);
                prefix_cursor += (prefix_len + 1) / 2;
                std::copy(pfx.begin(), pfx.end(), cur.begin());
                // overlap (copied from prev, then mismatches applied)
                std::copy(prev.begin() + osp, prev.begin() + osp + ovl, cur.begin() + osc);
                // suffix (literal)
                std::string sfx = unpack_bases(suffix_pool + suffix_cursor, suffix_len);
                suffix_cursor += (suffix_len + 1) / 2;
                std::copy(sfx.begin(), sfx.end(), cur.begin() + suffix_begin);

                uint16_t mc = mismatch_counts[oi];
                std::vector<uint16_t> positions(mc);
                for (uint16_t k = 0; k < mc; k++) {
                    uint16_t p = (uint16_t)(mpos_bytes[mpos_cursor] | (mpos_bytes[mpos_cursor + 1] << 8));
                    mpos_cursor += 2;
                    positions[k] = p;
                }
                for (uint16_t k = 0; k < mc; k++) {
                    uint8_t byte = mbase_bytes[mbase_cursor + k / 2];
                    uint8_t code = (k % 2 == 0) ? (byte >> 4) : (byte & 0x0F);
                    cur[osc + positions[k]] = code_to_base(code);
                }
                mbase_cursor += (mc + 1) / 2;
            }
            read_seq_by_orig[orig_idx] = cur;
            prev = cur;
        }

        // ---- reassemble records in original order ----
        std::ofstream out(out_path, std::ios::binary);
        if (!out) throw std::runtime_error("cannot open for write " + out_path);
        for (uint32_t rec = 0; rec < num_records; rec++) {
            std::string header((const char*)headers + hdr_offsets[rec], hdr_offsets[rec + 1] - hdr_offsets[rec]);
            out << ">" << header << "\n";
            for (uint32_t ri = rec_read_offsets[rec]; ri < rec_read_offsets[rec + 1]; ri++) {
                out << read_seq_by_orig[ri];
            }
            out << "\n";
        }

        std::fprintf(stderr, "harc_decode: %s -> %s (%u records, %u reads)\n",
                      in_path.c_str(), out_path.c_str(), num_records, num_reads);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "Error: %s\n", e.what());
        return 1;
    }
    return 0;
}
