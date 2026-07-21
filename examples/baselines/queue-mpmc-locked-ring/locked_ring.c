#define _POSIX_C_SOURCE 200112L

#include "vibesys_queue_abi.h"

#include <pthread.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

struct vsq_queue {
  size_t capacity;
  size_t max_value_size;
  uint32_t producer_count;
  uint32_t consumer_count;
  uint8_t *storage;
  size_t *lengths;
  size_t head;
  size_t tail;
  size_t count;
  pthread_mutex_t lock;
};

struct vsq_producer {
  struct vsq_queue *queue;
};

struct vsq_consumer {
  struct vsq_queue *queue;
};

static bool fits_size_t(uint64_t value) {
  return value <= (uint64_t)SIZE_MAX;
}

static bool multiply_overflows(size_t left, size_t right) {
  return right != 0 && left > SIZE_MAX / right;
}

static void destroy_queue(struct vsq_queue *queue) {
  if (queue == NULL) {
    return;
  }
  (void)pthread_mutex_destroy(&queue->lock);
  free(queue->lengths);
  free(queue->storage);
  free(queue);
}

uint32_t vsq_abi_version(void) { return VSQ_ABI_VERSION; }

vsq_status vsq_queue_create(uint64_t capacity, uint64_t max_value_size,
                            uint32_t producer_count,
                            uint32_t consumer_count,
                            struct vsq_queue **queue_out) {
  if (queue_out == NULL || capacity == 0 || max_value_size == 0 ||
      producer_count == 0 || consumer_count == 0 || !fits_size_t(capacity) ||
      !fits_size_t(max_value_size)) {
    return VSQ_INVALID;
  }
  const size_t item_capacity = (size_t)capacity;
  const size_t value_capacity = (size_t)max_value_size;
  if (multiply_overflows(item_capacity, value_capacity) ||
      multiply_overflows(item_capacity, sizeof(size_t))) {
    return VSQ_INVALID;
  }

  struct vsq_queue *queue = calloc(1, sizeof(*queue));
  if (queue == NULL) {
    return VSQ_INTERNAL_ERROR;
  }
  if (pthread_mutex_init(&queue->lock, NULL) != 0) {
    free(queue);
    return VSQ_INTERNAL_ERROR;
  }
  queue->storage = malloc(item_capacity * value_capacity);
  queue->lengths = malloc(item_capacity * sizeof(*queue->lengths));
  if (queue->storage == NULL || queue->lengths == NULL) {
    destroy_queue(queue);
    return VSQ_INTERNAL_ERROR;
  }
  queue->capacity = item_capacity;
  queue->max_value_size = value_capacity;
  queue->producer_count = producer_count;
  queue->consumer_count = consumer_count;
  *queue_out = queue;
  return VSQ_OK;
}

void vsq_queue_destroy(struct vsq_queue *queue) { destroy_queue(queue); }

vsq_status vsq_producer_create(struct vsq_queue *queue, uint32_t producer_id,
                               struct vsq_producer **producer_out) {
  if (queue == NULL || producer_out == NULL ||
      producer_id >= queue->producer_count) {
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

void vsq_producer_destroy(struct vsq_producer *producer) { free(producer); }

vsq_status vsq_consumer_create(struct vsq_queue *queue, uint32_t consumer_id,
                               struct vsq_consumer **consumer_out) {
  if (queue == NULL || consumer_out == NULL ||
      consumer_id >= queue->consumer_count) {
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

void vsq_consumer_destroy(struct vsq_consumer *consumer) { free(consumer); }

vsq_status vsq_try_enqueue(struct vsq_producer *producer, const uint8_t *data,
                           uint64_t length) {
  if (producer == NULL || !fits_size_t(length)) {
    return VSQ_INVALID;
  }
  struct vsq_queue *queue = producer->queue;
  const size_t value_size = (size_t)length;
  if (value_size > queue->max_value_size ||
      (value_size != 0 && data == NULL)) {
    return VSQ_INVALID;
  }
  if (pthread_mutex_lock(&queue->lock) != 0) {
    return VSQ_INTERNAL_ERROR;
  }
  if (queue->count == queue->capacity) {
    (void)pthread_mutex_unlock(&queue->lock);
    return VSQ_FULL;
  }
  const size_t slot = queue->tail;
  if (value_size != 0) {
    memcpy(queue->storage + slot * queue->max_value_size, data, value_size);
  }
  queue->lengths[slot] = value_size;
  queue->tail = slot + 1 == queue->capacity ? 0 : slot + 1;
  ++queue->count;
  (void)pthread_mutex_unlock(&queue->lock);
  return VSQ_OK;
}

vsq_status vsq_try_dequeue(struct vsq_consumer *consumer, uint8_t *output,
                           uint64_t output_capacity,
                           uint64_t *output_length) {
  if (consumer == NULL || output_length == NULL ||
      !fits_size_t(output_capacity)) {
    return VSQ_INVALID;
  }
  struct vsq_queue *queue = consumer->queue;
  const size_t output_size = (size_t)output_capacity;
  if (pthread_mutex_lock(&queue->lock) != 0) {
    return VSQ_INTERNAL_ERROR;
  }
  if (queue->count == 0) {
    (void)pthread_mutex_unlock(&queue->lock);
    return VSQ_EMPTY;
  }
  const size_t slot = queue->head;
  const size_t value_size = queue->lengths[slot];
  if (value_size > output_size || (value_size != 0 && output == NULL)) {
    (void)pthread_mutex_unlock(&queue->lock);
    return VSQ_INVALID;
  }
  if (value_size != 0) {
    memcpy(output, queue->storage + slot * queue->max_value_size, value_size);
  }
  *output_length = (uint64_t)value_size;
  queue->head = slot + 1 == queue->capacity ? 0 : slot + 1;
  --queue->count;
  (void)pthread_mutex_unlock(&queue->lock);
  return VSQ_OK;
}
