#include "vibesys_priority_queue_abi.h"

#include <pthread.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

struct item {
    uint8_t *data;
    uint64_t length;
    uint64_t priority;
    uint64_t sequence;
};

struct vspq_queue {
    pthread_mutex_t mutex;
    struct item *items;
    uint64_t capacity;
    uint64_t max_value_size;
    uint64_t head;
    uint64_t size;
    uint64_t next_sequence;
    uint32_t producer_count;
    uint32_t consumer_count;
};

struct vspq_producer {
    struct vspq_queue *queue;
};

struct vspq_consumer {
    struct vspq_queue *queue;
};

#ifdef VSPQ_TEST_RETAIN_INPUT
#define VSPQ_FREE_VALUE(value) ((void)(value))
#else
#define VSPQ_FREE_VALUE(value) free(value)
#endif

uint32_t vspq_abi_version(void) {
    return VSPQ_ABI_VERSION;
}

vspq_status vspq_queue_create(
    uint64_t capacity,
    uint64_t max_value_size,
    uint32_t producer_count,
    uint32_t consumer_count,
    vspq_queue **queue_out) {
    if (capacity == 0 || max_value_size == 0 || producer_count == 0 ||
        consumer_count == 0 || queue_out == NULL) {
        return VSPQ_INVALID;
    }
    struct vspq_queue *queue = calloc(1, sizeof(*queue));
    if (queue == NULL) {
        return VSPQ_INTERNAL_ERROR;
    }
    queue->items = calloc((size_t)capacity, sizeof(*queue->items));
    if (queue->items == NULL || pthread_mutex_init(&queue->mutex, NULL) != 0) {
        free(queue->items);
        free(queue);
        return VSPQ_INTERNAL_ERROR;
    }
    queue->capacity = capacity;
    queue->max_value_size = max_value_size;
    queue->producer_count = producer_count;
    queue->consumer_count = consumer_count;
    *queue_out = queue;
    return VSPQ_OK;
}

void vspq_queue_destroy(vspq_queue *queue) {
    if (queue == NULL) {
        return;
    }
    for (uint64_t index = 0; index < queue->capacity; ++index) {
        VSPQ_FREE_VALUE(queue->items[index].data);
    }
    pthread_mutex_destroy(&queue->mutex);
    free(queue->items);
    free(queue);
}

vspq_status vspq_producer_create(
    vspq_queue *queue,
    uint32_t producer_id,
    vspq_producer **producer_out) {
    if (queue == NULL || producer_out == NULL || producer_id >= queue->producer_count) {
        return VSPQ_INVALID;
    }
    struct vspq_producer *producer = malloc(sizeof(*producer));
    if (producer == NULL) {
        return VSPQ_INTERNAL_ERROR;
    }
    producer->queue = queue;
    *producer_out = producer;
    return VSPQ_OK;
}

void vspq_producer_destroy(vspq_producer *producer) {
    free(producer);
}

vspq_status vspq_consumer_create(
    vspq_queue *queue,
    uint32_t consumer_id,
    vspq_consumer **consumer_out) {
    if (queue == NULL || consumer_out == NULL || consumer_id >= queue->consumer_count) {
        return VSPQ_INVALID;
    }
    struct vspq_consumer *consumer = malloc(sizeof(*consumer));
    if (consumer == NULL) {
        return VSPQ_INTERNAL_ERROR;
    }
    consumer->queue = queue;
    *consumer_out = consumer;
    return VSPQ_OK;
}

void vspq_consumer_destroy(vspq_consumer *consumer) {
    free(consumer);
}

vspq_status vspq_try_enqueue(
    vspq_producer *producer,
    uint64_t priority,
    const uint8_t *data,
    uint64_t length) {
    if (producer == NULL || (data == NULL && length != 0) ||
        length > producer->queue->max_value_size) {
        return VSPQ_INVALID;
    }
#ifdef VSPQ_TEST_HANG_CAPACITY_ONE
    if (producer->queue->capacity == 1) {
        volatile unsigned int keep_running = 1;
        while (keep_running) {
        }
    }
#endif
#ifdef VSPQ_TEST_FIXED_LENGTH_ONLY
    if (length != producer->queue->max_value_size) {
        return VSPQ_INVALID;
    }
#endif
    struct vspq_queue *queue = producer->queue;
    pthread_mutex_lock(&queue->mutex);
    if (queue->size == queue->capacity) {
        pthread_mutex_unlock(&queue->mutex);
        return VSPQ_FULL;
    }
    uint8_t *copy = NULL;
    if (length != 0) {
#ifdef VSPQ_TEST_RETAIN_INPUT
        copy = (uint8_t *)data;
#else
        copy = malloc((size_t)length);
        if (copy == NULL) {
            pthread_mutex_unlock(&queue->mutex);
            return VSPQ_INTERNAL_ERROR;
        }
        memcpy(copy, data, (size_t)length);
#endif
    }
    uint64_t tail = (queue->head + queue->size) % queue->capacity;
    queue->items[tail].data = copy;
    queue->items[tail].length = length;
    queue->items[tail].priority = priority;
    queue->items[tail].sequence = queue->next_sequence++;
    queue->size++;
    pthread_mutex_unlock(&queue->mutex);
    return VSPQ_OK;
}

vspq_status vspq_try_dequeue(
    vspq_consumer *consumer,
    uint8_t *output,
    uint64_t output_capacity,
    uint64_t *output_length) {
    if (consumer == NULL || output_length == NULL) {
        return VSPQ_INVALID;
    }
    struct vspq_queue *queue = consumer->queue;
    pthread_mutex_lock(&queue->mutex);
    if (queue->size == 0) {
        pthread_mutex_unlock(&queue->mutex);
        return VSPQ_EMPTY;
    }
    uint64_t best_offset = 0;
    for (uint64_t offset = 1; offset < queue->size; ++offset) {
        struct item *candidate =
            &queue->items[(queue->head + offset) % queue->capacity];
        struct item *best =
            &queue->items[(queue->head + best_offset) % queue->capacity];
        if (candidate->priority < best->priority ||
            (candidate->priority == best->priority &&
             candidate->sequence < best->sequence)) {
            best_offset = offset;
        }
    }
    struct item *item =
        &queue->items[(queue->head + best_offset) % queue->capacity];
    if (item->length > output_capacity || (output == NULL && item->length != 0)) {
        pthread_mutex_unlock(&queue->mutex);
        return VSPQ_INVALID;
    }
    if (item->length != 0) {
        memcpy(output, item->data, (size_t)item->length);
    }
    *output_length = item->length;
    VSPQ_FREE_VALUE(item->data);
    item->data = NULL;
    item->length = 0;
    for (uint64_t offset = best_offset; offset + 1 < queue->size; ++offset) {
        uint64_t current = (queue->head + offset) % queue->capacity;
        uint64_t next = (queue->head + offset + 1) % queue->capacity;
        queue->items[current] = queue->items[next];
    }
    uint64_t tail = (queue->head + queue->size - 1) % queue->capacity;
    memset(&queue->items[tail], 0, sizeof(queue->items[tail]));
    queue->size--;
    pthread_mutex_unlock(&queue->mutex);
    return VSPQ_OK;
}
