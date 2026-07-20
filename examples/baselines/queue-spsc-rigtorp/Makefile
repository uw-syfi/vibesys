.PHONY: all clean

CXX ?= c++
ABI_HEADER := $(firstword $(wildcard ../../evaluators/queue/include/vibesys_queue_abi.h ../_evaluator/queue/include/vibesys_queue_abi.h))

ifeq ($(ABI_HEADER),)
$(error could not locate vibesys_queue_abi.h)
endif

CXXFLAGS ?= -O3 -DNDEBUG -march=native
CXXFLAGS += -std=c++17 -fPIC -Wall -Wextra -Wpedantic -Werror -I$(dir $(ABI_HEADER))

ifeq ($(shell uname -s),Darwin)
SHARED_FLAGS := -dynamiclib
else
SHARED_FLAGS := -shared
endif

all: queue-candidate.so

queue-candidate.so: spsc_queue.cpp $(ABI_HEADER)
	$(CXX) $(CXXFLAGS) $(SHARED_FLAGS) spsc_queue.cpp -o $@

clean:
	rm -f queue-candidate.so
