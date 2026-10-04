#ifndef VIBESYS_UNORDERED_MAP_ABI_H
#define VIBESYS_UNORDERED_MAP_ABI_H

#include <stdint.h>

#define VSUM_ABI_VERSION 1u

typedef struct vsum_map vsum_map;
typedef struct vsum_client vsum_client;

typedef uint32_t vsum_status;

#define VSUM_OK 0u
#define VSUM_MISSING 1u
#define VSUM_INVALID 2u
#define VSUM_INTERNAL_ERROR 3u

#ifdef __cplusplus
extern "C" {
#endif

uint32_t vsum_abi_version(void);

vsum_status vsum_map_create(
    uint64_t max_key_size,
    uint64_t max_value_size,
    uint32_t client_count,
    vsum_map **map_out);

void vsum_map_destroy(vsum_map *map);

vsum_status vsum_client_create(
    vsum_map *map,
    uint32_t client_id,
    vsum_client **client_out);

void vsum_client_destroy(vsum_client *client);

vsum_status vsum_try_put(
    vsum_client *client,
    const uint8_t *key,
    uint64_t key_len,
    const uint8_t *value,
    uint64_t value_len);

vsum_status vsum_try_get(
    vsum_client *client,
    const uint8_t *key,
    uint64_t key_len,
    uint8_t *output,
    uint64_t output_capacity,
    uint64_t *output_length);

vsum_status vsum_try_remove(
    vsum_client *client,
    const uint8_t *key,
    uint64_t key_len,
    uint8_t *output,
    uint64_t output_capacity,
    uint64_t *output_length);

#ifdef __cplusplus
}
#endif

#endif
