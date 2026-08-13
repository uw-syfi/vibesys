#![deny(unsafe_op_in_unsafe_fn)]

mod ffi;

use std::cmp::Ordering;
use std::collections::BinaryHeap;
use std::sync::{Arc, Mutex};

const ABI_VERSION: u32 = 1;
const STATUS_OK: u32 = 0;
const STATUS_FULL: u32 = 1;
const STATUS_EMPTY: u32 = 2;
const STATUS_INVALID: u32 = 3;
const STATUS_INTERNAL_ERROR: u32 = 4;

struct Entry {
    priority: u64,
    sequence: u64,
    value: Vec<u8>,
}

impl PartialEq for Entry {
    fn eq(&self, other: &Self) -> bool {
        self.priority == other.priority && self.sequence == other.sequence
    }
}

impl Eq for Entry {}

impl PartialOrd for Entry {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Entry {
    fn cmp(&self, other: &Self) -> Ordering {
        other
            .priority
            .cmp(&self.priority)
            .then_with(|| other.sequence.cmp(&self.sequence))
    }
}

struct Values {
    next_sequence: u64,
    heap: BinaryHeap<Entry>,
}

struct QueueState {
    capacity: usize,
    max_value_size: usize,
    values: Mutex<Values>,
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
        let mut heap = BinaryHeap::new();
        heap.try_reserve_exact(capacity)
            .map_err(|_| STATUS_INTERNAL_ERROR)?;
        Ok(Self {
            producer_count,
            consumer_count,
            state: Arc::new(QueueState {
                capacity,
                max_value_size,
                values: Mutex::new(Values {
                    next_sequence: 0,
                    heap,
                }),
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
    state: Arc<QueueState>,
}

impl Producer {
    fn try_enqueue(&self, priority: u64, data: &[u8]) -> u32 {
        if data.len() > self.state.max_value_size {
            return STATUS_INVALID;
        }
        let Ok(mut values) = self.state.values.lock() else {
            return STATUS_INTERNAL_ERROR;
        };
        if values.heap.len() == self.state.capacity {
            return STATUS_FULL;
        }
        let mut value = Vec::new();
        if value.try_reserve_exact(data.len()).is_err() {
            return STATUS_INTERNAL_ERROR;
        }
        value.extend_from_slice(data);
        let sequence = values.next_sequence;
        values.next_sequence = values.next_sequence.wrapping_add(1);
        values.heap.push(Entry {
            priority,
            sequence,
            value,
        });
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
        let Some(entry) = values.heap.peek() else {
            return Err(STATUS_EMPTY);
        };
        if entry.value.len() > output.len() {
            return Err(STATUS_INVALID);
        }
        output[..entry.value.len()].copy_from_slice(&entry.value);
        let length = entry.value.len();
        values.heap.pop();
        Ok(length)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn safe_core_is_bounded_priority_queue() {
        let queue = Queue::new(3, 8, 1, 1).unwrap();
        let producer = queue.producer(0).unwrap();
        let consumer = queue.consumer(0).unwrap();
        assert_eq!(producer.try_enqueue(7, b"later"), STATUS_OK);
        assert_eq!(producer.try_enqueue(2, b"first"), STATUS_OK);
        assert_eq!(producer.try_enqueue(2, b"second"), STATUS_OK);
        assert_eq!(producer.try_enqueue(0, b"full"), STATUS_FULL);

        let mut output = [0_u8; 8];
        for expected in [b"first".as_slice(), b"second", b"later"] {
            assert_eq!(consumer.try_dequeue(&mut output), Ok(expected.len()));
            assert_eq!(&output[..expected.len()], expected);
        }
        assert_eq!(consumer.try_dequeue(&mut output), Err(STATUS_EMPTY));
    }
}
