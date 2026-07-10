#![deny(unsafe_op_in_unsafe_fn)]

mod ffi;

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

const ABI_VERSION: u32 = 1;
const STATUS_OK: u32 = 0;
const STATUS_FULL: u32 = 1;
const STATUS_EMPTY: u32 = 2;
const STATUS_INVALID: u32 = 3;
const STATUS_INTERNAL_ERROR: u32 = 4;

struct QueueState {
    capacity: usize,
    max_value_size: usize,
    values: Mutex<VecDeque<Vec<u8>>>,
}

pub struct Queue {
    producer_count: u32,
    consumer_count: u32,
    state: Arc<QueueState>,
}

impl Queue {
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
        let mut values = VecDeque::new();
        values
            .try_reserve_exact(capacity)
            .map_err(|_| STATUS_INTERNAL_ERROR)?;

        Ok(Self {
            producer_count,
            consumer_count,
            state: Arc::new(QueueState {
                capacity,
                max_value_size,
                values: Mutex::new(values),
            }),
        })
    }

    fn producer(&self, id: u32) -> Result<Producer, u32> {
        if id >= self.producer_count {
            return Err(STATUS_INVALID);
        }
        Ok(Producer {
            state: Arc::clone(&self.state),
        })
    }

    fn consumer(&self, id: u32) -> Result<Consumer, u32> {
        if id >= self.consumer_count {
            return Err(STATUS_INVALID);
        }
        Ok(Consumer {
            state: Arc::clone(&self.state),
        })
    }
}

pub struct Producer {
    state: Arc<QueueState>,
}

impl Producer {
    fn try_enqueue(&self, data: &[u8]) -> u32 {
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
        values.push_back(value);
        STATUS_OK
    }
}

pub struct Consumer {
    state: Arc<QueueState>,
}

impl Consumer {
    fn try_dequeue(&self, output: &mut [u8]) -> Result<usize, u32> {
        let Ok(mut values) = self.state.values.lock() else {
            return Err(STATUS_INTERNAL_ERROR);
        };
        let Some(value) = values.front() else {
            return Err(STATUS_EMPTY);
        };
        if value.len() > output.len() {
            return Err(STATUS_INVALID);
        }

        output[..value.len()].copy_from_slice(value);
        let length = value.len();
        values.pop_front();
        Ok(length)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn safe_core_is_bounded_fifo() {
        let queue = Queue::new(2, 8, 1, 1).unwrap();
        let producer = queue.producer(0).unwrap();
        let consumer = queue.consumer(0).unwrap();

        assert_eq!(producer.try_enqueue(b"first"), STATUS_OK);
        assert_eq!(producer.try_enqueue(b"second"), STATUS_OK);
        assert_eq!(producer.try_enqueue(b"third"), STATUS_FULL);

        let mut output = [0_u8; 8];
        assert_eq!(consumer.try_dequeue(&mut output), Ok(5));
        assert_eq!(&output[..5], b"first");
        assert_eq!(consumer.try_dequeue(&mut output), Ok(6));
        assert_eq!(&output[..6], b"second");
        assert_eq!(consumer.try_dequeue(&mut output), Err(STATUS_EMPTY));
    }

    #[test]
    fn failed_dequeue_leaves_value_and_output_unchanged() {
        let queue = Queue::new(1, 8, 1, 1).unwrap();
        let producer = queue.producer(0).unwrap();
        let consumer = queue.consumer(0).unwrap();
        assert_eq!(producer.try_enqueue(b"value"), STATUS_OK);

        let mut short = [0xA5_u8; 4];
        assert_eq!(consumer.try_dequeue(&mut short), Err(STATUS_INVALID));
        assert_eq!(short, [0xA5; 4]);

        let mut output = [0_u8; 8];
        assert_eq!(consumer.try_dequeue(&mut output), Ok(5));
        assert_eq!(&output[..5], b"value");
    }
}
