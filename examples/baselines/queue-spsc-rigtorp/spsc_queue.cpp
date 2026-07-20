/*
 * The ring algorithm in this evaluator-side baseline is adapted from
 * rigtorp/SPSCQueue. It intentionally lives outside the optimizer input.
 *
 * Copyright (c) 2020 Erik Rigtorp <erik@rigtorp.se>
 * Copyright (c) 2026 VibeSys contributors
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to
 * deal in the Software without restriction, including without limitation the
 * rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
 * sell copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
 * FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
 * IN THE SOFTWARE.
 */

#include "vibesys_queue_abi.h"

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <memory>
#include <new>

namespace {

constexpr std::size_t kCacheLineSize = 64;

bool fits_size_t(std::uint64_t value) noexcept {
  return value <= std::numeric_limits<std::size_t>::max();
}

bool multiply_overflows(std::size_t left, std::size_t right) noexcept {
  return right != 0 && left > std::numeric_limits<std::size_t>::max() / right;
}

std::size_t advance(std::size_t index, std::size_t ring_size) noexcept {
  ++index;
  return index == ring_size ? 0 : index;
}

} // namespace

struct vsq_queue {
  vsq_queue(std::size_t requested_capacity, std::size_t requested_value_size)
      : ring_size(requested_capacity + 1),
        max_value_size(requested_value_size),
        slots(new (std::nothrow)
                  std::uint8_t[ring_size * requested_value_size]),
        lengths(new (std::nothrow) std::size_t[ring_size]) {}

  const std::size_t ring_size;
  const std::size_t max_value_size;
  std::unique_ptr<std::uint8_t[]> slots;
  std::unique_ptr<std::size_t[]> lengths;

  // Each thread writes only its own index. Isolating them prevents false
  // sharing; remote-index caches live in the thread-confined handles below.
  alignas(kCacheLineSize) std::atomic<std::size_t> write_index{0};
  alignas(kCacheLineSize) std::atomic<std::size_t> read_index{0};
};

struct alignas(kCacheLineSize) vsq_producer {
  explicit vsq_producer(vsq_queue *owner) : queue(owner) {}

  vsq_queue *queue;
  std::size_t read_index_cache{0};
};

struct alignas(kCacheLineSize) vsq_consumer {
  explicit vsq_consumer(vsq_queue *owner) : queue(owner) {}

  vsq_queue *queue;
  std::size_t write_index_cache{0};
};

extern "C" {

std::uint32_t vsq_abi_version() { return VSQ_ABI_VERSION; }

vsq_status vsq_queue_create(std::uint64_t capacity,
                            std::uint64_t max_value_size,
                            std::uint32_t producer_count,
                            std::uint32_t consumer_count,
                            vsq_queue **queue_out) {
  if (queue_out == nullptr || capacity == 0 || max_value_size == 0 ||
      producer_count != 1 || consumer_count != 1 || !fits_size_t(capacity) ||
      !fits_size_t(max_value_size)) {
    return VSQ_INVALID;
  }

  const auto capacity_size = static_cast<std::size_t>(capacity);
  const auto value_size = static_cast<std::size_t>(max_value_size);
  if (capacity_size == std::numeric_limits<std::size_t>::max()) {
    return VSQ_INVALID;
  }
  const auto ring_size = capacity_size + 1;
  if (multiply_overflows(ring_size, value_size) ||
      multiply_overflows(ring_size, sizeof(std::size_t))) {
    return VSQ_INVALID;
  }

  auto queue = std::unique_ptr<vsq_queue>(
      new (std::nothrow) vsq_queue(capacity_size, value_size));
  if (!queue || !queue->slots || !queue->lengths) {
    return VSQ_INTERNAL_ERROR;
  }

  *queue_out = queue.release();
  return VSQ_OK;
}

void vsq_queue_destroy(vsq_queue *queue) { delete queue; }

vsq_status vsq_producer_create(vsq_queue *queue, std::uint32_t producer_id,
                               vsq_producer **producer_out) {
  if (queue == nullptr || producer_id != 0 || producer_out == nullptr) {
    return VSQ_INVALID;
  }
  auto producer = std::unique_ptr<vsq_producer>(
      new (std::nothrow) vsq_producer(queue));
  if (!producer) {
    return VSQ_INTERNAL_ERROR;
  }
  *producer_out = producer.release();
  return VSQ_OK;
}

void vsq_producer_destroy(vsq_producer *producer) { delete producer; }

vsq_status vsq_consumer_create(vsq_queue *queue, std::uint32_t consumer_id,
                               vsq_consumer **consumer_out) {
  if (queue == nullptr || consumer_id != 0 || consumer_out == nullptr) {
    return VSQ_INVALID;
  }
  auto consumer = std::unique_ptr<vsq_consumer>(
      new (std::nothrow) vsq_consumer(queue));
  if (!consumer) {
    return VSQ_INTERNAL_ERROR;
  }
  *consumer_out = consumer.release();
  return VSQ_OK;
}

void vsq_consumer_destroy(vsq_consumer *consumer) { delete consumer; }

vsq_status vsq_try_enqueue(vsq_producer *producer, const std::uint8_t *data,
                           std::uint64_t length) {
  if (producer == nullptr || !fits_size_t(length)) {
    return VSQ_INVALID;
  }
  auto &queue = *producer->queue;
  const auto value_size = static_cast<std::size_t>(length);
  if (value_size > queue.max_value_size ||
      (value_size != 0 && data == nullptr)) {
    return VSQ_INVALID;
  }

  const auto write_index =
      queue.write_index.load(std::memory_order_relaxed);
  const auto next_write_index = advance(write_index, queue.ring_size);
  if (next_write_index == producer->read_index_cache) {
    producer->read_index_cache =
        queue.read_index.load(std::memory_order_acquire);
    if (next_write_index == producer->read_index_cache) {
      return VSQ_FULL;
    }
  }

  if (value_size != 0) {
    std::memcpy(queue.slots.get() + write_index * queue.max_value_size, data,
                value_size);
  }
  queue.lengths[write_index] = value_size;
  queue.write_index.store(next_write_index, std::memory_order_release);
  return VSQ_OK;
}

vsq_status vsq_try_dequeue(vsq_consumer *consumer, std::uint8_t *output,
                           std::uint64_t output_capacity,
                           std::uint64_t *output_length) {
  if (consumer == nullptr || output_length == nullptr ||
      !fits_size_t(output_capacity)) {
    return VSQ_INVALID;
  }
  auto &queue = *consumer->queue;
  const auto read_index = queue.read_index.load(std::memory_order_relaxed);
  if (read_index == consumer->write_index_cache) {
    consumer->write_index_cache =
        queue.write_index.load(std::memory_order_acquire);
    if (read_index == consumer->write_index_cache) {
      return VSQ_EMPTY;
    }
  }

  const auto value_size = queue.lengths[read_index];
  if (value_size > static_cast<std::size_t>(output_capacity) ||
      (value_size != 0 && output == nullptr)) {
    return VSQ_INVALID;
  }
  if (value_size != 0) {
    std::memcpy(output,
                queue.slots.get() + read_index * queue.max_value_size,
                value_size);
  }
  *output_length = value_size;
  queue.read_index.store(advance(read_index, queue.ring_size),
                         std::memory_order_release);
  return VSQ_OK;
}

} // extern "C"
