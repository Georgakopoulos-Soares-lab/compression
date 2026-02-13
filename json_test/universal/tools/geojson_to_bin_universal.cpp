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

#include "../../../openzl/tools/json.hpp"

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

// Global point counter to debug
size_t DEBUG_TOTAL_POINTS = 0;

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

// A Dynamic Column that holds string data and offsets
struct Column {
    std::string name;
    std::vector<char> data;
    std::vector<uint32_t> offsets;

    Column(std::string n) : name(std::move(n)) {
        offsets.push_back(0);
    }

    void add_value(const json& val) {
        std::string s;
        if (val.is_null()) s = "";
        else if (val.is_string()) s = val.get<std::string>();
        else s = val.dump();
        
        data.insert(data.end(), s.begin(), s.end());
        offsets.push_back(data.size());
    }
    
    void add_empty() {
        offsets.push_back(data.size());
    }

    void clear() {
        data.clear();
        offsets.clear();
        offsets.push_back(0);
    }
};

struct UniversalBuilder {
    std::vector<Column> columns;
    std::vector<uint32_t> geom_offsets = {0};
    
    std::vector<double> px, py, pz;
    uint32_t num_features = 0;

    UniversalBuilder(const std::vector<std::string>& field_names) {
        for (const auto& f : field_names) {
            columns.emplace_back(f);
        }
    }

    void clear() {
        for (auto& col : columns) col.clear();
        geom_offsets = {0};
        px.clear(); py.clear(); pz.clear();
        num_features = 0;
    }

    size_t estimated_size() const {
        size_t sz = 0;
        for (const auto& col : columns) {
            sz += col.data.size();
            sz += col.offsets.size() * 4;
        }
        sz += px.size() * 24; // coords
        return sz;
    }

    void add_feature(const json& feat) {
        const auto& props = feat.contains("properties") ? feat["properties"] : json::object();
        
        // For each known column, find the value in properties
        for (auto& col : columns) {
            if (props.contains(col.name)) {
                col.add_value(props[col.name]);
            } else {
                col.add_empty();
            }
        }

        if (feat.contains("geometry") && !feat["geometry"].is_null()) {
            const auto& geom = feat["geometry"];
            if (geom.contains("coordinates")) {
                flatten_coords(geom["coordinates"], px, py, pz);
            }
        }
        geom_offsets.push_back(px.size());
        num_features++;
    }

    void write(const std::string& path) {
        std::ofstream out(path, std::ios::binary);
        if (!out.is_open()) throw std::runtime_error("Cannot open " + path);

        out.write("GEO1", 4);
        write_u32(out, num_features);

        // 1. Offsets
        for (const auto& col : columns) {
            out.write((const char*)col.offsets.data(), col.offsets.size() * 4);
        }
        out.write((const char*)geom_offsets.data(), geom_offsets.size() * 4);

        // 2. Lengths
        for (const auto& col : columns) {
            write_u32(out, col.data.size());
        }
        write_u32(out, px.size()); // total_points

        // 3. Padding Sizes
        for (const auto& col : columns) {
            write_u32(out, get_pad_size(col.data.size()));
        }
        if (columns.size() % 2 != 0) {
            write_u32(out, 0); // Align to 8 bytes for data section
        }

        // 4. Data Streams
        for (const auto& col : columns) {
            out.write(col.data.data(), col.data.size());
            pad_stream(out, col.data.size());
        }

        // 5. Geometry
        out.write((const char*)px.data(), px.size() * 8);
        out.write((const char*)py.data(), py.size() * 8); // Re-enabled Y
        out.write((const char*)pz.data(), pz.size() * 8); // Re-enabled Z

        out.close();
    }
};

int main(int argc, char* argv[]) {
    if (argc < 4) {
        std::cerr << "Usage: " << argv[0] << " <input.json> <mapping.json> <output_dir> [chunk_mb]" << std::endl;
        return 1;
    }

    std::string input_path = argv[1];
    std::string map_path = argv[2];
    std::string output_dir = argv[3];
    size_t chunk_mb = (argc > 4) ? std::stoul(argv[4]) : 200;
    size_t chunk_bytes = chunk_mb * 1024 * 1024;

    try {
        fs::create_directories(output_dir);

        // Load Mapping
        std::ifstream fmap(map_path);
        if (!fmap.is_open()) return 1;
        json mapping = json::parse(fmap);
        std::vector<std::string> fields = mapping["fields"].get<std::vector<std::string>>();
        std::cout << "Loaded mapping with " << fields.size() << " fields." << std::endl;

        UniversalBuilder builder(fields);

        // Load Data
        std::ifstream f(input_path);
        json data = json::parse(f);
        const auto& features = data["features"];
        
        size_t chunk_idx = 0;
        size_t idx = 0;
        size_t total = features.size();

        std::cout << "Processing " << total << " features..." << std::endl;

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
            
            if (idx % 10000 == 0) std::cout << "  " << idx << "/" << total << std::endl;
            idx++;
        }

        if (builder.num_features > 0) {
            char filename[256];
            snprintf(filename, sizeof(filename), "chunk_%05zu.bin", chunk_idx++);
            std::string path = output_dir + "/" + filename;
            std::cout << "Writing last chunk " << path << " (" << builder.num_features << " records)..." << std::endl;
            builder.write(path);
        }

        std::cout << "Done." << std::endl;

    } catch (const std::exception& e) {
        std::cerr << "Error: " << e.what() << std::endl;
        return 1;
    }
    return 0;
}
