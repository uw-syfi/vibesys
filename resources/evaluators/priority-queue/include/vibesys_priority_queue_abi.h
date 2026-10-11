#ifndef VIBESYS_PRIORITY_QUEUE_ABI_H
#define VIBESYS_PRIORITY_QUEUE_ABI_H

#include <stdint.h>

#define VSPQ_ABI_VERSION 1u

typedef struct vspq_queue vspq_queue;
typedef struct vspq_producer vspq_producer;
typedef struct vspq_consumer vspq_consumer;

typedef uint32_t vspq_status;

#define VSPQ_OK 0u
#define VSPQ_FULL 1u
#define VSPQ_EMPTY 2u
#define VSPQ_INVALID 3u
#define VSPQ_INTERNAL_ERROR 4u

#ifdef __cplusplus
extern "C" {
#endif

uint32_t vspq_abi_version(void);

vspq_status vspq_queue_create(
    uint64_t capacity,
    uint64_t max_value_size,
    uint32_t producer_count,
    uint32_t consumer_count,
    vspq_queue **queue_out);

void vspq_queue_destroy(vspq_queue *queue);

vspq_status vspq_producer_create(
    vspq_queue *queue,
    uint32_t producer_id,
    vspq_producer **producer_out);

void vspq_producer_destroy(vspq_producer *producer);

vspq_status vspq_consumer_create(
    vspq_queue *queue,
    uint32_t consumer_id,
    vspq_consumer **consumer_out);

void vspq_consumer_destroy(vspq_consumer *consumer);

vspq_status vspq_try_enqueue(
    vspq_producer *producer,
    uint64_t priority,
    const uint8_t *data,
    uint64_t length);

vspq_status vspq_try_dequeue(
    vspq_consumer *consumer,
    uint8_t *output,
    uint64_t output_capacity,
    uint64_t *output_length);

#ifdef __cplusplus
}
#endif

#endif
