#include "vibesys_queue_abi.h"

#include <pthread.h>
#include <stdlib.h>
#include <string.h>

struct item {
    uint8_t *data;
    uint64_t length;
};

struct vsq_queue {
    pthread_mutex_t mutex;
    struct item *items;
    uint64_t capacity;
    uint64_t max_value_size;
    uint64_t head;
    uint64_t size;
    uint32_t producer_count;
    uint32_t consumer_count;
};

struct vsq_producer {
    struct vsq_queue *queue;
};

struct vsq_consumer {
    struct vsq_queue *queue;
};

#ifdef VSQ_TEST_RETAIN_INPUT
#define VSQ_FREE_VALUE(value) ((void)(value))
#else
#define VSQ_FREE_VALUE(value) free(value)
#endif

uint32_t vsq_abi_version(void) {
    return VSQ_ABI_VERSION;
}

vsq_status vsq_queue_create(
    uint64_t capacity,
    uint64_t max_value_size,
    uint32_t producer_count,
    uint32_t consumer_count,
    vsq_queue **queue_out) {
    if (capacity == 0 || max_value_size == 0 || producer_count == 0 ||
        consumer_count == 0 || queue_out == NULL) {
        return VSQ_INVALID;
    }
    struct vsq_queue *queue = calloc(1, sizeof(*queue));
    if (queue == NULL) {
        return VSQ_INTERNAL_ERROR;
    }
    queue->items = calloc((size_t)capacity, sizeof(*queue->items));
    if (queue->items == NULL || pthread_mutex_init(&queue->mutex, NULL) != 0) {
        free(queue->items);
        free(queue);
        return VSQ_INTERNAL_ERROR;
    }
    queue->capacity = capacity;
    queue->max_value_size = max_value_size;
    queue->producer_count = producer_count;
    queue->consumer_count = consumer_count;
    *queue_out = queue;
    return VSQ_OK;
}

void vsq_queue_destroy(vsq_queue *queue) {
    if (queue == NULL) {
        return;
    }
    for (uint64_t index = 0; index < queue->capacity; ++index) {
        VSQ_FREE_VALUE(queue->items[index].data);
    }
    pthread_mutex_destroy(&queue->mutex);
    free(queue->items);
    free(queue);
}

vsq_status vsq_producer_create(
    vsq_queue *queue,
    uint32_t producer_id,
    vsq_producer **producer_out) {
    if (queue == NULL || producer_out == NULL || producer_id >= queue->producer_count) {
        return VSQ_INVALID;
    }
    struct vsq_producer *producer = malloc(sizeof(*producer));
    if (producer == NULL) {
        return VSQ_INTERNAL_ERROR;
    }
    producer->queue = queue;
    *producer_out = producer;
    return VSQ_OK;
}

void vsq_producer_destroy(vsq_producer *producer) {
    free(producer);
}

vsq_status vsq_consumer_create(
    vsq_queue *queue,
    uint32_t consumer_id,
    vsq_consumer **consumer_out) {
    if (queue == NULL || consumer_out == NULL || consumer_id >= queue->consumer_count) {
        return VSQ_INVALID;
    }
    struct vsq_consumer *consumer = malloc(sizeof(*consumer));
    if (consumer == NULL) {
        return VSQ_INTERNAL_ERROR;
    }
    consumer->queue = queue;
    *consumer_out = consumer;
    return VSQ_OK;
}

void vsq_consumer_destroy(vsq_consumer *consumer) {
    free(consumer);
}

vsq_status vsq_try_enqueue(vsq_producer *producer, const uint8_t *data, uint64_t length) {
    if (producer == NULL || (data == NULL && length != 0) ||
        length > producer->queue->max_value_size) {
        return VSQ_INVALID;
    }
#ifdef VSQ_TEST_FIXED_LENGTH_ONLY
    if (length != producer->queue->max_value_size) {
        return VSQ_INVALID;
    }
#endif
    struct vsq_queue *queue = producer->queue;
    pthread_mutex_lock(&queue->mutex);
    if (queue->size == queue->capacity) {
        pthread_mutex_unlock(&queue->mutex);
        return VSQ_FULL;
    }
    uint8_t *copy = NULL;
    if (length != 0) {
#ifdef VSQ_TEST_RETAIN_INPUT
        copy = (uint8_t *)data;
#else
        copy = malloc((size_t)length);
        if (copy == NULL) {
            pthread_mutex_unlock(&queue->mutex);
            return VSQ_INTERNAL_ERROR;
        }
        memcpy(copy, data, (size_t)length);
#endif
    }
    uint64_t tail = (queue->head + queue->size) % queue->capacity;
    queue->items[tail].data = copy;
    queue->items[tail].length = length;
    queue->size++;
    pthread_mutex_unlock(&queue->mutex);
    return VSQ_OK;
}

vsq_status vsq_try_dequeue(
    vsq_consumer *consumer,
    uint8_t *output,
    uint64_t output_capacity,
    uint64_t *output_length) {
    if (consumer == NULL || output_length == NULL) {
        return VSQ_INVALID;
    }
    struct vsq_queue *queue = consumer->queue;
    pthread_mutex_lock(&queue->mutex);
    if (queue->size == 0) {
        pthread_mutex_unlock(&queue->mutex);
        return VSQ_EMPTY;
    }
    struct item *item = &queue->items[queue->head];
    if (item->length > output_capacity || (output == NULL && item->length != 0)) {
        pthread_mutex_unlock(&queue->mutex);
        return VSQ_INVALID;
    }
    if (item->length != 0) {
        memcpy(output, item->data, (size_t)item->length);
    }
    *output_length = item->length;
    VSQ_FREE_VALUE(item->data);
    item->data = NULL;
    item->length = 0;
    queue->head = (queue->head + 1) % queue->capacity;
    queue->size--;
    pthread_mutex_unlock(&queue->mutex);
    return VSQ_OK;
}
