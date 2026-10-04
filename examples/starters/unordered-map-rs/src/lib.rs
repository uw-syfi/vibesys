#![deny(unsafe_op_in_unsafe_fn)]

mod ffi;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

const ABI_VERSION: u32 = 1;
const STATUS_OK: u32 = 0;
const STATUS_MISSING: u32 = 1;
const STATUS_INVALID: u32 = 2;
const STATUS_INTERNAL_ERROR: u32 = 3;

struct MapState {
    max_key_size: usize,
    max_value_size: usize,
    values: Mutex<HashMap<Vec<u8>, Vec<u8>>>,
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
                values: Mutex::new(HashMap::new()),
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
        let Ok(mut values) = self.state.values.lock() else {
            return STATUS_INTERNAL_ERROR;
        };
        values.insert(key.to_vec(), value.to_vec());
        STATUS_OK
    }

    fn try_get(&self, key: &[u8], output: &mut [u8]) -> Result<usize, u32> {
        if key.len() > self.state.max_key_size {
            return Err(STATUS_INVALID);
        }
        let Ok(values) = self.state.values.lock() else {
            return Err(STATUS_INTERNAL_ERROR);
        };
        let Some(value) = values.get(key) else {
            return Err(STATUS_MISSING);
        };
        if value.len() > output.len() {
            return Err(STATUS_INVALID);
        }
        output[..value.len()].copy_from_slice(value);
        Ok(value.len())
    }

    fn try_remove(&self, key: &[u8], output: &mut [u8]) -> Result<usize, u32> {
        if key.len() > self.state.max_key_size {
            return Err(STATUS_INVALID);
        }
        let Ok(mut values) = self.state.values.lock() else {
            return Err(STATUS_INTERNAL_ERROR);
        };
        let Some(value) = values.get(key) else {
            return Err(STATUS_MISSING);
        };
        if value.len() > output.len() {
            return Err(STATUS_INVALID);
        }
        output[..value.len()].copy_from_slice(value);
        let length = value.len();
        values.remove(key);
        Ok(length)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn safe_core_put_get_replace_remove() {
        let map = Map::new(8, 8, 1).unwrap();
        let client = map.client(0).unwrap();
        let mut output = [0_u8; 8];

        assert_eq!(client.try_put(b"k1", b"v1"), STATUS_OK);
        assert_eq!(client.try_get(b"k1", &mut output), Ok(2));
        assert_eq!(&output[..2], b"v1");

        assert_eq!(client.try_put(b"k1", b"v2"), STATUS_OK);
        assert_eq!(client.try_get(b"k1", &mut output), Ok(2));
        assert_eq!(&output[..2], b"v2");

        assert_eq!(client.try_remove(b"k1", &mut output), Ok(2));
        assert_eq!(&output[..2], b"v2");
        output.fill(0xaa);
        assert_eq!(client.try_get(b"k1", &mut output), Err(STATUS_MISSING));
        assert!(output.iter().all(|byte| *byte == 0xaa));
        assert_eq!(client.try_remove(b"k1", &mut output), Err(STATUS_MISSING));
        assert!(output.iter().all(|byte| *byte == 0xaa));
    }
}
