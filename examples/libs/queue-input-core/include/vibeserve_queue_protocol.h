#ifndef VIBESERVE_QUEUE_PROTOCOL_H
#define VIBESERVE_QUEUE_PROTOCOL_H

#include <stdint.h>

#define VSQ_PROTOCOL_VERSION 1u
#define VSQ_FD_BASE 3
#define VSQ_FRAME_SIZE 16u
#define VSQ_BENCHMARK_PIPELINE_DEPTH 64u

enum vsq_scenario {
    VSQ_SCENARIO_SPSC = 1,
    VSQ_SCENARIO_MPSC = 2,
    VSQ_SCENARIO_MPMC = 3,
};

enum vsq_operation {
    VSQ_OPERATION_ENQUEUE = 1,
    VSQ_OPERATION_DEQUEUE = 2,
};

enum vsq_status {
    VSQ_STATUS_ENQUEUED = 1,
    VSQ_STATUS_FULL = 2,
    VSQ_STATUS_VALUE = 3,
    VSQ_STATUS_EMPTY = 4,
    VSQ_STATUS_ERROR = 5,
};

struct vsq_request {
    uint32_t operation;
    uint32_t reserved;
    uint64_t value;
};

struct vsq_response {
    uint32_t status;
    uint32_t reserved;
    uint64_t value;
};

_Static_assert(sizeof(struct vsq_request) == VSQ_FRAME_SIZE, "request layout mismatch");
_Static_assert(sizeof(struct vsq_response) == VSQ_FRAME_SIZE, "response layout mismatch");

#endif
