#include "vibesys_stack_abi.h"

#include <boost/lockfree/stack.hpp>

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <new>
#include <vector>

/*
 * Thin adapter over the unmodified Boost.Lockfree stack behind the VibeSys
 * copying byte-value ABI.
 *
 * Boost owns LIFO ordering. Two things it does not provide are supplied here:
 *
 *  - Bounding. boost::lockfree::stack is a node-pool Treiber stack. A second
 *    Boost stack of free slot indices is the capacity token. A producer takes
 *    a slot before it copies and a consumer returns a slot after it copies, so
 *    VSST_FULL is reported only when all capacity slots are held and neither
 *    memcpy runs inside a Boost operation.
 *  - Value storage. Stack entries are slot indices into a fixed-stride arena,
 *    so payload bytes never move while Boost splices nodes.
 *
 * See README.md for the linearizability argument and for the two places this
 * design deviates from the strict reading of the contract.
 */

namespace {

constexpr std::uint32_t kNoSlot = std::numeric_limits<std::uint32_t>::max();

using index_stack = boost::lockfree::stack<std::uint32_t>;

bool fits_size_t(std::uint64_t value) {
  return value <=
         static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max());
}

bool multiply_overflows(std::size_t left, std::size_t right) {
  return right != 0 && left > std::numeric_limits<std::size_t>::max() / right;
}

}  // namespace

struct vsst_stack {
  vsst_stack(std::size_t item_capacity, std::size_t value_capacity,
             std::uint32_t producers, std::uint32_t consumers)
      : capacity(item_capacity),
        max_value_size(value_capacity),
        producer_count(producers),
        consumer_count(consumers),
        storage(item_capacity * value_capacity),
        lengths(item_capacity),
        free_slots(item_capacity),
        items(item_capacity) {
    for (std::uint32_t index = 0; index < static_cast<std::uint32_t>(capacity);
         ++index) {
      if (!free_slots.bounded_push(index)) {
        throw std::bad_alloc();
      }
    }
  }

  std::size_t capacity;
  std::size_t max_value_size;
  std::uint32_t producer_count;
  std::uint32_t consumer_count;
  std::vector<std::uint8_t> storage;
  std::vector<std::size_t> lengths;
  index_stack free_slots;
  index_stack items;
};

struct vsst_producer {
  vsst_stack *stack;
};

struct vsst_consumer {
  vsst_stack *stack;
};

extern "C" {

std::uint32_t vsst_abi_version(void) { return VSST_ABI_VERSION; }

vsst_status vsst_stack_create(std::uint64_t capacity,
                              std::uint64_t max_value_size,
                              std::uint32_t producer_count,
                              std::uint32_t consumer_count,
                              vsst_stack **stack_out) {
  if (stack_out == nullptr || capacity == 0 || max_value_size == 0 ||
      producer_count == 0 || consumer_count == 0 || !fits_size_t(capacity) ||
      !fits_size_t(max_value_size)) {
    return VSST_INVALID;
  }
  if (capacity >= static_cast<std::uint64_t>(kNoSlot)) {
    return VSST_INVALID;
  }
  const std::size_t item_capacity = static_cast<std::size_t>(capacity);
  const std::size_t value_capacity = static_cast<std::size_t>(max_value_size);
  if (multiply_overflows(item_capacity, value_capacity)) {
    return VSST_INVALID;
  }

  try {
    *stack_out = new vsst_stack(item_capacity, value_capacity, producer_count,
                                consumer_count);
  } catch (...) {
    return VSST_INTERNAL_ERROR;
  }
  return VSST_OK;
}

void vsst_stack_destroy(vsst_stack *stack) { delete stack; }

vsst_status vsst_producer_create(vsst_stack *stack, std::uint32_t producer_id,
                                 vsst_producer **producer_out) {
  if (stack == nullptr || producer_out == nullptr ||
      producer_id >= stack->producer_count) {
    return VSST_INVALID;
  }
  vsst_producer *producer = new (std::nothrow) vsst_producer{stack};
  if (producer == nullptr) {
    return VSST_INTERNAL_ERROR;
  }
  *producer_out = producer;
  return VSST_OK;
}

void vsst_producer_destroy(vsst_producer *producer) { delete producer; }

vsst_status vsst_consumer_create(vsst_stack *stack, std::uint32_t consumer_id,
                                 vsst_consumer **consumer_out) {
  if (stack == nullptr || consumer_out == nullptr ||
      consumer_id >= stack->consumer_count) {
    return VSST_INVALID;
  }
  vsst_consumer *consumer = new (std::nothrow) vsst_consumer{stack};
  if (consumer == nullptr) {
    return VSST_INTERNAL_ERROR;
  }
  *consumer_out = consumer;
  return VSST_OK;
}

void vsst_consumer_destroy(vsst_consumer *consumer) { delete consumer; }

vsst_status vsst_try_push(vsst_producer *producer, const std::uint8_t *data,
                          std::uint64_t length) {
  if (producer == nullptr || !fits_size_t(length) ||
      (data == nullptr && length != 0)) {
    return VSST_INVALID;
  }
  vsst_stack *stack = producer->stack;
  const std::size_t value_size = static_cast<std::size_t>(length);
  if (value_size > stack->max_value_size) {
    return VSST_INVALID;
  }

  std::uint32_t slot = kNoSlot;
  if (!stack->free_slots.pop(slot)) {
    return VSST_FULL;
  }
  if (value_size != 0) {
    std::memcpy(stack->storage.data() + slot * stack->max_value_size, data,
                value_size);
  }
  stack->lengths[slot] = value_size;
  if (!stack->items.bounded_push(slot)) {
    if (!stack->free_slots.bounded_push(slot)) {
      return VSST_INTERNAL_ERROR;
    }
    return VSST_FULL;
  }
  return VSST_OK;
}

vsst_status vsst_try_pop(vsst_consumer *consumer, std::uint8_t *output,
                         std::uint64_t output_capacity,
                         std::uint64_t *output_length) {
  if (consumer == nullptr || output_length == nullptr ||
      !fits_size_t(output_capacity)) {
    return VSST_INVALID;
  }
  vsst_stack *stack = consumer->stack;

  std::uint32_t slot = kNoSlot;
  if (!stack->items.pop(slot)) {
    return VSST_EMPTY;
  }

  const std::size_t value_size = stack->lengths[slot];
  if (value_size > static_cast<std::size_t>(output_capacity) ||
      (value_size != 0 && output == nullptr)) {
    /*
     * Boost cannot peek, so an undersized output is only detectable after the
     * removal. Requeue the untouched slot and report VSST_INVALID. See the
     * README: this is the one operation that is not externally atomic.
     */
    if (!stack->items.bounded_push(slot)) {
      if (!stack->free_slots.bounded_push(slot)) {
        return VSST_INTERNAL_ERROR;
      }
      return VSST_INTERNAL_ERROR;
    }
    return VSST_INVALID;
  }

  if (value_size != 0) {
    std::memcpy(output, stack->storage.data() + slot * stack->max_value_size,
                value_size);
  }
  *output_length = static_cast<std::uint64_t>(value_size);
  if (!stack->free_slots.bounded_push(slot)) {
    return VSST_INTERNAL_ERROR;
  }
  return VSST_OK;
}

}  // extern "C"
