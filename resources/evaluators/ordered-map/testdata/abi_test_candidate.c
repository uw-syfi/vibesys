#include "vibesys_ordered_map_abi.h"

#include <pthread.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

struct item {
    uint8_t *key;
    uint64_t key_len;
    uint8_t *value;
    uint64_t value_len;
};

struct vsom_map {
    pthread_mutex_t mutex;
    struct item *items;
    uint64_t size;
    uint64_t capacity;
    uint64_t max_key_size;
    uint64_t max_value_size;
    uint32_t client_count;
};

struct vsom_client {
    struct vsom_map *map;
};

#ifdef VSOM_TEST_RETAIN_INPUT
#define VSOM_FREE_BUFFER(value) ((void)(value))
#else
#define VSOM_FREE_BUFFER(value) free(value)
#endif

uint32_t vsom_abi_version(void) {
    return VSOM_ABI_VERSION;
}

static int key_cmp(const uint8_t *left, uint64_t left_len, const uint8_t *right, uint64_t right_len) {
    uint64_t n = left_len < right_len ? left_len : right_len;
    int cmp = 0;
    if (n != 0) {
        cmp = memcmp(left, right, (size_t)n);
    }
    if (cmp != 0) {
        return cmp;
    }
    if (left_len < right_len) {
        return -1;
    }
    if (left_len > right_len) {
        return 1;
    }
    return 0;
}

static uint8_t *copy_bytes(const uint8_t *data, uint64_t length) {
    if (length == 0) {
        return NULL;
    }
#ifdef VSOM_TEST_RETAIN_INPUT
    return (uint8_t *)data;
#else
    uint8_t *copy = malloc((size_t)length);
    if (copy == NULL) {
        return NULL;
    }
    memcpy(copy, data, (size_t)length);
    return copy;
#endif
}

vsom_status vsom_map_create(
    uint64_t max_key_size,
    uint64_t max_value_size,
    uint32_t client_count,
    vsom_map **map_out) {
    if (max_key_size == 0 || max_value_size == 0 || client_count == 0 || map_out == NULL) {
        return VSOM_INVALID;
    }
    struct vsom_map *map = calloc(1, sizeof(*map));
    if (map == NULL || pthread_mutex_init(&map->mutex, NULL) != 0) {
        free(map);
        return VSOM_INTERNAL_ERROR;
    }
    map->max_key_size = max_key_size;
    map->max_value_size = max_value_size;
    map->client_count = client_count;
    *map_out = map;
    return VSOM_OK;
}

void vsom_map_destroy(vsom_map *map) {
#ifdef VSOM_TEST_HANG_ON_DESTROY
    volatile unsigned int keep_running = 1;
    while (keep_running) {
    }
#endif
    if (map == NULL) {
        return;
    }
    for (uint64_t index = 0; index < map->size; ++index) {
        VSOM_FREE_BUFFER(map->items[index].key);
        VSOM_FREE_BUFFER(map->items[index].value);
    }
    pthread_mutex_destroy(&map->mutex);
    free(map->items);
    free(map);
}

vsom_status vsom_client_create(
    vsom_map *map,
    uint32_t client_id,
    vsom_client **client_out) {
    if (map == NULL || client_out == NULL || client_id >= map->client_count) {
        return VSOM_INVALID;
    }
    struct vsom_client *client = malloc(sizeof(*client));
    if (client == NULL) {
        return VSOM_INTERNAL_ERROR;
    }
    client->map = map;
    *client_out = client;
    return VSOM_OK;
}

void vsom_client_destroy(vsom_client *client) {
    free(client);
}

static uint64_t lower_bound(const struct vsom_map *map, const uint8_t *key, uint64_t key_len) {
    uint64_t lo = 0;
    uint64_t hi = map->size;
    while (lo < hi) {
        uint64_t mid = lo + (hi - lo) / 2;
        if (key_cmp(map->items[mid].key, map->items[mid].key_len, key, key_len) < 0) {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    return lo;
}

vsom_status vsom_try_put(
    vsom_client *client,
    const uint8_t *key,
    uint64_t key_len,
    const uint8_t *value,
    uint64_t value_len) {
    if (client == NULL || (key == NULL && key_len != 0) || (value == NULL && value_len != 0) ||
        key_len > client->map->max_key_size || value_len > client->map->max_value_size) {
        return VSOM_INVALID;
    }
#ifdef VSOM_TEST_HANG_ON_PUT
    if (client->map->client_count == 1) {
        volatile unsigned int keep_running = 1;
        while (keep_running) {
        }
    }
#endif
#ifdef VSOM_TEST_FIXED_LENGTH_ONLY
    if (key_len != client->map->max_key_size || value_len != client->map->max_value_size) {
        return VSOM_INVALID;
    }
#endif
    struct vsom_map *map = client->map;
    pthread_mutex_lock(&map->mutex);
    uint64_t index = lower_bound(map, key, key_len);
    bool found = index < map->size &&
        key_cmp(map->items[index].key, map->items[index].key_len, key, key_len) == 0;
    uint8_t *key_copy = found ? map->items[index].key : copy_bytes(key, key_len);
    uint8_t *value_copy = copy_bytes(value, value_len);
    if ((key_len != 0 && key_copy == NULL) || (value_len != 0 && value_copy == NULL)) {
        if (!found) {
            VSOM_FREE_BUFFER(key_copy);
        }
        VSOM_FREE_BUFFER(value_copy);
        pthread_mutex_unlock(&map->mutex);
        return VSOM_INTERNAL_ERROR;
    }
    if (found) {
        VSOM_FREE_BUFFER(map->items[index].value);
        map->items[index].value = value_copy;
        map->items[index].value_len = value_len;
        pthread_mutex_unlock(&map->mutex);
        return VSOM_OK;
    }
    if (map->size == map->capacity) {
        uint64_t next = map->capacity == 0 ? 8 : map->capacity * 2;
        struct item *items = realloc(map->items, (size_t)next * sizeof(*items));
        if (items == NULL) {
            VSOM_FREE_BUFFER(key_copy);
            VSOM_FREE_BUFFER(value_copy);
            pthread_mutex_unlock(&map->mutex);
            return VSOM_INTERNAL_ERROR;
        }
        map->items = items;
        map->capacity = next;
    }
    memmove(&map->items[index + 1], &map->items[index], (size_t)(map->size - index) * sizeof(*map->items));
    map->items[index].key = key_copy;
    map->items[index].key_len = key_len;
    map->items[index].value = value_copy;
    map->items[index].value_len = value_len;
    map->size++;
    pthread_mutex_unlock(&map->mutex);
    return VSOM_OK;
}

static vsom_status lookup(
    vsom_client *client,
    const uint8_t *key,
    uint64_t key_len,
    uint8_t *value_out,
    uint64_t value_cap,
    uint64_t *value_len,
    bool remove) {
    if (client == NULL || value_len == NULL || (key == NULL && key_len != 0)) {
        return VSOM_INVALID;
    }
    if (key_len > client->map->max_key_size) {
        return VSOM_INVALID;
    }
    struct vsom_map *map = client->map;
    pthread_mutex_lock(&map->mutex);
    uint64_t index = lower_bound(map, key, key_len);
    if (index >= map->size ||
        key_cmp(map->items[index].key, map->items[index].key_len, key, key_len) != 0) {
        pthread_mutex_unlock(&map->mutex);
        return VSOM_MISSING;
    }
    struct item *item = &map->items[index];
    if (item->value_len > value_cap || (value_out == NULL && item->value_len != 0)) {
        pthread_mutex_unlock(&map->mutex);
        return VSOM_INVALID;
    }
    if (item->value_len != 0) {
        memcpy(value_out, item->value, (size_t)item->value_len);
    }
    *value_len = item->value_len;
    if (remove) {
        VSOM_FREE_BUFFER(item->key);
        VSOM_FREE_BUFFER(item->value);
        memmove(&map->items[index], &map->items[index + 1], (size_t)(map->size - index - 1) * sizeof(*map->items));
        map->size--;
    }
    pthread_mutex_unlock(&map->mutex);
    return VSOM_OK;
}

vsom_status vsom_try_get(
    vsom_client *client,
    const uint8_t *key,
    uint64_t key_len,
    uint8_t *value_out,
    uint64_t value_cap,
    uint64_t *value_len) {
    return lookup(client, key, key_len, value_out, value_cap, value_len, false);
}

vsom_status vsom_try_remove(
    vsom_client *client,
    const uint8_t *key,
    uint64_t key_len,
    uint8_t *value_out,
    uint64_t value_cap,
    uint64_t *value_len) {
    return lookup(client, key, key_len, value_out, value_cap, value_len, true);
}

static vsom_status copy_item(
    const struct item *item,
    uint8_t *key_out,
    uint64_t key_cap,
    uint64_t *key_len,
    uint8_t *val_out,
    uint64_t val_cap,
    uint64_t *val_len) {
    if (key_len == NULL || val_len == NULL) {
        return VSOM_INVALID;
    }
    if (item->key_len > key_cap || item->value_len > val_cap ||
        (key_out == NULL && item->key_len != 0) || (val_out == NULL && item->value_len != 0)) {
        return VSOM_INVALID;
    }
    if (item->key_len != 0) {
        memcpy(key_out, item->key, (size_t)item->key_len);
    }
    if (item->value_len != 0) {
        memcpy(val_out, item->value, (size_t)item->value_len);
    }
    *key_len = item->key_len;
    *val_len = item->value_len;
    return VSOM_OK;
}

static vsom_status endpoint(
    vsom_client *client,
    bool max,
    uint8_t *key_out,
    uint64_t key_cap,
    uint64_t *key_len,
    uint8_t *val_out,
    uint64_t val_cap,
    uint64_t *val_len) {
    if (client == NULL) {
        return VSOM_INVALID;
    }
    struct vsom_map *map = client->map;
    pthread_mutex_lock(&map->mutex);
    if (map->size == 0) {
        pthread_mutex_unlock(&map->mutex);
        return VSOM_MISSING;
    }
    struct item *item = max ? &map->items[map->size - 1] : &map->items[0];
    vsom_status status = copy_item(item, key_out, key_cap, key_len, val_out, val_cap, val_len);
    pthread_mutex_unlock(&map->mutex);
    return status;
}

vsom_status vsom_try_min(
    vsom_client *client,
    uint8_t *key_out,
    uint64_t key_cap,
    uint64_t *key_len,
    uint8_t *val_out,
    uint64_t val_cap,
    uint64_t *val_len) {
    return endpoint(client, false, key_out, key_cap, key_len, val_out, val_cap, val_len);
}

vsom_status vsom_try_max(
    vsom_client *client,
    uint8_t *key_out,
    uint64_t key_cap,
    uint64_t *key_len,
    uint8_t *val_out,
    uint64_t val_cap,
    uint64_t *val_len) {
    return endpoint(client, true, key_out, key_cap, key_len, val_out, val_cap, val_len);
}

static vsom_status neighbor(
    vsom_client *client,
    const uint8_t *key,
    uint64_t key_len,
    bool successor,
    uint8_t *key_out,
    uint64_t key_cap,
    uint64_t *key_len_out,
    uint8_t *val_out,
    uint64_t val_cap,
    uint64_t *val_len) {
    if (client == NULL || (key == NULL && key_len != 0)) {
        return VSOM_INVALID;
    }
    if (key_len > client->map->max_key_size) {
        return VSOM_INVALID;
    }
    struct vsom_map *map = client->map;
    pthread_mutex_lock(&map->mutex);
    uint64_t index = lower_bound(map, key, key_len);
    if (successor) {
        if (index < map->size &&
            key_cmp(map->items[index].key, map->items[index].key_len, key, key_len) == 0) {
            index++;
        }
        if (index >= map->size) {
            pthread_mutex_unlock(&map->mutex);
            return VSOM_MISSING;
        }
    } else {
        if (index == 0) {
            pthread_mutex_unlock(&map->mutex);
            return VSOM_MISSING;
        }
        index--;
    }
    vsom_status status =
        copy_item(&map->items[index], key_out, key_cap, key_len_out, val_out, val_cap, val_len);
    pthread_mutex_unlock(&map->mutex);
    return status;
}

vsom_status vsom_try_predecessor(
    vsom_client *client,
    const uint8_t *key,
    uint64_t key_len,
    uint8_t *key_out,
    uint64_t key_cap,
    uint64_t *key_len_out,
    uint8_t *val_out,
    uint64_t val_cap,
    uint64_t *val_len) {
    return neighbor(
        client, key, key_len, false, key_out, key_cap, key_len_out, val_out, val_cap, val_len);
}

vsom_status vsom_try_successor(
    vsom_client *client,
    const uint8_t *key,
    uint64_t key_len,
    uint8_t *key_out,
    uint64_t key_cap,
    uint64_t *key_len_out,
    uint8_t *val_out,
    uint64_t val_cap,
    uint64_t *val_len) {
    return neighbor(
        client, key, key_len, true, key_out, key_cap, key_len_out, val_out, val_cap, val_len);
}

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
    uint64_t *remaining_out) {
    if (client == NULL || count_out == NULL || remaining_out == NULL ||
        (start == NULL && start_len != 0) || (end == NULL && end_len != 0)) {
        return VSOM_INVALID;
    }
    if (start_len > client->map->max_key_size || end_len > client->map->max_key_size) {
        return VSOM_INVALID;
    }
    struct vsom_map *map = client->map;
    pthread_mutex_lock(&map->mutex);
    uint64_t begin = lower_bound(map, start, start_len);
    uint64_t stop = lower_bound(map, end, end_len);
    uint64_t available = begin < stop ? stop - begin : 0;
    if (max_items == 0) {
        *count_out = 0;
        *remaining_out = available == 0 ? 0 : 1;
        pthread_mutex_unlock(&map->mutex);
        return VSOM_OK;
    }
    if (lengths_out_key == NULL || lengths_out_val == NULL) {
        pthread_mutex_unlock(&map->mutex);
        return VSOM_INVALID;
    }
    uint64_t take = available < max_items ? available : max_items;
    for (uint64_t index = 0; index < take; ++index) {
        struct item *item = &map->items[begin + index];
        if (item->key_len > key_stride || item->value_len > val_stride) {
            pthread_mutex_unlock(&map->mutex);
            return VSOM_INVALID;
        }
    }
    if ((keys_out == NULL && key_stride * max_items != 0) ||
        (vals_out == NULL && val_stride * max_items != 0)) {
        pthread_mutex_unlock(&map->mutex);
        return VSOM_INVALID;
    }
    for (uint64_t index = 0; index < take; ++index) {
        struct item *item = &map->items[begin + index];
        if (item->key_len != 0) {
            memcpy(keys_out + index * key_stride, item->key, (size_t)item->key_len);
        }
        if (item->value_len != 0) {
            memcpy(vals_out + index * val_stride, item->value, (size_t)item->value_len);
        }
        lengths_out_key[index] = item->key_len;
        lengths_out_val[index] = item->value_len;
    }
    *count_out = take;
    *remaining_out = available > max_items ? 1 : 0;
    pthread_mutex_unlock(&map->mutex);
    return VSOM_OK;
}
