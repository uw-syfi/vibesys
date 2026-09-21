#include "vibesys_unordered_map_abi.h"

#include <pthread.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

struct entry {
    uint8_t *key;
    uint64_t key_len;
    uint8_t *value;
    uint64_t value_len;
    bool occupied;
};

struct vsum_map {
    pthread_mutex_t mutex;
    struct entry *entries;
    uint64_t count;
    uint64_t cap;
    uint64_t max_key_size;
    uint64_t max_value_size;
    uint32_t client_count;
};

struct vsum_client {
    struct vsum_map *map;
};

#ifdef VSUM_TEST_RETAIN_INPUT
#define VSUM_FREE_BUFFER(value) ((void)(value))
#else
#define VSUM_FREE_BUFFER(value) free(value)
#endif

uint32_t vsum_abi_version(void) {
    return VSUM_ABI_VERSION;
}

vsum_status vsum_map_create(
    uint64_t max_key_size,
    uint64_t max_value_size,
    uint32_t client_count,
    vsum_map **map_out) {
    if (max_key_size == 0 || max_value_size == 0 || client_count == 0 || map_out == NULL) {
        return VSUM_INVALID;
    }
    struct vsum_map *map = calloc(1, sizeof(*map));
    if (map == NULL) {
        return VSUM_INTERNAL_ERROR;
    }
    map->cap = 8;
    map->entries = calloc((size_t)map->cap, sizeof(*map->entries));
    if (map->entries == NULL || pthread_mutex_init(&map->mutex, NULL) != 0) {
        free(map->entries);
        free(map);
        return VSUM_INTERNAL_ERROR;
    }
    map->max_key_size = max_key_size;
    map->max_value_size = max_value_size;
    map->client_count = client_count;
    *map_out = map;
    return VSUM_OK;
}

void vsum_map_destroy(vsum_map *map) {
#ifdef VSUM_TEST_HANG_ON_DESTROY
    volatile unsigned int keep_running = 1;
    while (keep_running) {
    }
#endif
    if (map == NULL) {
        return;
    }
    for (uint64_t index = 0; index < map->cap; ++index) {
        if (map->entries[index].occupied) {
            VSUM_FREE_BUFFER(map->entries[index].key);
            VSUM_FREE_BUFFER(map->entries[index].value);
        }
    }
    pthread_mutex_destroy(&map->mutex);
    free(map->entries);
    free(map);
}

vsum_status vsum_client_create(
    vsum_map *map,
    uint32_t client_id,
    vsum_client **client_out) {
    if (map == NULL || client_out == NULL || client_id >= map->client_count) {
        return VSUM_INVALID;
    }
    struct vsum_client *client = malloc(sizeof(*client));
    if (client == NULL) {
        return VSUM_INTERNAL_ERROR;
    }
    client->map = map;
    *client_out = client;
    return VSUM_OK;
}

void vsum_client_destroy(vsum_client *client) {
    free(client);
}

static uint8_t *copy_bytes(const uint8_t *data, uint64_t length) {
    if (length == 0) {
        return NULL;
    }
#ifdef VSUM_TEST_RETAIN_INPUT
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

static bool keys_equal(
    const uint8_t *left,
    uint64_t left_len,
    const uint8_t *right,
    uint64_t right_len) {
    if (left_len != right_len) {
        return false;
    }
    return left_len == 0 || memcmp(left, right, (size_t)left_len) == 0;
}

static struct entry *find_entry(
    struct vsum_map *map,
    const uint8_t *key,
    uint64_t key_len,
    struct entry **empty_out) {
    struct entry *empty = NULL;
    for (uint64_t index = 0; index < map->cap; ++index) {
        struct entry *entry = &map->entries[index];
        if (!entry->occupied) {
            if (empty == NULL) {
                empty = entry;
            }
            continue;
        }
        if (keys_equal(entry->key, entry->key_len, key, key_len)) {
            if (empty_out != NULL) {
                *empty_out = NULL;
            }
            return entry;
        }
    }
    if (empty_out != NULL) {
        *empty_out = empty;
    }
    return NULL;
}

static vsum_status grow_entries(struct vsum_map *map) {
    uint64_t cap = map->cap * 2;
    if (cap < 8) {
        cap = 8;
    }
    struct entry *entries = realloc(map->entries, (size_t)cap * sizeof(*entries));
    if (entries == NULL) {
        return VSUM_INTERNAL_ERROR;
    }
    memset(entries + map->cap, 0, (size_t)(cap - map->cap) * sizeof(*entries));
    map->entries = entries;
    map->cap = cap;
    return VSUM_OK;
}

vsum_status vsum_try_put(
    vsum_client *client,
    const uint8_t *key,
    uint64_t key_len,
    const uint8_t *value,
    uint64_t value_len) {
    if (client == NULL || (key == NULL && key_len != 0) || (value == NULL && value_len != 0) ||
        key_len > client->map->max_key_size || value_len > client->map->max_value_size) {
        return VSUM_INVALID;
    }
#ifdef VSUM_TEST_HANG_SINGLE_CLIENT
    if (client->map->client_count == 1) {
        volatile unsigned int keep_running = 1;
        while (keep_running) {
        }
    }
#endif
#ifdef VSUM_TEST_FIXED_LENGTH_ONLY
    if (key_len != client->map->max_key_size || value_len != client->map->max_value_size) {
        return VSUM_INVALID;
    }
#endif
    struct vsum_map *map = client->map;
    pthread_mutex_lock(&map->mutex);
    struct entry *empty = NULL;
    struct entry *existing = find_entry(map, key, key_len, &empty);
    if (existing == NULL && empty == NULL) {
        if (grow_entries(map) != VSUM_OK) {
            pthread_mutex_unlock(&map->mutex);
            return VSUM_INTERNAL_ERROR;
        }
        existing = find_entry(map, key, key_len, &empty);
    }
    uint8_t *key_copy = existing != NULL ? existing->key : copy_bytes(key, key_len);
    uint8_t *value_copy = copy_bytes(value, value_len);
    if ((key_len != 0 && key_copy == NULL) || (value_len != 0 && value_copy == NULL)) {
        if (existing == NULL) {
            VSUM_FREE_BUFFER(key_copy);
        }
        VSUM_FREE_BUFFER(value_copy);
        pthread_mutex_unlock(&map->mutex);
        return VSUM_INTERNAL_ERROR;
    }
    if (existing != NULL) {
        VSUM_FREE_BUFFER(existing->value);
        existing->value = value_copy;
        existing->value_len = value_len;
        pthread_mutex_unlock(&map->mutex);
        return VSUM_OK;
    }
    empty->key = key_copy;
    empty->key_len = key_len;
    empty->value = value_copy;
    empty->value_len = value_len;
    empty->occupied = true;
    map->count++;
    pthread_mutex_unlock(&map->mutex);
    return VSUM_OK;
}

static vsum_status lookup(
    vsum_client *client,
    const uint8_t *key,
    uint64_t key_len,
    uint8_t *output,
    uint64_t output_capacity,
    uint64_t *output_length,
    bool remove) {
    if (client == NULL || output_length == NULL || (key == NULL && key_len != 0)) {
        return VSUM_INVALID;
    }
#ifdef VSUM_TEST_FIXED_LENGTH_ONLY
    if (key_len != client->map->max_key_size) {
        return VSUM_INVALID;
    }
#endif
    struct vsum_map *map = client->map;
    pthread_mutex_lock(&map->mutex);
    struct entry *entry = find_entry(map, key, key_len, NULL);
    if (entry == NULL) {
#ifdef VSUM_TEST_CLOBBER_MISSING
        if (output != NULL && output_capacity != 0) {
            memset(output, 0xff, (size_t)output_capacity);
        }
        *output_length = 0;
#endif
        pthread_mutex_unlock(&map->mutex);
        return VSUM_MISSING;
    }
    if (entry->value_len > output_capacity || (output == NULL && entry->value_len != 0)) {
#ifdef VSUM_TEST_REMOVE_ON_UNDERSIZED
        if (remove) {
            VSUM_FREE_BUFFER(entry->key);
            VSUM_FREE_BUFFER(entry->value);
            memset(entry, 0, sizeof(*entry));
            map->count--;
        }
#endif
        pthread_mutex_unlock(&map->mutex);
        return VSUM_INVALID;
    }
    if (entry->value_len != 0) {
        memcpy(output, entry->value, (size_t)entry->value_len);
    }
    *output_length = entry->value_len;
    if (remove) {
        VSUM_FREE_BUFFER(entry->key);
        VSUM_FREE_BUFFER(entry->value);
        memset(entry, 0, sizeof(*entry));
        map->count--;
    }
    pthread_mutex_unlock(&map->mutex);
    return VSUM_OK;
}

vsum_status vsum_try_get(
    vsum_client *client,
    const uint8_t *key,
    uint64_t key_len,
    uint8_t *output,
    uint64_t output_capacity,
    uint64_t *output_length) {
    return lookup(client, key, key_len, output, output_capacity, output_length, false);
}

vsum_status vsum_try_remove(
    vsum_client *client,
    const uint8_t *key,
    uint64_t key_len,
    uint8_t *output,
    uint64_t output_capacity,
    uint64_t *output_length) {
    return lookup(client, key, key_len, output, output_capacity, output_length, true);
}
