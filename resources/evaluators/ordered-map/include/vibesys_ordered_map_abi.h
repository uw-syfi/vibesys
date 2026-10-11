#ifndef VIBESYS_ORDERED_MAP_ABI_H
#define VIBESYS_ORDERED_MAP_ABI_H

#include <stdint.h>

#define VSOM_ABI_VERSION 1u

typedef struct vsom_map vsom_map;
typedef struct vsom_client vsom_client;

typedef uint32_t vsom_status;

#define VSOM_OK 0u
#define VSOM_MISSING 1u
#define VSOM_INVALID 2u
#define VSOM_INTERNAL_ERROR 3u

#ifdef __cplusplus
extern "C" {
#endif

uint32_t vsom_abi_version(void);

vsom_status vsom_map_create(
    uint64_t max_key_size,
    uint64_t max_value_size,
    uint32_t client_count,
    vsom_map **map_out);

void vsom_map_destroy(vsom_map *map);

vsom_status vsom_client_create(
    vsom_map *map,
    uint32_t client_id,
    vsom_client **client_out);

void vsom_client_destroy(vsom_client *client);

vsom_status vsom_try_put(
    vsom_client *client,
    const uint8_t *key,
    uint64_t key_len,
    const uint8_t *value,
    uint64_t value_len);

vsom_status vsom_try_get(
    vsom_client *client,
    const uint8_t *key,
    uint64_t key_len,
    uint8_t *value_out,
    uint64_t value_cap,
    uint64_t *value_len);

vsom_status vsom_try_remove(
    vsom_client *client,
    const uint8_t *key,
    uint64_t key_len,
    uint8_t *value_out,
    uint64_t value_cap,
    uint64_t *value_len);

vsom_status vsom_try_min(
    vsom_client *client,
    uint8_t *key_out,
    uint64_t key_cap,
    uint64_t *key_len,
    uint8_t *val_out,
    uint64_t val_cap,
    uint64_t *val_len);

vsom_status vsom_try_max(
    vsom_client *client,
    uint8_t *key_out,
    uint64_t key_cap,
    uint64_t *key_len,
    uint8_t *val_out,
    uint64_t val_cap,
    uint64_t *val_len);

vsom_status vsom_try_predecessor(
    vsom_client *client,
    const uint8_t *key,
    uint64_t key_len,
    uint8_t *key_out,
    uint64_t key_cap,
    uint64_t *key_len_out,
    uint8_t *val_out,
    uint64_t val_cap,
    uint64_t *val_len);

vsom_status vsom_try_successor(
    vsom_client *client,
    const uint8_t *key,
    uint64_t key_len,
    uint8_t *key_out,
    uint64_t key_cap,
    uint64_t *key_len_out,
    uint8_t *val_out,
    uint64_t val_cap,
    uint64_t *val_len);

vsom_status vsom_try_range(
    vsom_client *client,
    const uint8_t *start,
    uint64_t start_len,
    const uint8_t *end,
    uint64_t end_len,
    uint8_t *keys_out,
    uint64_t key_stride,
    uint8_t *vals_out,
    uint64_t val_stride,
    uint64_t *lengths_out_key,
    uint64_t *lengths_out_val,
    uint64_t max_items,
    uint64_t *count_out,
    uint64_t *remaining_out);

#ifdef __cplusplus
}
#endif

#endif
