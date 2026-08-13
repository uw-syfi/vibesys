#include "vibesys_priority_queue_abi.h"

#include <oneapi/tbb/concurrent_priority_queue.h>

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <new>
#include <vector>

/*
 * Thin adapter over the unmodified oneTBB tbb::concurrent_priority_queue behind
 * the VibeSys copying byte-value ABI.
 *
 * TBB owns the ordering. Two things it does not provide are supplied here:
 *
 *  - Bounding. tbb::concurrent_priority_queue is unbounded, so a lock-free
 *    stack of payload slots is the capacity token. A producer takes a slot
 *    before it copies and a consumer returns a slot after it copies, so
 *    VSPQ_FULL is reported only when all capacity slots are held and neither
 *    memcpy runs inside a TBB operation.
 *  - Value storage. Heap entries are (priority, slot) pairs into a fixed-stride
 *    arena, so the payload bytes never move while TBB sifts.
 *
 * See README.md for the linearizability argument and for the two places this
 * design deviates from the strict reading of the contract.
 */

namespace {

constexpr std::size_t kCacheLine = 64;
constexpr std::uint32_t kNoSlot = std::numeric_limits<std::uint32_t>::max();

struct entry {
  std::uint64_t priority;
  std::uint32_t slot;
};

/*
 * try_pop removes the greatest element under Compare, and the contract wants
 * the lowest numeric priority, so the order is inverted here rather than by
 * rewriting priorities.
 */
struct lower_priority_ranks_higher {
  bool operator()(const entry &left, const entry &right) const {
    return left.priority > right.priority;
  }
};

/*
 * Tagged Treiber stack of free slot indices. The tag is bumped on every push
 * and pop, so a CAS that observes an unchanged word observes an unchanged
 * stack and cannot splice a recycled slot onto a stale successor. acquire()
 * fails only when the stack is genuinely empty, which is what makes VSPQ_FULL
 * exact.
 */
class slot_pool {
 public:
  /*
   * `next` must outlive the pool and hold `count` entries. The successor links
   * are atomic because acquire() reads next_[index] speculatively, before the
   * CAS proves it still owns `index`, and a concurrent release() of that same
   * slot may be writing it. The value read is then stale and the CAS discards
   * it, but the access itself still has to be race-free.
   */
  void reset(std::atomic<std::uint32_t> *next, std::uint32_t count) {
    next_ = next;
    for (std::uint32_t index = 0; index + 1 < count; ++index) {
      next_[index].store(index + 1, std::memory_order_relaxed);
    }
    if (count != 0) {
      next_[count - 1].store(kNoSlot, std::memory_order_relaxed);
    }
    head_.store(pack(0, count == 0 ? kNoSlot : 0), std::memory_order_relaxed);
  }

  std::uint32_t acquire() {
    std::uint64_t head = head_.load(std::memory_order_acquire);
    for (;;) {
      const std::uint32_t index = slot_of(head);
      if (index == kNoSlot) {
        return kNoSlot;
      }
      const std::uint64_t updated =
          pack(tag_of(head) + 1, next_[index].load(std::memory_order_relaxed));
      if (head_.compare_exchange_weak(head, updated, std::memory_order_acquire,
                                      std::memory_order_acquire)) {
        return index;
      }
    }
  }

  void release(std::uint32_t index) {
    std::uint64_t head = head_.load(std::memory_order_relaxed);
    for (;;) {
      next_[index].store(slot_of(head), std::memory_order_relaxed);
      const std::uint64_t updated = pack(tag_of(head) + 1, index);
      if (head_.compare_exchange_weak(head, updated, std::memory_order_release,
                                      std::memory_order_relaxed)) {
        return;
      }
    }
  }

 private:
  static std::uint64_t pack(std::uint32_t tag, std::uint32_t slot) {
    return (static_cast<std::uint64_t>(tag) << 32) |
           static_cast<std::uint64_t>(slot);
  }
  static std::uint32_t tag_of(std::uint64_t head) {
    return static_cast<std::uint32_t>(head >> 32);
  }
  static std::uint32_t slot_of(std::uint64_t head) {
    return static_cast<std::uint32_t>(head);
  }

  alignas(kCacheLine) std::atomic<std::uint64_t> head_{pack(0, kNoSlot)};
  std::atomic<std::uint32_t> *next_ = nullptr;
};

using priority_queue =
    tbb::concurrent_priority_queue<entry, lower_priority_ranks_higher>;

}  // namespace

struct vspq_queue {
  vspq_queue(std::size_t item_capacity, std::size_t value_capacity,
             std::uint32_t producers, std::uint32_t consumers)
      : capacity(item_capacity),
        max_value_size(value_capacity),
        producer_count(producers),
        consumer_count(consumers),
        storage(item_capacity * value_capacity),
        lengths(item_capacity),
        next_slot(item_capacity),
        items(item_capacity) {
    free_slots.reset(next_slot.data(), static_cast<std::uint32_t>(capacity));
  }

  std::size_t capacity;
  std::size_t max_value_size;
  std::uint32_t producer_count;
  std::uint32_t consumer_count;
  std::vector<std::uint8_t> storage;
  std::vector<std::size_t> lengths;
  std::vector<std::atomic<std::uint32_t>> next_slot;
  slot_pool free_slots;
  priority_queue items;
};

/*
 * TBB has no per-thread handle, so the handles only carry the queue. They exist
 * to satisfy the ABI lifecycle.
 */
struct vspq_producer {
  vspq_queue *queue;
};

struct vspq_consumer {
  vspq_queue *queue;
};

namespace {

bool fits_size_t(std::uint64_t value) {
  return value <=
         static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max());
}

bool multiply_overflows(std::size_t left, std::size_t right) {
  return right != 0 && left > std::numeric_limits<std::size_t>::max() / right;
}

}  // namespace

extern "C" {

std::uint32_t vspq_abi_version(void) { return VSPQ_ABI_VERSION; }

vspq_status vspq_queue_create(std::uint64_t capacity,
                              std::uint64_t max_value_size,
                              std::uint32_t producer_count,
                              std::uint32_t consumer_count,
                              vspq_queue **queue_out) {
  if (queue_out == nullptr || capacity == 0 || max_value_size == 0 ||
      producer_count == 0 || consumer_count == 0 || !fits_size_t(capacity) ||
      !fits_size_t(max_value_size)) {
    return VSPQ_INVALID;
  }
  /* Slot indices are 32-bit so they pack alongside the ABA tag. */
  if (capacity >= static_cast<std::uint64_t>(kNoSlot)) {
    return VSPQ_INVALID;
  }
  const std::size_t item_capacity = static_cast<std::size_t>(capacity);
  const std::size_t value_capacity = static_cast<std::size_t>(max_value_size);
  if (multiply_overflows(item_capacity, value_capacity)) {
    return VSPQ_INVALID;
  }

  try {
    *queue_out = new vspq_queue(item_capacity, value_capacity, producer_count,
                                consumer_count);
  } catch (...) {
    return VSPQ_INTERNAL_ERROR;
  }
  return VSPQ_OK;
}

void vspq_queue_destroy(vspq_queue *queue) { delete queue; }

vspq_status vspq_producer_create(vspq_queue *queue, std::uint32_t producer_id,
                                 vspq_producer **producer_out) {
  if (queue == nullptr || producer_out == nullptr ||
      producer_id >= queue->producer_count) {
    return VSPQ_INVALID;
  }
  vspq_producer *producer = new (std::nothrow) vspq_producer{queue};
  if (producer == nullptr) {
    return VSPQ_INTERNAL_ERROR;
  }
  *producer_out = producer;
  return VSPQ_OK;
}

void vspq_producer_destroy(vspq_producer *producer) { delete producer; }

vspq_status vspq_consumer_create(vspq_queue *queue, std::uint32_t consumer_id,
                                 vspq_consumer **consumer_out) {
  if (queue == nullptr || consumer_out == nullptr ||
      consumer_id >= queue->consumer_count) {
    return VSPQ_INVALID;
  }
  vspq_consumer *consumer = new (std::nothrow) vspq_consumer{queue};
  if (consumer == nullptr) {
    return VSPQ_INTERNAL_ERROR;
  }
  *consumer_out = consumer;
  return VSPQ_OK;
}

void vspq_consumer_destroy(vspq_consumer *consumer) { delete consumer; }

vspq_status vspq_try_enqueue(vspq_producer *producer, std::uint64_t priority,
                             const std::uint8_t *data, std::uint64_t length) {
  if (producer == nullptr || !fits_size_t(length)) {
    return VSPQ_INVALID;
  }
  vspq_queue *queue = producer->queue;
  const std::size_t value_size = static_cast<std::size_t>(length);
  if (value_size > queue->max_value_size ||
      (value_size != 0 && data == nullptr)) {
    return VSPQ_INVALID;
  }

  const std::uint32_t slot = queue->free_slots.acquire();
  if (slot == kNoSlot) {
    return VSPQ_FULL;
  }
  if (value_size != 0) {
    std::memcpy(queue->storage.data() + slot * queue->max_value_size, data,
                value_size);
  }
  queue->lengths[slot] = value_size;

  try {
    queue->items.push(entry{priority, slot});
  } catch (...) {
    queue->free_slots.release(slot);
    return VSPQ_INTERNAL_ERROR;
  }
  return VSPQ_OK;
}

vspq_status vspq_try_dequeue(vspq_consumer *consumer, std::uint8_t *output,
                             std::uint64_t output_capacity,
                             std::uint64_t *output_length) {
  if (consumer == nullptr || output_length == nullptr ||
      !fits_size_t(output_capacity)) {
    return VSPQ_INVALID;
  }
  vspq_queue *queue = consumer->queue;
  const std::size_t output_size = static_cast<std::size_t>(output_capacity);

  entry popped{};
  if (!queue->items.try_pop(popped)) {
    return VSPQ_EMPTY;
  }

  const std::size_t value_size = queue->lengths[popped.slot];
  if (value_size > output_size || (value_size != 0 && output == nullptr)) {
    /*
     * TBB cannot peek, so an undersized output is only detectable after the
     * removal. Requeue the untouched entry and report VSPQ_INVALID. See the
     * README: this is the one operation that is not externally atomic.
     */
    try {
      queue->items.push(popped);
    } catch (...) {
      queue->free_slots.release(popped.slot);
      return VSPQ_INTERNAL_ERROR;
    }
    return VSPQ_INVALID;
  }

  if (value_size != 0) {
    std::memcpy(output, queue->storage.data() + popped.slot * queue->max_value_size,
                value_size);
  }
  *output_length = static_cast<std::uint64_t>(value_size);
  queue->free_slots.release(popped.slot);
  return VSPQ_OK;
}

}  // extern "C"
