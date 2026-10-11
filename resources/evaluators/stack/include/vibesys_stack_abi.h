#ifndef VIBESYS_STACK_ABI_H
#define VIBESYS_STACK_ABI_H

#include <stdint.h>

#define VSST_ABI_VERSION 1u

typedef struct vsst_stack vsst_stack;
typedef struct vsst_producer vsst_producer;
typedef struct vsst_consumer vsst_consumer;

typedef uint32_t vsst_status;

#define VSST_OK 0u
#define VSST_FULL 1u
#define VSST_EMPTY 2u
#define VSST_INVALID 3u
#define VSST_INTERNAL_ERROR 4u

#ifdef __cplusplus
extern "C" {
#endif

uint32_t vsst_abi_version(void);

vsst_status vsst_stack_create(
    uint64_t capacity,
    uint64_t max_value_size,
    uint32_t producer_count,
    uint32_t consumer_count,
    vsst_stack **stack_out);

void vsst_stack_destroy(vsst_stack *stack);

vsst_status vsst_producer_create(
    vsst_stack *stack,
    uint32_t producer_id,
    vsst_producer **producer_out);

void vsst_producer_destroy(vsst_producer *producer);

vsst_status vsst_consumer_create(
    vsst_stack *stack,
    uint32_t consumer_id,
    vsst_consumer **consumer_out);

void vsst_consumer_destroy(vsst_consumer *consumer);

vsst_status vsst_try_push(
    vsst_producer *producer,
    const uint8_t *data,
    uint64_t length);

vsst_status vsst_try_pop(
    vsst_consumer *consumer,
    uint8_t *output,
    uint64_t output_capacity,
    uint64_t *output_length);

#ifdef __cplusplus
}
#endif

#endif
