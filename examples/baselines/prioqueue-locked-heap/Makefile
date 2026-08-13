.PHONY: all clean

CC ?= cc
ABI_HEADER := $(firstword $(wildcard ../../evaluators/priority-queue/include/vibesys_priority_queue_abi.h ../_evaluator/priority-queue/include/vibesys_priority_queue_abi.h))

ifeq ($(ABI_HEADER),)
$(error could not locate vibesys_priority_queue_abi.h)
endif

CFLAGS ?= -O3 -DNDEBUG -march=native
CFLAGS += -std=c11 -fPIC -Wall -Wextra -Wpedantic -Werror -I$(dir $(ABI_HEADER)) -pthread

ifeq ($(shell uname -s),Darwin)
SHARED_FLAGS := -dynamiclib
else
SHARED_FLAGS := -shared
endif

all: priority-queue-candidate.so

priority-queue-candidate.so: locked_heap.c $(ABI_HEADER)
	$(CC) $(CFLAGS) $(SHARED_FLAGS) locked_heap.c -o $@

clean:
	rm -f priority-queue-candidate.so
