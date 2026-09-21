#include "vibesys_unordered_map_abi.h"

#include <oneapi/tbb/concurrent_hash_map.h>

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <new>
#include <vector>

/*
 * Thin adapter over the unmodified oneTBB tbb::concurrent_hash_map behind the
 * VibeSys copying byte-key ABI.
 *
 * TBB owns the map. The adapter copies keys and values into std::vector
 * buffers, hashes those bytes, and holds a TBB accessor for the duration of
 * each call so get/remove can peek before committing an undersized INVALID.
 *
 * See README.md for the linearizability argument.
 */

namespace {

struct byte_hash_compare {
  static std::size_t hash(const std::vector<std::uint8_t> &key) {
    std::size_t value = static_cast<std::size_t>(1469598103934665603ull);
    for (std::uint8_t byte : key) {
      value ^= static_cast<std::size_t>(byte);
      value *= static_cast<std::size_t>(1099511628211ull);
    }
    return value;
  }

  static bool equal(const std::vector<std::uint8_t> &left,
                    const std::vector<std::uint8_t> &right) {
    return left == right;
  }
};

using map_type =
    tbb::concurrent_hash_map<std::vector<std::uint8_t>,
                             std::vector<std::uint8_t>, byte_hash_compare>;

bool fits_size_t(std::uint64_t value) {
  return value <=
         static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max());
}

std::vector<std::uint8_t> copy_bytes(const std::uint8_t *data,
                                     std::uint64_t length) {
  std::vector<std::uint8_t> out(static_cast<std::size_t>(length));
  if (length != 0) {
    std::memcpy(out.data(), data, static_cast<std::size_t>(length));
  }
  return out;
}

}  // namespace

struct vsum_map {
  vsum_map(std::size_t key_capacity, std::size_t value_capacity,
           std::uint32_t clients)
      : max_key_size(key_capacity),
        max_value_size(value_capacity),
        client_count(clients) {}

  std::size_t max_key_size;
  std::size_t max_value_size;
  std::uint32_t client_count;
  map_type items;
};

/*
 * TBB has no per-thread handle, so the handles only carry the map. They exist
 * to satisfy the ABI lifecycle.
 */
struct vsum_client {
  vsum_map *map;
};

extern "C" {

std::uint32_t vsum_abi_version(void) { return VSUM_ABI_VERSION; }

vsum_status vsum_map_create(std::uint64_t max_key_size,
                            std::uint64_t max_value_size,
                            std::uint32_t client_count, vsum_map **map_out) {
  if (map_out == nullptr || max_key_size == 0 || max_value_size == 0 ||
      client_count == 0 || !fits_size_t(max_key_size) ||
      !fits_size_t(max_value_size)) {
    return VSUM_INVALID;
  }

  try {
    *map_out = new vsum_map(static_cast<std::size_t>(max_key_size),
                            static_cast<std::size_t>(max_value_size),
                            client_count);
  } catch (...) {
    return VSUM_INTERNAL_ERROR;
  }
  return VSUM_OK;
}

void vsum_map_destroy(vsum_map *map) { delete map; }

vsum_status vsum_client_create(vsum_map *map, std::uint32_t client_id,
                               vsum_client **client_out) {
  if (map == nullptr || client_out == nullptr ||
      client_id >= map->client_count) {
    return VSUM_INVALID;
  }
  vsum_client *client = new (std::nothrow) vsum_client{map};
  if (client == nullptr) {
    return VSUM_INTERNAL_ERROR;
  }
  *client_out = client;
  return VSUM_OK;
}

void vsum_client_destroy(vsum_client *client) { delete client; }

vsum_status vsum_try_put(vsum_client *client, const std::uint8_t *key,
                         std::uint64_t key_len, const std::uint8_t *value,
                         std::uint64_t value_len) {
  if (client == nullptr || !fits_size_t(key_len) || !fits_size_t(value_len) ||
      (key == nullptr && key_len != 0) || (value == nullptr && value_len != 0)) {
    return VSUM_INVALID;
  }
  vsum_map *map = client->map;
  if (key_len > map->max_key_size || value_len > map->max_value_size) {
    return VSUM_INVALID;
  }

  try {
    std::vector<std::uint8_t> stored_key = copy_bytes(key, key_len);
    std::vector<std::uint8_t> stored_value = copy_bytes(value, value_len);
    map_type::accessor accessor;
    map->items.insert(accessor, stored_key);
    accessor->second = std::move(stored_value);
  } catch (...) {
    return VSUM_INTERNAL_ERROR;
  }
  return VSUM_OK;
}

vsum_status vsum_try_get(vsum_client *client, const std::uint8_t *key,
                         std::uint64_t key_len, std::uint8_t *output,
                         std::uint64_t output_capacity,
                         std::uint64_t *output_length) {
  if (client == nullptr || output_length == nullptr || !fits_size_t(key_len) ||
      !fits_size_t(output_capacity) || (key == nullptr && key_len != 0)) {
    return VSUM_INVALID;
  }
  vsum_map *map = client->map;
  if (key_len > map->max_key_size) {
    return VSUM_INVALID;
  }

  try {
    std::vector<std::uint8_t> stored_key = copy_bytes(key, key_len);
    map_type::const_accessor accessor;
    if (!map->items.find(accessor, stored_key)) {
      return VSUM_MISSING;
    }
    const std::size_t value_size = accessor->second.size();
    if (value_size > static_cast<std::size_t>(output_capacity) ||
        (value_size != 0 && output == nullptr)) {
      return VSUM_INVALID;
    }
    if (value_size != 0) {
      std::memcpy(output, accessor->second.data(), value_size);
    }
    *output_length = static_cast<std::uint64_t>(value_size);
  } catch (...) {
    return VSUM_INTERNAL_ERROR;
  }
  return VSUM_OK;
}

vsum_status vsum_try_remove(vsum_client *client, const std::uint8_t *key,
                            std::uint64_t key_len, std::uint8_t *output,
                            std::uint64_t output_capacity,
                            std::uint64_t *output_length) {
  if (client == nullptr || output_length == nullptr || !fits_size_t(key_len) ||
      !fits_size_t(output_capacity) || (key == nullptr && key_len != 0)) {
    return VSUM_INVALID;
  }
  vsum_map *map = client->map;
  if (key_len > map->max_key_size) {
    return VSUM_INVALID;
  }

  try {
    std::vector<std::uint8_t> stored_key = copy_bytes(key, key_len);
    map_type::accessor accessor;
    if (!map->items.find(accessor, stored_key)) {
      return VSUM_MISSING;
    }
    const std::size_t value_size = accessor->second.size();
    if (value_size > static_cast<std::size_t>(output_capacity) ||
        (value_size != 0 && output == nullptr)) {
      return VSUM_INVALID;
    }
    if (value_size != 0) {
      std::memcpy(output, accessor->second.data(), value_size);
    }
    *output_length = static_cast<std::uint64_t>(value_size);
    map->items.erase(accessor);
  } catch (...) {
    return VSUM_INTERNAL_ERROR;
  }
  return VSUM_OK;
}

}  // extern "C"
