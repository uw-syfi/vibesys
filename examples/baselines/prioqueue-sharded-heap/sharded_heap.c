#define _POSIX_C_SOURCE 200112L

#include "vibesys_priority_queue_abi.h"

#include <pthread.h>
#include <stdalign.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define CACHE_LINE 64u
#define MAX_SHARDS 16u

/*
 * Producers own a shard, so pushes only contend with the consumers that happen
 * to visit that shard. Consumers scan the shards for the smallest priority and
 * then pop from the winning shard, so pops from different shards run in
 * parallel.
 *
 * The scan is not a global snapshot: a lower priority can land in a shard the
 * scan already passed. That insertion always happens inside the dequeue call
 * that missed it, so the two operations overlap and the missed enqueue is still
 * orderable after the dequeue. See README.md for the linearizability argument
 * and for the reservation window that the contract permits.
 *
 * Payload slots are the capacity tokens. A producer takes a slot before it
 * copies, and a consumer returns a slot after it copies, so both memcpy calls
 * run outside every lock.
 */
struct heap_entry {
  uint64_t priority;
  size_t slot;
};

struct shard {
  alignas(CACHE_LINE) pthread_mutex_t lock;
  struct heap_entry *entries;
  size_t count;
};

struct vspq_queue {
  size_t capacity;
  size_t max_value_size;
  uint32_t producer_count;
  uint32_t consumer_count;
  size_t shard_count;
  uint8_t *storage;
  size_t *lengths;
  struct heap_entry *entry_arena;
  struct shard *shards;
  size_t shards_initialized;

  alignas(CACHE_LINE) pthread_mutex_t pool_lock;
  size_t *free_slots;
  size_t free_count;
};

struct vspq_producer {
  struct vspq_queue *queue;
  size_t shard;
};

struct vspq_consumer {
  struct vspq_queue *queue;
  size_t scan_start;
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

static void *allocate_aligned(size_t size) {
  void *pointer = NULL;
  if (posix_memalign(&pointer, CACHE_LINE, size) != 0) {
    return NULL;
  }
  return pointer;
}

static void destroy_queue(struct vspq_queue *queue) {
  if (queue == NULL) {
    return;
  }
  for (size_t index = 0; index < queue->shards_initialized; ++index) {
    (void)pthread_mutex_destroy(&queue->shards[index].lock);
  }
  (void)pthread_mutex_destroy(&queue->pool_lock);
  free(queue->shards);
  free(queue->entry_arena);
  free(queue->free_slots);
  free(queue->lengths);
  free(queue->storage);
  free(queue);
}

/* Takes one capacity token. SIZE_MAX means the queue holds capacity items. */
static size_t acquire_slot(struct vspq_queue *queue) {
  if (pthread_mutex_lock(&queue->pool_lock) != 0) {
    return SIZE_MAX;
  }
  size_t slot = SIZE_MAX;
  if (queue->free_count != 0) {
    slot = queue->free_slots[--queue->free_count];
  }
  (void)pthread_mutex_unlock(&queue->pool_lock);
  return slot;
}

static void release_slot(struct vspq_queue *queue, size_t slot) {
  if (pthread_mutex_lock(&queue->pool_lock) != 0) {
    return;
  }
  queue->free_slots[queue->free_count++] = slot;
  (void)pthread_mutex_unlock(&queue->pool_lock);
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
  const size_t shard_count =
      producer_count < MAX_SHARDS ? (size_t)producer_count : (size_t)MAX_SHARDS;
  if (multiply_overflows(item_capacity, value_capacity) ||
      multiply_overflows(item_capacity, sizeof(size_t)) ||
      multiply_overflows(item_capacity, sizeof(struct heap_entry)) ||
      multiply_overflows(item_capacity * sizeof(struct heap_entry),
                         shard_count)) {
    return VSPQ_INVALID;
  }

  struct vspq_queue *queue = calloc(1, sizeof(*queue));
  if (queue == NULL) {
    return VSPQ_INTERNAL_ERROR;
  }
  if (pthread_mutex_init(&queue->pool_lock, NULL) != 0) {
    free(queue);
    return VSPQ_INTERNAL_ERROR;
  }
  queue->storage = malloc(item_capacity * value_capacity);
  queue->lengths = malloc(item_capacity * sizeof(*queue->lengths));
  queue->free_slots = malloc(item_capacity * sizeof(*queue->free_slots));
  queue->entry_arena =
      malloc(item_capacity * shard_count * sizeof(*queue->entry_arena));
  queue->shards = allocate_aligned(shard_count * sizeof(*queue->shards));
  if (queue->storage == NULL || queue->lengths == NULL ||
      queue->free_slots == NULL || queue->entry_arena == NULL ||
      queue->shards == NULL) {
    destroy_queue(queue);
    return VSPQ_INTERNAL_ERROR;
  }
  for (size_t index = 0; index < shard_count; ++index) {
    struct shard *shard = &queue->shards[index];
    if (pthread_mutex_init(&shard->lock, NULL) != 0) {
      destroy_queue(queue);
      return VSPQ_INTERNAL_ERROR;
    }
    queue->shards_initialized = index + 1;
    shard->entries = queue->entry_arena + index * item_capacity;
    shard->count = 0;
  }
  for (size_t index = 0; index < item_capacity; ++index) {
    queue->free_slots[index] = index;
  }
  queue->free_count = item_capacity;
  queue->capacity = item_capacity;
  queue->max_value_size = value_capacity;
  queue->producer_count = producer_count;
  queue->consumer_count = consumer_count;
  queue->shard_count = shard_count;
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
  producer->shard = (size_t)producer_id % queue->shard_count;
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
  /* Stagger the scan so consumers do not queue up on the same shard lock. */
  consumer->scan_start = (size_t)consumer_id % queue->shard_count;
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
  const size_t slot = acquire_slot(queue);
  if (slot == SIZE_MAX) {
    return VSPQ_FULL;
  }
  if (value_size != 0) {
    memcpy(queue->storage + slot * queue->max_value_size, data, value_size);
  }
  queue->lengths[slot] = value_size;

  struct shard *shard = &queue->shards[producer->shard];
  if (pthread_mutex_lock(&shard->lock) != 0) {
    release_slot(queue, slot);
    return VSPQ_INTERNAL_ERROR;
  }
  const size_t index = shard->count;
  shard->entries[index].priority = priority;
  shard->entries[index].slot = slot;
  shard->count = index + 1;
  sift_up(shard->entries, index);
  (void)pthread_mutex_unlock(&shard->lock);
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
  const size_t shard_count = queue->shard_count;

  for (;;) {
    size_t best = SIZE_MAX;
    uint64_t best_priority = 0;
    uint64_t runner_up_priority = UINT64_MAX;
    for (size_t step = 0; step < shard_count; ++step) {
      size_t index = consumer->scan_start + step;
      if (index >= shard_count) {
        index -= shard_count;
      }
      struct shard *shard = &queue->shards[index];
      if (pthread_mutex_lock(&shard->lock) != 0) {
        return VSPQ_INTERNAL_ERROR;
      }
      const bool occupied = shard->count != 0;
      const uint64_t priority = occupied ? shard->entries[0].priority : 0;
      (void)pthread_mutex_unlock(&shard->lock);
      if (!occupied) {
        continue;
      }
      if (best == SIZE_MAX || priority < best_priority) {
        if (best != SIZE_MAX) {
          runner_up_priority = best_priority;
        }
        best = index;
        best_priority = priority;
      } else if (priority < runner_up_priority) {
        runner_up_priority = priority;
      }
    }
    if (best == SIZE_MAX) {
      return VSPQ_EMPTY;
    }

    struct shard *shard = &queue->shards[best];
    if (pthread_mutex_lock(&shard->lock) != 0) {
      return VSPQ_INTERNAL_ERROR;
    }
    /*
     * The winning entry may be gone: another consumer can pop the shard between
     * the scan and this lock. Only commit while the shard still beats every
     * other shard the scan observed, otherwise start over. A shard whose
     * priority dropped below the scanned value received a concurrent enqueue,
     * which is orderable after this dequeue.
     */
    if (shard->count == 0 || shard->entries[0].priority > runner_up_priority) {
      (void)pthread_mutex_unlock(&shard->lock);
      continue;
    }
    const size_t slot = shard->entries[0].slot;
    const size_t value_size = queue->lengths[slot];
    if (value_size > output_size || (value_size != 0 && output == NULL)) {
      (void)pthread_mutex_unlock(&shard->lock);
      return VSPQ_INVALID;
    }
    const size_t last = shard->count - 1;
    shard->entries[0] = shard->entries[last];
    shard->count = last;
    sift_down(shard->entries, last, 0);
    (void)pthread_mutex_unlock(&shard->lock);

    if (value_size != 0) {
      memcpy(output, queue->storage + slot * queue->max_value_size, value_size);
    }
    *output_length = (uint64_t)value_size;
    release_slot(queue, slot);
    return VSPQ_OK;
  }
}
