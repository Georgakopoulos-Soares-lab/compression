# Builds the `nyxfqz` tool by reusing OpenZL's own make variables/macros.
#
# Usage (run from the openzl/ directory, AFTER `make zli` has built deps+objs):
#     make -f nyxfqz.make nyxfqz
#
# It links the same object set as `zli` (minus cli/zli.o) plus our
# nyx/nyxfqz.o, so training/io/cpp/core are all available.

include Makefile

nyx/nyxfqz.o: CPPFLAGS += $(XGBOOST_INCLUDE_PATHS) -DZDICT_STATIC_LINKING_ONLY
nyxfqz: LDFLAGS += $(XGBOOST_LDFLAGS)
nyxfqz: CPPFLAGS += $(XGBOOST_INCLUDE_PATHS) -DZDICT_STATIC_LINKING_ONLY
nyxfqz: LDLIBS += $(XGBOOST_LDLIBS)

$(eval $(call cxx_program,nyxfqz, \
	nyx/nyxfqz.o \
	$(CLI_CXXOBJS) \
	$(ARG_CXXOBJS) \
	$(LOGGER_CXXOBJS) \
	$(CUSTOM_PARSERS_COBJS) \
	$(CUSTOM_PARSERS_CXXOBJS) \
	$(SHARED_COMPONENTS_CXXOBJS) \
	$(CSV_COBJS) \
	$(CSV_CXXOBJS) \
	$(PROFILES_SDDL_COBJS) \
	$(PARQUET_COBJS) \
	$(PARQUET_CXXOBJS) \
	$(VISUALIZER_CXXOBJS) \
	$(IO_CXXOBJS) \
	$(TRAINING_CXXOBJS) \
	$(SDDL_COMPILER_CXXOBJS) \
	$(SDDL2_COMPILER_CXXOBJS) \
	$(SDDL2_ASSEMBLER_CXXOBJS) \
	$(ML_SELECTOR_COBJS) \
	$(ML_SELECTOR_CXXOBJS) \
	$(ZLCPP_OBJS) \
	$(LIBOBJS), \
	$(LIBZSTD_A) $(LIBLZ4_A) $(LIBXGBOOST_A) $(LIBDMLC_A)))
