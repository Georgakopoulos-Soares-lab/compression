#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <string>

namespace fs = std::filesystem;

static void usage(const char* argv0) {
  std::cerr
  << "Usage: " << argv0 << " <input.tsv> <out_dir> <chunk_bytes> [--no-repeat-header] [--ext .tsv] [--prefix chunk_] [--digits 5] [--preamble-file <path>]\n"
      << "\n"
      << "Splits a delimited text file into size-bounded chunks without breaking lines.\n"
      << "By default, repeats the first line (header) in every chunk.\n";
}

static bool starts_with(const std::string& s, const std::string& p) {
  return s.size() >= p.size() && s.compare(0, p.size(), p) == 0;
}

int main(int argc, char** argv) {
  if (argc < 4) {
    usage(argv[0]);
    return 2;
  }

  fs::path input_path = argv[1];
  fs::path out_dir = argv[2];
  std::uint64_t chunk_bytes = 0;
  try {
    chunk_bytes = std::stoull(argv[3]);
  } catch (...) {
    std::cerr << "Error: invalid chunk_bytes: " << argv[3] << "\n";
    return 2;
  }
  if (chunk_bytes == 0) {
    std::cerr << "Error: chunk_bytes must be > 0\n";
    return 2;
  }

  bool repeat_header = true;
  std::string ext = ".tsv";
  std::string prefix = "chunk_";
  int digits = 5;
  fs::path preamble_file;
  std::string preamble;

  for (int i = 4; i < argc; i++) {
    std::string a = argv[i];
    if (a == "--no-repeat-header") {
      repeat_header = false;
    } else if (a == "--repeat-header") {
      repeat_header = true;
    } else if (a == "--ext") {
      if (i + 1 >= argc) {
        std::cerr << "Error: --ext requires a value\n";
        return 2;
      }
      ext = argv[++i];
    } else if (a == "--prefix") {
      if (i + 1 >= argc) {
        std::cerr << "Error: --prefix requires a value\n";
        return 2;
      }
      prefix = argv[++i];
    } else if (a == "--digits") {
      if (i + 1 >= argc) {
        std::cerr << "Error: --digits requires a value\n";
        return 2;
      }
      try {
        digits = std::stoi(argv[++i]);
      } catch (...) {
        std::cerr << "Error: invalid --digits value\n";
        return 2;
      }
      if (digits < 1 || digits > 12) {
        std::cerr << "Error: --digits out of range (1..12)\n";
        return 2;
      }
    } else if (a == "--preamble-file") {
      if (i + 1 >= argc) {
        std::cerr << "Error: --preamble-file requires a value\n";
        return 2;
      }
      preamble_file = argv[++i];
    } else {
      std::cerr << "Error: unknown arg: " << a << "\n";
      usage(argv[0]);
      return 2;
    }
  }

  if (!fs::exists(input_path)) {
    std::cerr << "Error: input not found: " << input_path << "\n";
    return 2;
  }

  if (!preamble_file.empty()) {
    std::ifstream pf(preamble_file, std::ios::in | std::ios::binary);
    if (!pf) {
      std::cerr << "Error: failed to open preamble file: " << preamble_file << "\n";
      return 2;
    }
    preamble.assign((std::istreambuf_iterator<char>(pf)), std::istreambuf_iterator<char>());
  }

  fs::create_directories(out_dir);

  std::ifstream in(input_path, std::ios::in | std::ios::binary);
  if (!in) {
    std::cerr << "Error: failed to open input: " << input_path << "\n";
    return 1;
  }

  std::string header;
  if (!std::getline(in, header)) {
    std::cerr << "Error: empty input file\n";
    return 1;
  }
  header.push_back('\n');

  auto chunk_path = [&](int idx) {
    std::ostringstream oss;
    oss << prefix << std::setfill('0') << std::setw(digits) << idx << ext;
    return out_dir / oss.str();
  };

  std::ofstream out;
  std::uint64_t curr_bytes = 0;
  std::uint64_t total_bytes = 0;
  std::uint64_t data_lines = 0;
  int chunk_idx = -1;

  auto open_next = [&]() {
    if (out.is_open()) {
      out.close();
    }
    chunk_idx++;
    fs::path p = chunk_path(chunk_idx);
    out.open(p, std::ios::out | std::ios::binary | std::ios::trunc);
    if (!out) {
      std::cerr << "Error: failed to open output: " << p << "\n";
      std::exit(1);
    }
    curr_bytes = 0;
    if (!preamble.empty()) {
      out.write(preamble.data(), static_cast<std::streamsize>(preamble.size()));
      curr_bytes += preamble.size();
      total_bytes += preamble.size();
    }
    if (repeat_header) {
      out.write(header.data(), static_cast<std::streamsize>(header.size()));
      curr_bytes += header.size();
      total_bytes += header.size();
    }
  };

  open_next();

  std::string line;
  while (std::getline(in, line)) {
    line.push_back('\n');
    if (curr_bytes > 0 && curr_bytes + line.size() > chunk_bytes) {
      open_next();
    }
    out.write(line.data(), static_cast<std::streamsize>(line.size()));
    curr_bytes += line.size();
    total_bytes += line.size();
    data_lines++;
  }

  if (out.is_open()) {
    out.close();
  }

  std::cout << "Wrote " << (chunk_idx + 1) << " chunk(s) to: " << out_dir << "\n";
  std::cout << "Data lines written: " << data_lines << "\n";
  std::cout << "Total bytes written (incl headers): " << total_bytes << "\n";
  return 0;
}
