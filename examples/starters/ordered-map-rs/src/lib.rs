#![deny(unsafe_op_in_unsafe_fn)]

mod ffi;

use std::collections::BTreeMap;
use std::ops::Bound::{Excluded, Included, Unbounded};
use std::sync::{Arc, Mutex};

const ABI_VERSION: u32 = 1;
const STATUS_OK: u32 = 0;
const STATUS_MISSING: u32 = 1;
const STATUS_INVALID: u32 = 2;
const STATUS_INTERNAL_ERROR: u32 = 3;

struct MapState {
    max_key_size: usize,
    max_value_size: usize,
    entries: Mutex<BTreeMap<Vec<u8>, Vec<u8>>>,
}

pub struct Map {
    client_count: u32,
    state: Arc<MapState>,
}

impl Map {
    fn new(max_key_size: u64, max_value_size: u64, client_count: u32) -> Result<Self, u32> {
        if max_key_size == 0 || max_value_size == 0 || client_count == 0 {
            return Err(STATUS_INVALID);
        }
        let max_key_size = usize::try_from(max_key_size).map_err(|_| STATUS_INVALID)?;
        let max_value_size = usize::try_from(max_value_size).map_err(|_| STATUS_INVALID)?;
        Ok(Self {
            client_count,
            state: Arc::new(MapState {
                max_key_size,
                max_value_size,
                entries: Mutex::new(BTreeMap::new()),
            }),
        })
    }

    fn client(&self, id: u32) -> Result<Client, u32> {
        (id < self.client_count)
            .then(|| Client {
                state: Arc::clone(&self.state),
            })
            .ok_or(STATUS_INVALID)
    }
}

pub struct Client {
    state: Arc<MapState>,
}

impl Client {
    fn try_put(&self, key: &[u8], value: &[u8]) -> u32 {
        if key.len() > self.state.max_key_size || value.len() > self.state.max_value_size {
            return STATUS_INVALID;
        }
        let Ok(mut entries) = self.state.entries.lock() else {
            return STATUS_INTERNAL_ERROR;
        };
        let mut stored_key = Vec::new();
        if stored_key.try_reserve_exact(key.len()).is_err() {
            return STATUS_INTERNAL_ERROR;
        }
        stored_key.extend_from_slice(key);
        let mut stored_value = Vec::new();
        if stored_value.try_reserve_exact(value.len()).is_err() {
            return STATUS_INTERNAL_ERROR;
        }
        stored_value.extend_from_slice(value);
        entries.insert(stored_key, stored_value);
        STATUS_OK
    }

    fn try_get(&self, key: &[u8], value_out: &mut [u8]) -> Result<usize, u32> {
        if key.len() > self.state.max_key_size {
            return Err(STATUS_INVALID);
        }
        lookup_copy(&self.state.entries, key, value_out, false)
    }

    fn try_remove(&self, key: &[u8], value_out: &mut [u8]) -> Result<usize, u32> {
        if key.len() > self.state.max_key_size {
            return Err(STATUS_INVALID);
        }
        lookup_copy(&self.state.entries, key, value_out, true)
    }

    fn try_min(&self, key_out: &mut [u8], val_out: &mut [u8]) -> Result<(usize, usize), u32> {
        copy_endpoint(&self.state.entries, key_out, val_out, true)
    }

    fn try_max(&self, key_out: &mut [u8], val_out: &mut [u8]) -> Result<(usize, usize), u32> {
        copy_endpoint(&self.state.entries, key_out, val_out, false)
    }

    fn try_predecessor(
        &self,
        key: &[u8],
        key_out: &mut [u8],
        val_out: &mut [u8],
    ) -> Result<(usize, usize), u32> {
        if key.len() > self.state.max_key_size {
            return Err(STATUS_INVALID);
        }
        neighbor_copy(&self.state.entries, key, key_out, val_out, true)
    }

    fn try_successor(
        &self,
        key: &[u8],
        key_out: &mut [u8],
        val_out: &mut [u8],
    ) -> Result<(usize, usize), u32> {
        if key.len() > self.state.max_key_size {
            return Err(STATUS_INVALID);
        }
        neighbor_copy(&self.state.entries, key, key_out, val_out, false)
    }

    #[allow(clippy::too_many_arguments)]
    fn try_range(
        &self,
        start: &[u8],
        end: &[u8],
        keys_out: &mut [u8],
        key_stride: usize,
        vals_out: &mut [u8],
        val_stride: usize,
        lengths_key: &mut [u64],
        lengths_val: &mut [u64],
        max_items: usize,
    ) -> Result<(u64, u64), u32> {
        if start.len() > self.state.max_key_size || end.len() > self.state.max_key_size {
            return Err(STATUS_INVALID);
        }
        let Ok(entries) = self.state.entries.lock() else {
            return Err(STATUS_INTERNAL_ERROR);
        };
        if start >= end {
            return Ok((0, 0));
        }
        let matched: Vec<(&Vec<u8>, &Vec<u8>)> = entries
            .range::<[u8], _>((Included(start), Excluded(end)))
            .collect();
        if max_items == 0 {
            return Ok((0, u64::from(!matched.is_empty())));
        }
        let remaining = u64::from(matched.len() > max_items);
        let take = matched.len().min(max_items);
        for (key, value) in &matched[..take] {
            if key.len() > key_stride || value.len() > val_stride {
                return Err(STATUS_INVALID);
            }
        }
        for (index, (key, value)) in matched[..take].iter().enumerate() {
            let key_offset = index * key_stride;
            let val_offset = index * val_stride;
            keys_out[key_offset..key_offset + key.len()].copy_from_slice(key);
            vals_out[val_offset..val_offset + value.len()].copy_from_slice(value);
            lengths_key[index] = key.len() as u64;
            lengths_val[index] = value.len() as u64;
        }
        Ok((take as u64, remaining))
    }
}

fn lookup_copy(
    lock: &Mutex<BTreeMap<Vec<u8>, Vec<u8>>>,
    key: &[u8],
    value_out: &mut [u8],
    remove: bool,
) -> Result<usize, u32> {
    let Ok(mut entries) = lock.lock() else {
        return Err(STATUS_INTERNAL_ERROR);
    };
    let Some(value) = entries.get(key) else {
        return Err(STATUS_MISSING);
    };
    if value.len() > value_out.len() {
        return Err(STATUS_INVALID);
    }
    value_out[..value.len()].copy_from_slice(value);
    let length = value.len();
    if remove {
        entries.remove(key);
    }
    Ok(length)
}

fn copy_endpoint(
    lock: &Mutex<BTreeMap<Vec<u8>, Vec<u8>>>,
    key_out: &mut [u8],
    val_out: &mut [u8],
    min: bool,
) -> Result<(usize, usize), u32> {
    let Ok(entries) = lock.lock() else {
        return Err(STATUS_INTERNAL_ERROR);
    };
    let Some((key, value)) = (if min {
        entries.iter().next()
    } else {
        entries.iter().next_back()
    }) else {
        return Err(STATUS_MISSING);
    };
    copy_pair(key, value, key_out, val_out)
}

fn neighbor_copy(
    lock: &Mutex<BTreeMap<Vec<u8>, Vec<u8>>>,
    key: &[u8],
    key_out: &mut [u8],
    val_out: &mut [u8],
    predecessor: bool,
) -> Result<(usize, usize), u32> {
    let Ok(entries) = lock.lock() else {
        return Err(STATUS_INTERNAL_ERROR);
    };
    let pair = if predecessor {
        entries
            .range::<[u8], _>((Unbounded, Excluded(key)))
            .next_back()
    } else {
        entries.range::<[u8], _>((Excluded(key), Unbounded)).next()
    };
    let Some((found_key, value)) = pair else {
        return Err(STATUS_MISSING);
    };
    copy_pair(found_key, value, key_out, val_out)
}

fn copy_pair(
    key: &[u8],
    value: &[u8],
    key_out: &mut [u8],
    val_out: &mut [u8],
) -> Result<(usize, usize), u32> {
    if key.len() > key_out.len() || value.len() > val_out.len() {
        return Err(STATUS_INVALID);
    }
    key_out[..key.len()].copy_from_slice(key);
    val_out[..value.len()].copy_from_slice(value);
    Ok((key.len(), value.len()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pair(client: &Client, key: &[u8], value: &[u8]) {
        assert_eq!(client.try_put(key, value), STATUS_OK);
    }

    #[test]
    fn point_ops_overwrite_and_remove() {
        let map = Map::new(8, 8, 1).unwrap();
        let client = map.client(0).unwrap();
        pair(&client, b"a", b"one");
        pair(&client, b"a", b"two");
        let mut output = [0_u8; 8];
        assert_eq!(client.try_get(b"a", &mut output), Ok(3));
        assert_eq!(&output[..3], b"two");
        assert_eq!(client.try_remove(b"a", &mut output), Ok(3));
        assert_eq!(&output[..3], b"two");
        assert_eq!(client.try_get(b"a", &mut output), Err(STATUS_MISSING));
    }

    #[test]
    fn min_max_predecessor_successor() {
        let map = Map::new(8, 8, 1).unwrap();
        let client = map.client(0).unwrap();
        let mut key_out = [0_u8; 8];
        let mut val_out = [0_u8; 8];
        assert_eq!(
            client.try_min(&mut key_out, &mut val_out),
            Err(STATUS_MISSING)
        );
        pair(&client, b"b", b"2");
        pair(&client, b"a", b"1");
        pair(&client, b"d", b"4");
        assert_eq!(client.try_min(&mut key_out, &mut val_out), Ok((1, 1)));
        assert_eq!(&key_out[..1], b"a");
        assert_eq!(client.try_max(&mut key_out, &mut val_out), Ok((1, 1)));
        assert_eq!(&key_out[..1], b"d");
        assert_eq!(
            client.try_predecessor(b"d", &mut key_out, &mut val_out),
            Ok((1, 1))
        );
        assert_eq!(&key_out[..1], b"b");
        assert_eq!(
            client.try_successor(b"a", &mut key_out, &mut val_out),
            Ok((1, 1))
        );
        assert_eq!(&key_out[..1], b"b");
        assert_eq!(
            client.try_predecessor(b"a", &mut key_out, &mut val_out),
            Err(STATUS_MISSING)
        );
        assert_eq!(
            client.try_successor(b"d", &mut key_out, &mut val_out),
            Err(STATUS_MISSING)
        );
    }

    #[test]
    fn range_truncates_with_remaining() {
        let map = Map::new(8, 8, 1).unwrap();
        let client = map.client(0).unwrap();
        pair(&client, b"a", b"1");
        pair(&client, b"b", b"2");
        pair(&client, b"c", b"3");
        let mut keys = [0_u8; 16];
        let mut vals = [0_u8; 16];
        let mut key_lens = [u64::MAX; 2];
        let mut val_lens = [u64::MAX; 2];
        let (count, remaining) = client
            .try_range(
                b"a",
                b"z",
                &mut keys,
                8,
                &mut vals,
                8,
                &mut key_lens,
                &mut val_lens,
                2,
            )
            .unwrap();
        assert_eq!(count, 2);
        assert_eq!(remaining, 1);
        assert_eq!(&keys[..1], b"a");
        assert_eq!(&keys[8..9], b"b");
        assert_eq!(key_lens, [1, 1]);
        assert_eq!(val_lens, [1, 1]);
    }

    #[test]
    fn range_empty_for_reversed_and_equal_bounds() {
        let map = Map::new(8, 8, 1).unwrap();
        let client = map.client(0).unwrap();
        pair(&client, b"a", b"1");
        pair(&client, b"c", b"3");
        let mut keys = [0_u8; 16];
        let mut vals = [0_u8; 16];
        let mut key_lens = [u64::MAX; 2];
        let mut val_lens = [u64::MAX; 2];
        let (count, remaining) = client
            .try_range(
                b"c",
                b"a",
                &mut keys,
                8,
                &mut vals,
                8,
                &mut key_lens,
                &mut val_lens,
                2,
            )
            .unwrap();
        assert_eq!((count, remaining), (0, 0));
        let (count, remaining) = client
            .try_range(
                b"b",
                b"b",
                &mut keys,
                8,
                &mut vals,
                8,
                &mut key_lens,
                &mut val_lens,
                2,
            )
            .unwrap();
        assert_eq!((count, remaining), (0, 0));
    }

    #[test]
    fn oversized_query_keys_are_invalid() {
        let map = Map::new(1, 8, 1).unwrap();
        let client = map.client(0).unwrap();
        pair(&client, b"a", b"1");
        let mut output = [0_u8; 8];
        assert_eq!(client.try_get(b"aa", &mut output), Err(STATUS_INVALID));
        assert_eq!(client.try_remove(b"aa", &mut output), Err(STATUS_INVALID));
        let mut key_out = [0_u8; 8];
        let mut val_out = [0_u8; 8];
        assert_eq!(
            client.try_predecessor(b"\xff\xff", &mut key_out, &mut val_out),
            Err(STATUS_INVALID)
        );
        assert_eq!(
            client.try_successor(b"\x00\x00", &mut key_out, &mut val_out),
            Err(STATUS_INVALID)
        );
        let mut keys = [0_u8; 16];
        let mut vals = [0_u8; 16];
        let mut key_lens = [u64::MAX; 2];
        let mut val_lens = [u64::MAX; 2];
        assert_eq!(
            client.try_range(
                b"aa",
                b"z",
                &mut keys,
                8,
                &mut vals,
                8,
                &mut key_lens,
                &mut val_lens,
                2,
            ),
            Err(STATUS_INVALID)
        );
        assert_eq!(
            client.try_range(
                b"a",
                b"zz",
                &mut keys,
                8,
                &mut vals,
                8,
                &mut key_lens,
                &mut val_lens,
                2,
            ),
            Err(STATUS_INVALID)
        );
        assert_eq!(client.try_get(b"a", &mut output), Ok(1));
        assert_eq!(&output[..1], b"1");
    }
}
