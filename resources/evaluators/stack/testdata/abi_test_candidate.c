#include "vibesys_stack_abi.h"

#include <pthread.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

struct item {
    uint8_t *data;
    uint64_t length;
};

struct vsst_stack {
    pthread_mutex_t mutex;
    struct item *items;
    uint64_t capacity;
    uint64_t max_value_size;
    uint64_t size;
    uint32_t producer_count;
    uint32_t consumer_count;
};

struct vsst_producer {
    struct vsst_stack *stack;
};

struct vsst_consumer {
    struct vsst_stack *stack;
};

#ifdef VSST_TEST_RETAIN_INPUT
#define VSST_FREE_VALUE(value) ((void)(value))
#else
#define VSST_FREE_VALUE(value) free(value)
#endif

uint32_t vsst_abi_version(void) {
    return VSST_ABI_VERSION;
}

vsst_status vsst_stack_create(
    uint64_t capacity,
    uint64_t max_value_size,
    uint32_t producer_count,
    uint32_t consumer_count,
    vsst_stack **stack_out) {
    if (capacity == 0 || max_value_size == 0 || producer_count == 0 ||
        consumer_count == 0 || stack_out == NULL) {
        return VSST_INVALID;
    }
    struct vsst_stack *stack = calloc(1, sizeof(*stack));
    if (stack == NULL) {
        return VSST_INTERNAL_ERROR;
    }
    stack->items = calloc((size_t)capacity, sizeof(*stack->items));
    if (stack->items == NULL || pthread_mutex_init(&stack->mutex, NULL) != 0) {
        free(stack->items);
        free(stack);
        return VSST_INTERNAL_ERROR;
    }
    stack->capacity = capacity;
    stack->max_value_size = max_value_size;
    stack->producer_count = producer_count;
    stack->consumer_count = consumer_count;
    *stack_out = stack;
    return VSST_OK;
}

void vsst_stack_destroy(vsst_stack *stack) {
#ifdef VSST_TEST_HANG_ON_DESTROY
    volatile unsigned int keep_running = 1;
    while (keep_running) {
    }
#endif
    if (stack == NULL) {
        return;
    }
    for (uint64_t index = 0; index < stack->capacity; ++index) {
        VSST_FREE_VALUE(stack->items[index].data);
    }
    pthread_mutex_destroy(&stack->mutex);
    free(stack->items);
    free(stack);
}

vsst_status vsst_producer_create(
    vsst_stack *stack,
    uint32_t producer_id,
    vsst_producer **producer_out) {
    if (stack == NULL || producer_out == NULL || producer_id >= stack->producer_count) {
        return VSST_INVALID;
    }
    struct vsst_producer *producer = malloc(sizeof(*producer));
    if (producer == NULL) {
        return VSST_INTERNAL_ERROR;
    }
    producer->stack = stack;
    *producer_out = producer;
    return VSST_OK;
}

void vsst_producer_destroy(vsst_producer *producer) {
    free(producer);
}

vsst_status vsst_consumer_create(
    vsst_stack *stack,
    uint32_t consumer_id,
    vsst_consumer **consumer_out) {
    if (stack == NULL || consumer_out == NULL || consumer_id >= stack->consumer_count) {
        return VSST_INVALID;
    }
    struct vsst_consumer *consumer = malloc(sizeof(*consumer));
    if (consumer == NULL) {
        return VSST_INTERNAL_ERROR;
    }
    consumer->stack = stack;
    *consumer_out = consumer;
    return VSST_OK;
}

void vsst_consumer_destroy(vsst_consumer *consumer) {
    free(consumer);
}

vsst_status vsst_try_push(
    vsst_producer *producer,
    const uint8_t *data,
    uint64_t length) {
    if (producer == NULL || (data == NULL && length != 0) ||
        length > producer->stack->max_value_size) {
        return VSST_INVALID;
    }
#ifdef VSST_TEST_HANG_CAPACITY_ONE
    if (producer->stack->capacity == 1) {
        volatile unsigned int keep_running = 1;
        while (keep_running) {
        }
    }
#endif
#ifdef VSST_TEST_FIXED_LENGTH_ONLY
    if (length != producer->stack->max_value_size) {
        return VSST_INVALID;
    }
#endif
    struct vsst_stack *stack = producer->stack;
    pthread_mutex_lock(&stack->mutex);
    if (stack->size == stack->capacity) {
        pthread_mutex_unlock(&stack->mutex);
        return VSST_FULL;
    }
    uint8_t *copy = NULL;
    if (length != 0) {
#ifdef VSST_TEST_RETAIN_INPUT
        copy = (uint8_t *)data;
#else
        copy = malloc((size_t)length);
        if (copy == NULL) {
            pthread_mutex_unlock(&stack->mutex);
            return VSST_INTERNAL_ERROR;
        }
        memcpy(copy, data, (size_t)length);
#endif
    }
    stack->items[stack->size].data = copy;
    stack->items[stack->size].length = length;
    stack->size++;
    pthread_mutex_unlock(&stack->mutex);
    return VSST_OK;
}

vsst_status vsst_try_pop(
    vsst_consumer *consumer,
    uint8_t *output,
    uint64_t output_capacity,
    uint64_t *output_length) {
    if (consumer == NULL || output_length == NULL) {
        return VSST_INVALID;
    }
    struct vsst_stack *stack = consumer->stack;
    pthread_mutex_lock(&stack->mutex);
    if (stack->size == 0) {
        pthread_mutex_unlock(&stack->mutex);
        return VSST_EMPTY;
    }
    struct item *item = &stack->items[stack->size - 1];
    if (item->length > output_capacity || (output == NULL && item->length != 0)) {
        pthread_mutex_unlock(&stack->mutex);
        return VSST_INVALID;
    }
    if (item->length != 0) {
        memcpy(output, item->data, (size_t)item->length);
    }
    *output_length = item->length;
    VSST_FREE_VALUE(item->data);
    item->data = NULL;
    item->length = 0;
    stack->size--;
    pthread_mutex_unlock(&stack->mutex);
    return VSST_OK;
}
