.PHONY: all clean

CXX ?= c++
ABI_HEADER := $(firstword $(wildcard ../../evaluators/stack/include/vibesys_stack_abi.h ../_evaluator/stack/include/vibesys_stack_abi.h))

ifeq ($(ABI_HEADER),)
$(error could not locate vibesys_stack_abi.h)
endif

# Boost.Lockfree is header-only. Debian/Ubuntu ships it as libboost-dev.
CXXFLAGS ?= -O3 -DNDEBUG -march=native
CXXFLAGS += -std=c++17 -fPIC -Wall -Wextra -Wpedantic -Werror -I$(dir $(ABI_HEADER)) -pthread

ifeq ($(shell uname -s),Darwin)
SHARED_FLAGS := -dynamiclib
else
SHARED_FLAGS := -shared
endif

all: stack-candidate.so

stack-candidate.so: boost_lockfree_stack.cpp $(ABI_HEADER)
	$(CXX) $(CXXFLAGS) $(SHARED_FLAGS) boost_lockfree_stack.cpp -o $@

clean:
	rm -f stack-candidate.so
