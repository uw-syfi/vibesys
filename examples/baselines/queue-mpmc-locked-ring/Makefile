.PHONY: all clean

CC ?= cc
ABI_HEADER := $(firstword $(wildcard ../../evaluators/queue/include/vibesys_queue_abi.h ../_evaluator/queue/include/vibesys_queue_abi.h))

ifeq ($(ABI_HEADER),)
$(error could not locate vibesys_queue_abi.h)
endif

CFLAGS ?= -O3 -DNDEBUG -march=native
CFLAGS += -std=c11 -fPIC -Wall -Wextra -Wpedantic -Werror -I$(dir $(ABI_HEADER)) -pthread

ifeq ($(shell uname -s),Darwin)
SHARED_FLAGS := -dynamiclib
else
SHARED_FLAGS := -shared
endif

all: queue-candidate.so

queue-candidate.so: locked_ring.c $(ABI_HEADER)
	$(CC) $(CFLAGS) $(SHARED_FLAGS) locked_ring.c -o $@

clean:
	rm -f queue-candidate.so
