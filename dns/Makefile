# DNS TSV compression tools — Makefile
# Builds dns_compress, dns_decompress, and benchmark by linking against OpenZL

CXX := c++
CXXFLAGS := -std=c++17 -O3 -Wall

# OpenZL paths
OPENZL_ROOT := ../nyx/openzl
OPENZL_INCLUDE := $(OPENZL_ROOT)/include $(OPENZL_ROOT)/cpp/include $(OPENZL_ROOT)
OPENZL_LIBZSTD := $(OPENZL_ROOT)/deps/zstd/lib/libzstd.a

# Cached object files from zli build (all .o except zli.o main)
CACHE_DIR := $(shell ls -d $(OPENZL_ROOT)/cachedObjs/*/cli/zli.o 2>/dev/null | head -1 | xargs dirname | xargs dirname)
OPENZL_OBJS := $(shell find $(CACHE_DIR) -name "*.o" ! -name "zli.o" ! -name "*gtest*" ! -name "*test_*" ! -name "*_test.o" 2>/dev/null)

# Snappy and zstd from Homebrew
SNAPPY_PREFIX := $(shell brew --prefix snappy 2>/dev/null || echo /opt/homebrew/opt/snappy)
ZSTD_PREFIX := $(shell brew --prefix zstd 2>/dev/null || echo /opt/homebrew/opt/zstd)

INCLUDE_FLAGS := $(addprefix -I,$(OPENZL_INCLUDE)) -I$(SNAPPY_PREFIX)/include -I$(ZSTD_PREFIX)/include
LDFLAGS := -L$(SNAPPY_PREFIX)/lib -L$(ZSTD_PREFIX)/lib

# System frameworks (macOS CommonCrypto for MD5)
FRAMEWORKS := -framework Security

.PHONY: all clean

all: dns_compress dns_decompress benchmark

dns_compress: dns_compress.cpp
	$(CXX) $(CXXFLAGS) $(INCLUDE_FLAGS) -o $@ $< $(OPENZL_OBJS) $(OPENZL_LIBZSTD) $(LDFLAGS) -lc++ $(FRAMEWORKS)

dns_decompress: dns_decompress.cpp
	$(CXX) $(CXXFLAGS) $(INCLUDE_FLAGS) -o $@ $< $(OPENZL_OBJS) $(OPENZL_LIBZSTD) $(LDFLAGS) -lc++ $(FRAMEWORKS)

benchmark: benchmark.cpp
	$(CXX) $(CXXFLAGS) $(INCLUDE_FLAGS) -o $@ $< $(OPENZL_OBJS) $(OPENZL_LIBZSTD) $(LDFLAGS) -lsnappy -lzstd -lc++ $(FRAMEWORKS)

clean:
	rm -f dns_compress dns_decompress benchmark
