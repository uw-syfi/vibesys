#define _POSIX_C_SOURCE 200112L

#include "vibesys_priority_queue_abi.h"

#include <pthread.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/*
 * Entry order in the heap array is the only thing that changes as items are
 * pushed and popped. Each entry keeps its payload slot, so sifting never moves
 * value bytes, and the slot column stays a permutation of [0, capacity): the
 * entry at index count is always free storage for the next push.
 */
struct heap_entry {
  uint64_t priority;
  size_t slot;
};

struct vspq_queue {
  size_t capacity;
  size_t max_value_size;
  uint32_t producer_count;
  uint32_t consumer_count;
  uint8_t *storage;
  size_t *lengths;
  struct heap_entry *entries;
  size_t count;
  pthread_mutex_t lock;
};

struct vspq_producer {
  struct vspq_queue *queue;
};

struct vspq_consumer {
  struct vspq_queue *queue;
};

static bool fits_size_t(uint64_t value) { return value <= (uint64_t)SIZE_MAX; }

static bool multiply_overflows(size_t left, size_t right) {
  return right != 0 && left > SIZE_MAX / right;
}

static void swap_entries(struct heap_entry *entries, size_t left, size_t right) {
  const struct heap_entry held = entries[left];
  entries[left] = entries[right];
  entries[right] = held;
}

static void sift_up(struct heap_entry *entries, size_t index) {
  while (index != 0) {
    const size_t parent = (index - 1) / 2;
    if (entries[parent].priority <= entries[index].priority) {
      return;
    }
    swap_entries(entries, parent, index);
    index = parent;
  }
}

static void sift_down(struct heap_entry *entries, size_t count, size_t index) {
  for (;;) {
    const size_t left = index * 2 + 1;
    if (left >= count) {
      return;
    }
    const size_t right = left + 1;
    size_t smallest = left;
    if (right < count && entries[right].priority < entries[left].priority) {
      smallest = right;
    }
    if (entries[index].priority <= entries[smallest].priority) {
      return;
    }
    swap_entries(entries, index, smallest);
    index = smallest;
  }
}

static void destroy_queue(struct vspq_queue *queue) {
  if (queue == NULL) {
    return;
  }
  (void)pthread_mutex_destroy(&queue->lock);
  free(queue->entries);
  free(queue->lengths);
  free(queue->storage);
  free(queue);
}

uint32_t vspq_abi_version(void) { return VSPQ_ABI_VERSION; }

vspq_status vspq_queue_create(uint64_t capacity, uint64_t max_value_size,
                              uint32_t producer_count, uint32_t consumer_count,
                              struct vspq_queue **queue_out) {
  if (queue_out == NULL || capacity == 0 || max_value_size == 0 ||
      producer_count == 0 || consumer_count == 0 || !fits_size_t(capacity) ||
      !fits_size_t(max_value_size)) {
    return VSPQ_INVALID;
  }
  const size_t item_capacity = (size_t)capacity;
  const size_t value_capacity = (size_t)max_value_size;
  if (multiply_overflows(item_capacity, value_capacity) ||
      multiply_overflows(item_capacity, sizeof(size_t)) ||
      multiply_overflows(item_capacity, sizeof(struct heap_entry))) {
    return VSPQ_INVALID;
  }

  struct vspq_queue *queue = calloc(1, sizeof(*queue));
  if (queue == NULL) {
    return VSPQ_INTERNAL_ERROR;
  }
  if (pthread_mutex_init(&queue->lock, NULL) != 0) {
    free(queue);
    return VSPQ_INTERNAL_ERROR;
  }
  queue->storage = malloc(item_capacity * value_capacity);
  queue->lengths = malloc(item_capacity * sizeof(*queue->lengths));
  queue->entries = malloc(item_capacity * sizeof(*queue->entries));
  if (queue->storage == NULL || queue->lengths == NULL ||
      queue->entries == NULL) {
    destroy_queue(queue);
    return VSPQ_INTERNAL_ERROR;
  }
  for (size_t index = 0; index < item_capacity; ++index) {
    queue->entries[index].priority = 0;
    queue->entries[index].slot = index;
  }
  queue->capacity = item_capacity;
  queue->max_value_size = value_capacity;
  queue->producer_count = producer_count;
  queue->consumer_count = consumer_count;
  *queue_out = queue;
  return VSPQ_OK;
}

void vspq_queue_destroy(struct vspq_queue *queue) { destroy_queue(queue); }

vspq_status vspq_producer_create(struct vspq_queue *queue,
                                 uint32_t producer_id,
                                 struct vspq_producer **producer_out) {
  if (queue == NULL || producer_out == NULL ||
      producer_id >= queue->producer_count) {
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

void vspq_producer_destroy(struct vspq_producer *producer) { free(producer); }

vspq_status vspq_consumer_create(struct vspq_queue *queue,
                                 uint32_t consumer_id,
                                 struct vspq_consumer **consumer_out) {
  if (queue == NULL || consumer_out == NULL ||
      consumer_id >= queue->consumer_count) {
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

void vspq_consumer_destroy(struct vspq_consumer *consumer) { free(consumer); }

vspq_status vspq_try_enqueue(struct vspq_producer *producer, uint64_t priority,
                             const uint8_t *data, uint64_t length) {
  if (producer == NULL || !fits_size_t(length)) {
    return VSPQ_INVALID;
  }
  struct vspq_queue *queue = producer->queue;
  const size_t value_size = (size_t)length;
  if (value_size > queue->max_value_size || (value_size != 0 && data == NULL)) {
    return VSPQ_INVALID;
  }
  if (pthread_mutex_lock(&queue->lock) != 0) {
    return VSPQ_INTERNAL_ERROR;
  }
  if (queue->count == queue->capacity) {
    (void)pthread_mutex_unlock(&queue->lock);
    return VSPQ_FULL;
  }
  const size_t index = queue->count;
  const size_t slot = queue->entries[index].slot;
  if (value_size != 0) {
    memcpy(queue->storage + slot * queue->max_value_size, data, value_size);
  }
  queue->lengths[slot] = value_size;
  queue->entries[index].priority = priority;
  queue->count = index + 1;
  sift_up(queue->entries, index);
  (void)pthread_mutex_unlock(&queue->lock);
  return VSPQ_OK;
}

vspq_status vspq_try_dequeue(struct vspq_consumer *consumer, uint8_t *output,
                             uint64_t output_capacity,
                             uint64_t *output_length) {
  if (consumer == NULL || output_length == NULL ||
      !fits_size_t(output_capacity)) {
    return VSPQ_INVALID;
  }
  struct vspq_queue *queue = consumer->queue;
  const size_t output_size = (size_t)output_capacity;
  if (pthread_mutex_lock(&queue->lock) != 0) {
    return VSPQ_INTERNAL_ERROR;
  }
  if (queue->count == 0) {
    (void)pthread_mutex_unlock(&queue->lock);
    return VSPQ_EMPTY;
  }
  const size_t slot = queue->entries[0].slot;
  const size_t value_size = queue->lengths[slot];
  if (value_size > output_size || (value_size != 0 && output == NULL)) {
    (void)pthread_mutex_unlock(&queue->lock);
    return VSPQ_INVALID;
  }
  if (value_size != 0) {
    memcpy(output, queue->storage + slot * queue->max_value_size, value_size);
  }
  *output_length = (uint64_t)value_size;
  /*
   * Move the removed entry past the live prefix instead of overwriting it so
   * its slot returns to the free tail of the permutation.
   */
  const size_t last = queue->count - 1;
  swap_entries(queue->entries, 0, last);
  queue->count = last;
  sift_down(queue->entries, last, 0);
  (void)pthread_mutex_unlock(&queue->lock);
  return VSPQ_OK;
}
