#include <iostream>
#include <fstream>
#include <vector>
#include <string>
#include <algorithm>
#include <cstring>
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <filesystem>

// Include the JSON library found in the project
#include "../../openzl/tools/json.hpp"

namespace fs = std::filesystem;
using json = nlohmann::json;

// ============================================================================
// Utilities
// ============================================================================

void write_u32(std::ofstream& out, uint32_t val) {
    out.write(reinterpret_cast<const char*>(&val), 4);
}

void pad_stream(std::ofstream& out, size_t size, size_t align = 8) {
    size_t rem = size % align;
    if (rem != 0) {
        size_t pad = align - rem;
        static const char zeros[16] = {0};
        out.write(zeros, pad);
    }
}

uint32_t get_pad_size(size_t size, size_t align = 8) {
    size_t rem = size % align;
    return (rem == 0) ? 0 : (align - rem);
}

// ============================================================================
// Logic
// ============================================================================

void flatten_coords(const json& coords, std::vector<double>& px, std::vector<double>& py, std::vector<double>& pz) {
    if (coords.empty()) return;

    if (coords[0].is_number()) {
        px.push_back(coords[0].get<double>());
        py.push_back(coords[1].get<double>());
        if (coords.size() > 2) {
            pz.push_back(coords[2].get<double>());
        } else {
            pz.push_back(0.0);
        }
        return;
    }

    for (const auto& sub : coords) {
        flatten_coords(sub, px, py, pz);
    }
}

void process_str(const json& props, const std::string& key, std::vector<char>& buffer, std::vector<uint32_t>& offsets) {
    std::string val_str;
    if (props.contains(key) && !props[key].is_null()) {
        const auto& val = props[key];
        if (val.is_string()) {
            val_str = val.get<std::string>();
        } else {
            val_str = val.dump();
        }
    } else {
        val_str = "";
    }
    
    buffer.insert(buffer.end(), val_str.begin(), val_str.end());
    offsets.push_back(buffer.size());
}

struct ChunkBuilder {
    // Buffers
    std::vector<char> mapblklot_data;
    std::vector<char> blklot_data;
    std::vector<char> block_num_data;
    std::vector<char> lot_num_data;
    std::vector<char> from_st_data;
    std::vector<char> to_st_data;
    std::vector<char> street_data;
    std::vector<char> st_type_data;
    std::vector<char> odd_even_data;

    // Offsets
    std::vector<uint32_t> mapblklot_offsets = {0};
    std::vector<uint32_t> blklot_offsets = {0};
    std::vector<uint32_t> block_num_offsets = {0};
    std::vector<uint32_t> lot_num_offsets = {0};
    std::vector<uint32_t> from_st_offsets = {0};
    std::vector<uint32_t> to_st_offsets = {0};
    std::vector<uint32_t> street_offsets = {0};
    std::vector<uint32_t> st_type_offsets = {0};
    std::vector<uint32_t> odd_even_offsets = {0};
    
    std::vector<uint32_t> geom_offsets = {0};

    // Coordinates
    std::vector<double> points_x;
    std::vector<double> points_y;
    std::vector<double> points_z;
    
    uint32_t num_features = 0;

    void clear() {
        mapblklot_data.clear(); blklot_data.clear(); block_num_data.clear();
        lot_num_data.clear(); from_st_data.clear(); to_st_data.clear();
        street_data.clear(); st_type_data.clear(); odd_even_data.clear();
        
        mapblklot_offsets = {0}; blklot_offsets = {0}; block_num_offsets = {0};
        lot_num_offsets = {0}; from_st_offsets = {0}; to_st_offsets = {0};
        street_offsets = {0}; st_type_offsets = {0}; odd_even_offsets = {0};
        geom_offsets = {0};
        
        points_x.clear(); points_y.clear(); points_z.clear();
        num_features = 0;
    }
    
    size_t estimated_size() const {
        return mapblklot_data.size() + blklot_data.size() + block_num_data.size() +
               lot_num_data.size() + from_st_data.size() + to_st_data.size() +
               street_data.size() + st_type_data.size() + odd_even_data.size() +
               (points_x.size() * 24) + // coords
               (num_features * 4 * 10); // offsets approx
    }
    
    void add_feature(const json& feat) {
        const auto& props = feat["properties"];
        
        process_str(props, "MAPBLKLOT", mapblklot_data, mapblklot_offsets);
        process_str(props, "BLKLOT", blklot_data, blklot_offsets);
        process_str(props, "BLOCK_NUM", block_num_data, block_num_offsets);
        process_str(props, "LOT_NUM", lot_num_data, lot_num_offsets);
        process_str(props, "FROM_ST", from_st_data, from_st_offsets);
        process_str(props, "TO_ST", to_st_data, to_st_offsets);
        process_str(props, "STREET", street_data, street_offsets);
        process_str(props, "ST_TYPE", st_type_data, st_type_offsets);
        process_str(props, "ODD_EVEN", odd_even_data, odd_even_offsets);

        if (feat.contains("geometry") && !feat["geometry"].is_null()) {
            const auto& geom = feat["geometry"];
            if (geom.contains("coordinates")) {
                flatten_coords(geom["coordinates"], points_x, points_y, points_z);
            }
        }
        geom_offsets.push_back(points_x.size());
        num_features++;
    }
    
    void write(const std::string& path) {
        std::ofstream out(path, std::ios::binary);
        if (!out.is_open()) throw std::runtime_error("Cannot open " + path);

        // Magic
        out.write("CITY", 4);
        write_u32(out, num_features);

        // Offsets
        out.write((const char*)mapblklot_offsets.data(), mapblklot_offsets.size() * 4);
        out.write((const char*)blklot_offsets.data(), blklot_offsets.size() * 4);
        out.write((const char*)block_num_offsets.data(), block_num_offsets.size() * 4);
        out.write((const char*)lot_num_offsets.data(), lot_num_offsets.size() * 4);
        out.write((const char*)from_st_offsets.data(), from_st_offsets.size() * 4);
        out.write((const char*)to_st_offsets.data(), to_st_offsets.size() * 4);
        out.write((const char*)street_offsets.data(), street_offsets.size() * 4);
        out.write((const char*)st_type_offsets.data(), st_type_offsets.size() * 4);
        out.write((const char*)odd_even_offsets.data(), odd_even_offsets.size() * 4);
        out.write((const char*)geom_offsets.data(), geom_offsets.size() * 4);

        // Lengths
        uint32_t total_points = points_x.size();
        write_u32(out, mapblklot_data.size());
        write_u32(out, blklot_data.size());
        write_u32(out, block_num_data.size());
        write_u32(out, lot_num_data.size());
        write_u32(out, from_st_data.size());
        write_u32(out, to_st_data.size());
        write_u32(out, street_data.size());
        write_u32(out, st_type_data.size());
        write_u32(out, odd_even_data.size());
        write_u32(out, total_points);

        // Padding
        write_u32(out, get_pad_size(mapblklot_data.size()));
        write_u32(out, get_pad_size(blklot_data.size()));
        write_u32(out, get_pad_size(block_num_data.size()));
        write_u32(out, get_pad_size(lot_num_data.size()));
        write_u32(out, get_pad_size(from_st_data.size()));
        write_u32(out, get_pad_size(to_st_data.size()));
        write_u32(out, get_pad_size(street_data.size()));
        write_u32(out, get_pad_size(st_type_data.size()));
        write_u32(out, get_pad_size(odd_even_data.size()));

        // Data
        out.write(mapblklot_data.data(), mapblklot_data.size()); pad_stream(out, mapblklot_data.size());
        out.write(blklot_data.data(), blklot_data.size()); pad_stream(out, blklot_data.size());
        out.write(block_num_data.data(), block_num_data.size()); pad_stream(out, block_num_data.size());
        out.write(lot_num_data.data(), lot_num_data.size()); pad_stream(out, lot_num_data.size());
        out.write(from_st_data.data(), from_st_data.size()); pad_stream(out, from_st_data.size());
        out.write(to_st_data.data(), to_st_data.size()); pad_stream(out, to_st_data.size());
        out.write(street_data.data(), street_data.size()); pad_stream(out, street_data.size());
        out.write(st_type_data.data(), st_type_data.size()); pad_stream(out, st_type_data.size());
        out.write(odd_even_data.data(), odd_even_data.size()); pad_stream(out, odd_even_data.size());

        // Coordinates
        out.write((const char*)points_x.data(), points_x.size() * 8);
        out.write((const char*)points_y.data(), points_y.size() * 8);
        out.write((const char*)points_z.data(), points_z.size() * 8);

        out.close();
    }
};

int main(int argc, char* argv[]) {
    if (argc < 3) {
        std::cerr << "Usage: " << argv[0] << " <input.json> <output_dir> [chunk_mb]" << std::endl;
        return 1;
    }

    std::string input_path = argv[1];
    std::string output_dir = argv[2];
    size_t chunk_mb = (argc > 3) ? std::stoul(argv[3]) : 200;
    size_t chunk_bytes = chunk_mb * 1024 * 1024;

    std::cout << "Reading " << input_path << "..." << std::endl;
    
    try {
        fs::create_directories(output_dir);
        
        std::ifstream f(input_path);
        if (!f.is_open()) {
            std::cerr << "Could not open " << input_path << std::endl;
            return 1;
        }
        
        json data = json::parse(f);
        
        if (!data.contains("features") || !data["features"].is_array()) {
            std::cerr << "JSON must contain 'features' array." << std::endl;
            return 1;
        }

        const auto& features = data["features"];
        uint32_t num_features = features.size();
        std::cout << "Found " << num_features << " features." << std::endl;

        ChunkBuilder builder;
        size_t chunk_idx = 0;
        
        std::cout << "Processing features..." << std::endl;
        size_t idx = 0;
        for (const auto& feat : features) {
            builder.add_feature(feat);
            
            if (builder.estimated_size() >= chunk_bytes) {
                char filename[256];
                snprintf(filename, sizeof(filename), "chunk_%05zu.bin", chunk_idx++);
                std::string path = output_dir + "/" + filename;
                std::cout << "Writing chunk " << path << " (" << builder.num_features << " records)..." << std::endl;
                builder.write(path);
                builder.clear();
            }

            if (idx % 10000 == 0) {
                std::cout << "  Processed " << idx << "/" << num_features << "..." << std::endl;
            }
            idx++;
        }

        if (builder.num_features > 0) {
            char filename[256];
            snprintf(filename, sizeof(filename), "chunk_%05zu.bin", chunk_idx++);
            std::string path = output_dir + "/" + filename;
            std::cout << "Writing last chunk " << path << " (" << builder.num_features << " records)..." << std::endl;
            builder.write(path);
        }

        std::cout << "Done. Output in " << output_dir << std::endl;

    } catch (const std::exception& e) {
        std::cerr << "Error: " << e.what() << std::endl;
        return 1;
    }

    return 0;
}
