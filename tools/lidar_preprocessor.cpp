#include <iostream>
#include <fstream>
#include <vector>
#include <string>
#include <cstring>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>
#include <iomanip>
#include <sstream>

// KITTI Velodyne .bin format: interleaved x, y, z, intensity (all float32)
struct Point { float x, y, z, intensity; };

int main(int argc, char* argv[]) {
    if (argc < 4) {
        std::cerr << "Usage: " << argv[0]
                  << " <input.bin> <output_dir> <chunk_size_mb>" << std::endl;
        std::cerr << "  Reads interleaved KITTI LiDAR .bin, writes columnar LID1 chunks." << std::endl;
        return 1;
    }

    const std::string input_path = argv[1];
    const std::string output_dir = argv[2];
    const size_t chunk_bytes = std::stoul(argv[3]) * 1024UL * 1024UL;

    int fd = open(input_path.c_str(), O_RDONLY);
    if (fd == -1) {
        perror("Error opening input");
        return 1;
    }

    struct stat sb;
    if (fstat(fd, &sb) == -1) {
        perror("fstat");
        close(fd);
        return 1;
    }

    const size_t total_size = sb.st_size;
    if (total_size % sizeof(Point) != 0) {
        std::cerr << "Error: file size (" << total_size
                  << ") is not a multiple of " << sizeof(Point)
                  << " bytes" << std::endl;
        close(fd);
        return 1;
    }

    const size_t total_points = total_size / sizeof(Point);
    const auto* points = static_cast<const Point*>(
        mmap(nullptr, total_size, PROT_READ, MAP_PRIVATE, fd, 0));
    if (points == MAP_FAILED) {
        perror("mmap");
        close(fd);
        return 1;
    }

    const size_t pts_per_chunk = chunk_bytes / sizeof(Point);
    std::cout << "Input: " << total_points << " points ("
              << total_size / (1024*1024) << " MiB), chunk ~"
              << chunk_bytes / (1024*1024) << " MiB ("
              << pts_per_chunk << " pts/chunk)" << std::endl;

    size_t processed = 0;
    int chunk_idx = 0;

    while (processed < total_points) {
        const size_t end = std::min(processed + pts_per_chunk, total_points);
        const size_t count = end - processed;

        std::vector<float> cx(count), cy(count), cz(count), ci(count);
        for (size_t i = 0; i < count; ++i) {
            cx[i] = points[processed + i].x;
            cy[i] = points[processed + i].y;
            cz[i] = points[processed + i].z;
            ci[i] = points[processed + i].intensity;
        }

        std::ostringstream ss;
        ss << output_dir << "/chunk_"
           << std::setfill('0') << std::setw(5) << chunk_idx++
           << ".lidar.bin";
        std::ofstream out(ss.str(), std::ios::binary);
        if (!out.is_open()) {
            std::cerr << "Error: cannot open " << ss.str() << std::endl;
            return 1;
        }

        const char magic[4] = {'L', 'I', 'D', '1'};
        out.write(magic, 4);
        uint32_t n = static_cast<uint32_t>(count);
        out.write(reinterpret_cast<const char*>(&n), 4);
        out.write(reinterpret_cast<const char*>(cx.data()), count * sizeof(float));
        out.write(reinterpret_cast<const char*>(cy.data()), count * sizeof(float));
        out.write(reinterpret_cast<const char*>(cz.data()), count * sizeof(float));
        out.write(reinterpret_cast<const char*>(ci.data()), count * sizeof(float));
        out.close();

        processed += count;
    }

    munmap(const_cast<Point*>(points), total_size);
    close(fd);

    std::cout << "Wrote " << chunk_idx << " chunks to " << output_dir << std::endl;
    return 0;
}
