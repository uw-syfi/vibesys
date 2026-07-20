#ifndef VIBESYS_QUEUE_ABI_H
#define VIBESYS_QUEUE_ABI_H

#include <stdint.h>

#define VSQ_ABI_VERSION 1u

typedef struct vsq_queue vsq_queue;
typedef struct vsq_producer vsq_producer;
typedef struct vsq_consumer vsq_consumer;

typedef uint32_t vsq_status;

#define VSQ_OK 0u
#define VSQ_FULL 1u
#define VSQ_EMPTY 2u
#define VSQ_INVALID 3u
#define VSQ_INTERNAL_ERROR 4u

#ifdef __cplusplus
extern "C" {
#endif

uint32_t vsq_abi_version(void);

vsq_status vsq_queue_create(
    uint64_t capacity,
    uint64_t max_value_size,
    uint32_t producer_count,
    uint32_t consumer_count,
    vsq_queue **queue_out);

void vsq_queue_destroy(vsq_queue *queue);

vsq_status vsq_producer_create(
    vsq_queue *queue,
    uint32_t producer_id,
    vsq_producer **producer_out);

void vsq_producer_destroy(vsq_producer *producer);

vsq_status vsq_consumer_create(
    vsq_queue *queue,
    uint32_t consumer_id,
    vsq_consumer **consumer_out);

void vsq_consumer_destroy(vsq_consumer *consumer);

vsq_status vsq_try_enqueue(
    vsq_producer *producer,
    const uint8_t *data,
    uint64_t length);

vsq_status vsq_try_dequeue(
    vsq_consumer *consumer,
    uint8_t *output,
    uint64_t output_capacity,
    uint64_t *output_length);

#ifdef __cplusplus
}
#endif

#endif
