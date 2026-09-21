#include "vibesys_ordered_map_abi.h"

#include <oneapi/tbb/concurrent_map.h>

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <memory>
#include <new>
#include <string>
#include <string_view>
#include <utility>

/*
 * Adapter over unmodified oneTBB tbb::concurrent_map.
 *
 * concurrent_map insert/find/iterate are concurrent. Erase is not concurrent
 * with those operations, and the mapped value is an unsynchronized object, so
 * the adapter never erases. A missing mapping is a null published blob; put
 * publishes a new blob with atomic_store on the node's shared_ptr. Lookups
 * use a transparent unsigned-byte comparator and string_view, so an 8-byte
 * get does not heap-allocate. See README.md.
 */

namespace {

struct byte_less {
  using is_transparent = void;

  bool operator()(std::string_view left, std::string_view right) const {
    const std::size_t n = left.size() < right.size() ? left.size() : right.size();
    if (n != 0) {
      const int order = std::memcmp(left.data(), right.data(), n);
      if (order != 0) {
        return order < 0;
      }
    }
    return left.size() < right.size();
  }
};

using blob = std::string;
using blob_ptr = std::shared_ptr<const blob>;
using map_type = tbb::concurrent_map<std::string, blob_ptr, byte_less>;

bool fits_size_t(std::uint64_t value) {
  return value <=
         static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max());
}

bool multiply_overflows(std::size_t left, std::size_t right) {
  return right != 0 && left > std::numeric_limits<std::size_t>::max() / right;
}

std::string_view bytes_view(const std::uint8_t *data, std::uint64_t length) {
  if (length == 0) {
    return std::string_view();
  }
  return std::string_view(reinterpret_cast<const char *>(data),
                          static_cast<std::size_t>(length));
}

blob_ptr load_blob(const blob_ptr &slot) {
  return std::atomic_load_explicit(&slot, std::memory_order_acquire);
}

void store_blob(blob_ptr &slot, blob_ptr value) {
  std::atomic_store_explicit(&slot, std::move(value), std::memory_order_release);
}

bool cas_blob(blob_ptr &slot, blob_ptr &expected, blob_ptr desired) {
  return std::atomic_compare_exchange_strong_explicit(
      &slot, &expected, std::move(desired), std::memory_order_acq_rel,
      std::memory_order_acquire);
}

bool write_mapping(std::string_view key, std::string_view value,
                   std::uint8_t *key_out, std::uint64_t key_cap,
                   std::uint64_t *key_len, std::uint8_t *val_out,
                   std::uint64_t val_cap, std::uint64_t *val_len) {
  if (key.size() > static_cast<std::size_t>(key_cap) ||
      value.size() > static_cast<std::size_t>(val_cap) ||
      (key.size() != 0 && key_out == nullptr) ||
      (value.size() != 0 && val_out == nullptr)) {
    return false;
  }
  if (key.size() != 0) {
    std::memcpy(key_out, key.data(), key.size());
  }
  if (value.size() != 0) {
    std::memcpy(val_out, value.data(), value.size());
  }
  *key_len = static_cast<std::uint64_t>(key.size());
  *val_len = static_cast<std::uint64_t>(value.size());
  return true;
}

blob_ptr make_blob(const std::uint8_t *data, std::uint64_t length) {
  return std::make_shared<blob>(bytes_view(data, length));
}

}  // namespace

struct vsom_map {
  vsom_map(std::size_t key_capacity, std::size_t value_capacity,
           std::uint32_t clients)
      : max_key_size(key_capacity),
        max_value_size(value_capacity),
        client_count(clients) {}

  std::size_t max_key_size;
  std::size_t max_value_size;
  std::uint32_t client_count;
  map_type items;
};

struct vsom_client {
  vsom_map *map;
};

extern "C" {

std::uint32_t vsom_abi_version(void) { return VSOM_ABI_VERSION; }

vsom_status vsom_map_create(std::uint64_t max_key_size,
                            std::uint64_t max_value_size,
                            std::uint32_t client_count, vsom_map **map_out) {
  if (map_out == nullptr || max_key_size == 0 || max_value_size == 0 ||
      client_count == 0 || !fits_size_t(max_key_size) ||
      !fits_size_t(max_value_size)) {
    return VSOM_INVALID;
  }

  try {
    *map_out = new vsom_map(static_cast<std::size_t>(max_key_size),
                            static_cast<std::size_t>(max_value_size),
                            client_count);
  } catch (...) {
    return VSOM_INTERNAL_ERROR;
  }
  return VSOM_OK;
}

void vsom_map_destroy(vsom_map *map) { delete map; }

vsom_status vsom_client_create(vsom_map *map, std::uint32_t client_id,
                               vsom_client **client_out) {
  if (map == nullptr || client_out == nullptr ||
      client_id >= map->client_count) {
    return VSOM_INVALID;
  }
  vsom_client *client = new (std::nothrow) vsom_client{map};
  if (client == nullptr) {
    return VSOM_INTERNAL_ERROR;
  }
  *client_out = client;
  return VSOM_OK;
}

void vsom_client_destroy(vsom_client *client) { delete client; }

vsom_status vsom_try_put(vsom_client *client, const std::uint8_t *key,
                         std::uint64_t key_len, const std::uint8_t *value,
                         std::uint64_t value_len) {
  if (client == nullptr || !fits_size_t(key_len) || !fits_size_t(value_len) ||
      (key == nullptr && key_len != 0) || (value == nullptr && value_len != 0)) {
    return VSOM_INVALID;
  }
  vsom_map *map = client->map;
  if (key_len > map->max_key_size || value_len > map->max_value_size) {
    return VSOM_INVALID;
  }

  try {
    blob_ptr blob = make_blob(value, value_len);
    // Insert a null blob first so the node is published with a trivial
    // shared_ptr. The live value is always installed with atomic_store, which
    // is the only legal concurrent write of TBB's unsynchronized mapped value.
    const auto result =
        map->items.emplace(std::string(bytes_view(key, key_len)), blob_ptr{});
    store_blob(result.first->second, std::move(blob));
  } catch (...) {
    return VSOM_INTERNAL_ERROR;
  }
  return VSOM_OK;
}

vsom_status vsom_try_get(vsom_client *client, const std::uint8_t *key,
                         std::uint64_t key_len, std::uint8_t *value_out,
                         std::uint64_t value_cap, std::uint64_t *value_len) {
  if (client == nullptr || value_len == nullptr || !fits_size_t(key_len) ||
      !fits_size_t(value_cap) || (key == nullptr && key_len != 0)) {
    return VSOM_INVALID;
  }
  vsom_map *map = client->map;
  if (key_len > map->max_key_size) {
    return VSOM_INVALID;
  }

  try {
    const auto it = map->items.find(bytes_view(key, key_len));
    if (it == map->items.end()) {
      return VSOM_MISSING;
    }
    const blob_ptr blob = load_blob(it->second);
    if (!blob) {
      return VSOM_MISSING;
    }
    if (blob->size() > static_cast<std::size_t>(value_cap) ||
        (blob->size() != 0 && value_out == nullptr)) {
      return VSOM_INVALID;
    }
    if (blob->size() != 0) {
      std::memcpy(value_out, blob->data(), blob->size());
    }
    *value_len = static_cast<std::uint64_t>(blob->size());
  } catch (...) {
    return VSOM_INTERNAL_ERROR;
  }
  return VSOM_OK;
}

vsom_status vsom_try_remove(vsom_client *client, const std::uint8_t *key,
                            std::uint64_t key_len, std::uint8_t *value_out,
                            std::uint64_t value_cap, std::uint64_t *value_len) {
  if (client == nullptr || value_len == nullptr || !fits_size_t(key_len) ||
      !fits_size_t(value_cap) || (key == nullptr && key_len != 0)) {
    return VSOM_INVALID;
  }
  vsom_map *map = client->map;
  if (key_len > map->max_key_size) {
    return VSOM_INVALID;
  }

  try {
    const auto it = map->items.find(bytes_view(key, key_len));
    if (it == map->items.end()) {
      return VSOM_MISSING;
    }
    for (;;) {
      blob_ptr expected = load_blob(it->second);
      if (!expected) {
        return VSOM_MISSING;
      }
      if (expected->size() > static_cast<std::size_t>(value_cap) ||
          (expected->size() != 0 && value_out == nullptr)) {
        return VSOM_INVALID;
      }
      blob_ptr empty;
      if (!cas_blob(it->second, expected, std::move(empty))) {
        continue;
      }
      if (expected->size() != 0) {
        std::memcpy(value_out, expected->data(), expected->size());
      }
      *value_len = static_cast<std::uint64_t>(expected->size());
      return VSOM_OK;
    }
  } catch (...) {
    return VSOM_INTERNAL_ERROR;
  }
}

vsom_status vsom_try_min(vsom_client *client, std::uint8_t *key_out,
                         std::uint64_t key_cap, std::uint64_t *key_len,
                         std::uint8_t *val_out, std::uint64_t val_cap,
                         std::uint64_t *val_len) {
  if (client == nullptr || key_len == nullptr || val_len == nullptr ||
      !fits_size_t(key_cap) || !fits_size_t(val_cap)) {
    return VSOM_INVALID;
  }

  try {
    for (auto it = client->map->items.begin(); it != client->map->items.end();
         ++it) {
      const blob_ptr blob = load_blob(it->second);
      if (!blob) {
        continue;
      }
      if (!write_mapping(it->first, *blob, key_out, key_cap, key_len, val_out,
                         val_cap, val_len)) {
        return VSOM_INVALID;
      }
      return VSOM_OK;
    }
  } catch (...) {
    return VSOM_INTERNAL_ERROR;
  }
  return VSOM_MISSING;
}

vsom_status vsom_try_max(vsom_client *client, std::uint8_t *key_out,
                         std::uint64_t key_cap, std::uint64_t *key_len,
                         std::uint8_t *val_out, std::uint64_t val_cap,
                         std::uint64_t *val_len) {
  if (client == nullptr || key_len == nullptr || val_len == nullptr ||
      !fits_size_t(key_cap) || !fits_size_t(val_cap)) {
    return VSOM_INVALID;
  }

  try {
    map_type::iterator last = client->map->items.end();
    blob_ptr last_blob;
    for (auto it = client->map->items.begin(); it != client->map->items.end();
         ++it) {
      blob_ptr blob = load_blob(it->second);
      if (!blob) {
        continue;
      }
      last = it;
      last_blob = std::move(blob);
    }
    if (last == client->map->items.end()) {
      return VSOM_MISSING;
    }
    if (!write_mapping(last->first, *last_blob, key_out, key_cap, key_len,
                       val_out, val_cap, val_len)) {
      return VSOM_INVALID;
    }
  } catch (...) {
    return VSOM_INTERNAL_ERROR;
  }
  return VSOM_OK;
}

vsom_status vsom_try_predecessor(vsom_client *client, const std::uint8_t *key,
                                 std::uint64_t key_len, std::uint8_t *key_out,
                                 std::uint64_t key_cap, std::uint64_t *key_len_out,
                                 std::uint8_t *val_out, std::uint64_t val_cap,
                                 std::uint64_t *val_len) {
  if (client == nullptr || key_len_out == nullptr || val_len == nullptr ||
      !fits_size_t(key_len) || !fits_size_t(key_cap) || !fits_size_t(val_cap) ||
      (key == nullptr && key_len != 0)) {
    return VSOM_INVALID;
  }
  vsom_map *map = client->map;
  if (key_len > map->max_key_size) {
    return VSOM_INVALID;
  }

  try {
    const std::string_view query = bytes_view(key, key_len);
    const byte_less less{};
    map_type::iterator pred = map->items.end();
    blob_ptr pred_blob;
    for (auto it = map->items.begin(); it != map->items.end(); ++it) {
      if (!less(it->first, query)) {
        break;
      }
      blob_ptr blob = load_blob(it->second);
      if (!blob) {
        continue;
      }
      pred = it;
      pred_blob = std::move(blob);
    }
    if (pred == map->items.end()) {
      return VSOM_MISSING;
    }
    if (!write_mapping(pred->first, *pred_blob, key_out, key_cap, key_len_out,
                       val_out, val_cap, val_len)) {
      return VSOM_INVALID;
    }
  } catch (...) {
    return VSOM_INTERNAL_ERROR;
  }
  return VSOM_OK;
}

vsom_status vsom_try_successor(vsom_client *client, const std::uint8_t *key,
                               std::uint64_t key_len, std::uint8_t *key_out,
                               std::uint64_t key_cap, std::uint64_t *key_len_out,
                               std::uint8_t *val_out, std::uint64_t val_cap,
                               std::uint64_t *val_len) {
  if (client == nullptr || key_len_out == nullptr || val_len == nullptr ||
      !fits_size_t(key_len) || !fits_size_t(key_cap) || !fits_size_t(val_cap) ||
      (key == nullptr && key_len != 0)) {
    return VSOM_INVALID;
  }
  vsom_map *map = client->map;
  if (key_len > map->max_key_size) {
    return VSOM_INVALID;
  }

  try {
    for (auto it = map->items.upper_bound(bytes_view(key, key_len));
         it != map->items.end(); ++it) {
      const blob_ptr blob = load_blob(it->second);
      if (!blob) {
        continue;
      }
      if (!write_mapping(it->first, *blob, key_out, key_cap, key_len_out,
                         val_out, val_cap, val_len)) {
        return VSOM_INVALID;
      }
      return VSOM_OK;
    }
  } catch (...) {
    return VSOM_INTERNAL_ERROR;
  }
  return VSOM_MISSING;
}

vsom_status vsom_try_range(vsom_client *client, const std::uint8_t *start,
                           std::uint64_t start_len, const std::uint8_t *end,
                           std::uint64_t end_len, std::uint8_t *keys_out,
                           std::uint64_t key_stride, std::uint8_t *vals_out,
                           std::uint64_t val_stride, std::uint64_t *lengths_out_key,
                           std::uint64_t *lengths_out_val, std::uint64_t max_items,
                           std::uint64_t *count_out, std::uint64_t *remaining_out) {
  if (client == nullptr || count_out == nullptr || remaining_out == nullptr ||
      !fits_size_t(start_len) || !fits_size_t(end_len) ||
      !fits_size_t(key_stride) || !fits_size_t(val_stride) ||
      !fits_size_t(max_items) || (start == nullptr && start_len != 0) ||
      (end == nullptr && end_len != 0)) {
    return VSOM_INVALID;
  }
  vsom_map *map = client->map;
  if (start_len > map->max_key_size || end_len > map->max_key_size) {
    return VSOM_INVALID;
  }

  try {
    const std::string_view start_key = bytes_view(start, start_len);
    const std::string_view end_key = bytes_view(end, end_len);
    if (!byte_less{}(start_key, end_key)) {
      *count_out = 0;
      *remaining_out = 0;
      return VSOM_OK;
    }

    auto it = map->items.lower_bound(start_key);
    const auto last = map->items.lower_bound(end_key);

    auto next_live = [&]() -> blob_ptr {
      while (it != last) {
        blob_ptr blob = load_blob(it->second);
        if (blob) {
          return blob;
        }
        ++it;
      }
      return blob_ptr();
    };

    if (max_items == 0) {
      *count_out = 0;
      *remaining_out = next_live() ? 1 : 0;
      return VSOM_OK;
    }

    const std::size_t take_cap = static_cast<std::size_t>(max_items);
    const std::size_t key_step = static_cast<std::size_t>(key_stride);
    const std::size_t val_step = static_cast<std::size_t>(val_stride);
    if (multiply_overflows(take_cap, key_step) ||
        multiply_overflows(take_cap, val_step)) {
      return VSOM_INVALID;
    }

    std::size_t count = 0;
    auto probe = it;
    while (probe != last && count < take_cap) {
      const blob_ptr blob = load_blob(probe->second);
      if (blob) {
        if (probe->first.size() > key_step || blob->size() > val_step) {
          return VSOM_INVALID;
        }
        ++count;
      }
      ++probe;
    }
    if ((key_step * take_cap != 0 && keys_out == nullptr) ||
        (val_step * take_cap != 0 && vals_out == nullptr) ||
        lengths_out_key == nullptr || lengths_out_val == nullptr) {
      return VSOM_INVALID;
    }

    count = 0;
    while (it != last && count < take_cap) {
      const blob_ptr blob = load_blob(it->second);
      if (blob) {
        if (!it->first.empty()) {
          std::memcpy(keys_out + count * key_step, it->first.data(),
                      it->first.size());
        }
        if (!blob->empty()) {
          std::memcpy(vals_out + count * val_step, blob->data(), blob->size());
        }
        lengths_out_key[count] = static_cast<std::uint64_t>(it->first.size());
        lengths_out_val[count] = static_cast<std::uint64_t>(blob->size());
        ++count;
      }
      ++it;
    }
    bool remaining = false;
    while (it != last) {
      if (load_blob(it->second)) {
        remaining = true;
        break;
      }
      ++it;
    }
    *count_out = static_cast<std::uint64_t>(count);
    *remaining_out = remaining ? 1 : 0;
  } catch (...) {
    return VSOM_INTERNAL_ERROR;
  }
  return VSOM_OK;
}

}  // extern "C"
