.PHONY: all clean

CXX ?= c++
ABI_HEADER := $(firstword $(wildcard ../../evaluators/unordered-map/include/vibesys_unordered_map_abi.h ../_evaluator/unordered-map/include/vibesys_unordered_map_abi.h))

ifeq ($(ABI_HEADER),)
$(error could not locate vibesys_unordered_map_abi.h)
endif

# oneTBB is a system dependency; it is not vendored. Debian/Ubuntu ships it as
# libtbb-dev.
TBB_CFLAGS := $(shell pkg-config --cflags tbb 2>/dev/null)
TBB_LIBS := $(shell pkg-config --libs tbb 2>/dev/null)
ifeq ($(TBB_LIBS),)
TBB_LIBS := -ltbb
endif

CXXFLAGS ?= -O3 -DNDEBUG -march=native
# TBB headers are included as system headers so their warnings do not trip -Werror.
CXXFLAGS += -std=c++17 -fPIC -Wall -Wextra -Wpedantic -Werror -I$(dir $(ABI_HEADER)) $(patsubst -I%,-isystem %,$(TBB_CFLAGS)) -pthread

ifeq ($(shell uname -s),Darwin)
SHARED_FLAGS := -dynamiclib
else
SHARED_FLAGS := -shared
endif

all: unordered-map-candidate.so

unordered-map-candidate.so: tbb_unordered_map.cpp $(ABI_HEADER)
	$(CXX) $(CXXFLAGS) $(SHARED_FLAGS) tbb_unordered_map.cpp -o $@ $(TBB_LIBS)

clean:
	rm -f unordered-map-candidate.so
