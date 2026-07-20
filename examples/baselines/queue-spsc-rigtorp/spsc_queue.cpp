#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Winterference-size"
#endif
#include "rigtorp/SPSCQueue.h"
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC diagnostic pop
#endif
#include "vibesys_queue_abi.h"

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <memory>
#include <new>

namespace {

bool fits_size_t(std::uint64_t value) noexcept {
  return value <= std::numeric_limits<std::size_t>::max();
}

bool multiply_overflows(std::size_t left, std::size_t right) noexcept {
  return right != 0 && left > std::numeric_limits<std::size_t>::max() / right;
}

struct QueueEntry {
  QueueEntry(std::uint8_t *destination, const std::uint8_t *source,
             std::size_t source_size) noexcept
      : data(destination), size(source_size) {
    if (size != 0) {
      std::memcpy(data, source, size);
    }
  }

  std::uint8_t *data;
  std::size_t size;
};

} // namespace

struct vsq_queue {
  vsq_queue(std::size_t requested_capacity, std::size_t requested_value_size)
      : capacity(requested_capacity), max_value_size(requested_value_size),
        payloads(
            std::make_unique<std::uint8_t[]>(capacity * max_value_size)),
        entries(capacity) {}

  const std::size_t capacity;
  const std::size_t max_value_size;
  std::unique_ptr<std::uint8_t[]> payloads;
  rigtorp::SPSCQueue<QueueEntry> entries;
};

struct vsq_producer {
  explicit vsq_producer(vsq_queue *owner) : queue(owner) {}

  vsq_queue *queue;
  std::size_t payload_index{0};
};

struct vsq_consumer {
  explicit vsq_consumer(vsq_queue *owner) : queue(owner) {}

  vsq_queue *queue;
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
  if (capacity_size == std::numeric_limits<std::size_t>::max() ||
      multiply_overflows(capacity_size, value_size)) {
    return VSQ_INVALID;
  }

  try {
    auto queue = std::make_unique<vsq_queue>(capacity_size, value_size);
    *queue_out = queue.release();
  } catch (...) {
    return VSQ_INTERNAL_ERROR;
  }
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

  auto *destination = queue.payloads.get() +
                      producer->payload_index * queue.max_value_size;
  if (!queue.entries.try_emplace(destination, data, value_size)) {
    return VSQ_FULL;
  }
  ++producer->payload_index;
  if (producer->payload_index == queue.capacity) {
    producer->payload_index = 0;
  }
  return VSQ_OK;
}

vsq_status vsq_try_dequeue(vsq_consumer *consumer, std::uint8_t *output,
                           std::uint64_t output_capacity,
                           std::uint64_t *output_length) {
  if (consumer == nullptr || output_length == nullptr ||
      !fits_size_t(output_capacity)) {
    return VSQ_INVALID;
  }

  auto *entry = consumer->queue->entries.front();
  if (entry == nullptr) {
    return VSQ_EMPTY;
  }
  if (entry->size > static_cast<std::size_t>(output_capacity) ||
      (entry->size != 0 && output == nullptr)) {
    return VSQ_INVALID;
  }
  if (entry->size != 0) {
    std::memcpy(output, entry->data, entry->size);
  }
  *output_length = entry->size;
  consumer->queue->entries.pop();
  return VSQ_OK;
}

} // extern "C"
