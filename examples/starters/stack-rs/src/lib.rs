#![deny(unsafe_op_in_unsafe_fn)]

mod ffi;

use std::sync::{Arc, Mutex};

const ABI_VERSION: u32 = 1;
const STATUS_OK: u32 = 0;
const STATUS_FULL: u32 = 1;
const STATUS_EMPTY: u32 = 2;
const STATUS_INVALID: u32 = 3;
const STATUS_INTERNAL_ERROR: u32 = 4;

struct StackState {
    capacity: usize,
    max_value_size: usize,
    values: Mutex<Vec<Vec<u8>>>,
}

pub struct Stack {
    producer_count: u32,
    consumer_count: u32,
    state: Arc<StackState>,
}

impl Stack {
    fn new(
        capacity: u64,
        max_value_size: u64,
        producer_count: u32,
        consumer_count: u32,
    ) -> Result<Self, u32> {
        if capacity == 0 || max_value_size == 0 || producer_count == 0 || consumer_count == 0 {
            return Err(STATUS_INVALID);
        }
        let capacity = usize::try_from(capacity).map_err(|_| STATUS_INVALID)?;
        let max_value_size = usize::try_from(max_value_size).map_err(|_| STATUS_INVALID)?;
        let mut values = Vec::new();
        values
            .try_reserve_exact(capacity)
            .map_err(|_| STATUS_INTERNAL_ERROR)?;
        Ok(Self {
            producer_count,
            consumer_count,
            state: Arc::new(StackState {
                capacity,
                max_value_size,
                values: Mutex::new(values),
            }),
        })
    }

    fn producer(&self, id: u32) -> Result<Producer, u32> {
        (id < self.producer_count)
            .then(|| Producer {
                state: Arc::clone(&self.state),
            })
            .ok_or(STATUS_INVALID)
    }

    fn consumer(&self, id: u32) -> Result<Consumer, u32> {
        (id < self.consumer_count)
            .then(|| Consumer {
                state: Arc::clone(&self.state),
            })
            .ok_or(STATUS_INVALID)
    }
}

pub struct Producer {
    state: Arc<StackState>,
}

impl Producer {
    fn try_push(&self, data: &[u8]) -> u32 {
        if data.len() > self.state.max_value_size {
            return STATUS_INVALID;
        }
        let Ok(mut values) = self.state.values.lock() else {
            return STATUS_INTERNAL_ERROR;
        };
        if values.len() == self.state.capacity {
            return STATUS_FULL;
        }
        let mut value = Vec::new();
        if value.try_reserve_exact(data.len()).is_err() {
            return STATUS_INTERNAL_ERROR;
        }
        value.extend_from_slice(data);
        values.push(value);
        STATUS_OK
    }
}

pub struct Consumer {
    state: Arc<StackState>,
}

impl Consumer {
    fn try_pop(&self, output: &mut [u8]) -> Result<usize, u32> {
        let Ok(mut values) = self.state.values.lock() else {
            return Err(STATUS_INTERNAL_ERROR);
        };
        let Some(value) = values.last() else {
            return Err(STATUS_EMPTY);
        };
        if value.len() > output.len() {
            return Err(STATUS_INVALID);
        }
        output[..value.len()].copy_from_slice(value);
        let length = value.len();
        values.pop();
        Ok(length)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn safe_core_is_bounded_stack() {
        let stack = Stack::new(3, 8, 1, 1).unwrap();
        let producer = stack.producer(0).unwrap();
        let consumer = stack.consumer(0).unwrap();
        assert_eq!(producer.try_push(b"A"), STATUS_OK);
        assert_eq!(producer.try_push(b"B"), STATUS_OK);
        assert_eq!(producer.try_push(b"C"), STATUS_OK);
        assert_eq!(producer.try_push(b"D"), STATUS_FULL);

        let mut output = [0_u8; 8];
        for expected in [b"C".as_slice(), b"B", b"A"] {
            assert_eq!(consumer.try_pop(&mut output), Ok(expected.len()));
            assert_eq!(&output[..expected.len()], expected);
        }
        assert_eq!(consumer.try_pop(&mut output), Err(STATUS_EMPTY));
    }
}
