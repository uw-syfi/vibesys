#ifndef VIBESERVE_QUEUE_PROTOCOL_H
#define VIBESERVE_QUEUE_PROTOCOL_H

#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>

#define VSQ_PROTOCOL_VERSION 1u
#define VSQ_HEADER_SIZE 4096u
#define VSQ_LANE_SIZE 4096u
#define VSQ_RING_SLOTS 64u

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

struct vsq_header {
    uint8_t magic[8];
    uint32_t version;
    uint32_t lane_count;
    uint64_t capacity;
    uint32_t scenario;
    uint32_t ring_slots;
    _Atomic uint64_t ready;
    _Atomic uint64_t stop;
    uint8_t reserved[VSQ_HEADER_SIZE - 48];
};

struct vsq_lane {
    _Atomic uint64_t request_published;
    uint8_t request_published_padding[56];
    _Atomic uint64_t request_consumed;
    uint8_t request_consumed_padding[56];
    _Atomic uint64_t response_published;
    uint8_t response_published_padding[56];
    _Atomic uint64_t response_consumed;
    uint8_t response_consumed_padding[56];
    struct vsq_request requests[VSQ_RING_SLOTS];
    struct vsq_response responses[VSQ_RING_SLOTS];
    uint8_t reserved[VSQ_LANE_SIZE - 256 - (VSQ_RING_SLOTS * 32)];
};

static inline struct vsq_lane *vsq_lane_at(void *mapping, uint32_t lane_index) {
    return (struct vsq_lane *)((uint8_t *)mapping + VSQ_HEADER_SIZE +
                               ((size_t)lane_index * VSQ_LANE_SIZE));
}

_Static_assert(sizeof(struct vsq_request) == 16, "request layout mismatch");
_Static_assert(sizeof(struct vsq_response) == 16, "response layout mismatch");
_Static_assert(sizeof(struct vsq_header) == VSQ_HEADER_SIZE, "header layout mismatch");
_Static_assert(sizeof(struct vsq_lane) == VSQ_LANE_SIZE, "lane layout mismatch");
_Static_assert(offsetof(struct vsq_header, ready) == 32, "ready offset mismatch");
_Static_assert(offsetof(struct vsq_lane, requests) == 256, "request ring offset mismatch");
_Static_assert(offsetof(struct vsq_lane, responses) == 1280, "response ring offset mismatch");

#endif
