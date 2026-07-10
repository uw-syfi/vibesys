// Pointer validity and lifecycle requirements are defined by CANDIDATE_CONTRACT.md.
#![allow(clippy::missing_safety_doc)]

use std::collections::VecDeque;
use std::ptr;
use std::slice;
use std::sync::Mutex;

const ABI_VERSION: u32 = 1;
const STATUS_OK: u32 = 0;
const STATUS_FULL: u32 = 1;
const STATUS_EMPTY: u32 = 2;
const STATUS_INVALID: u32 = 3;
const STATUS_INTERNAL_ERROR: u32 = 4;

pub struct Queue {
    capacity: usize,
    max_value_size: usize,
    producer_count: u32,
    consumer_count: u32,
    values: Mutex<VecDeque<Vec<u8>>>,
}

pub struct Producer {
    queue: *mut Queue,
}

pub struct Consumer {
    queue: *mut Queue,
}

#[no_mangle]
pub extern "C" fn vsq_abi_version() -> u32 {
    ABI_VERSION
}

#[no_mangle]
pub unsafe extern "C" fn vsq_queue_create(
    capacity: u64,
    max_value_size: u64,
    producer_count: u32,
    consumer_count: u32,
    queue_out: *mut *mut Queue,
) -> u32 {
    if capacity == 0
        || max_value_size == 0
        || producer_count == 0
        || consumer_count == 0
        || queue_out.is_null()
    {
        return STATUS_INVALID;
    }

    let Ok(capacity) = usize::try_from(capacity) else {
        return STATUS_INVALID;
    };
    let Ok(max_value_size) = usize::try_from(max_value_size) else {
        return STATUS_INVALID;
    };

    let mut values = VecDeque::new();
    if values.try_reserve_exact(capacity).is_err() {
        return STATUS_INTERNAL_ERROR;
    }
    let queue = Box::new(Queue {
        capacity,
        max_value_size,
        producer_count,
        consumer_count,
        values: Mutex::new(values),
    });
    unsafe { *queue_out = Box::into_raw(queue) };
    STATUS_OK
}

#[no_mangle]
pub unsafe extern "C" fn vsq_queue_destroy(queue: *mut Queue) {
    if !queue.is_null() {
        drop(unsafe { Box::from_raw(queue) });
    }
}

#[no_mangle]
pub unsafe extern "C" fn vsq_producer_create(
    queue: *mut Queue,
    producer_id: u32,
    producer_out: *mut *mut Producer,
) -> u32 {
    if queue.is_null()
        || producer_out.is_null()
        || producer_id >= unsafe { (*queue).producer_count }
    {
        return STATUS_INVALID;
    }
    let producer = Box::new(Producer { queue });
    unsafe { *producer_out = Box::into_raw(producer) };
    STATUS_OK
}

#[no_mangle]
pub unsafe extern "C" fn vsq_producer_destroy(producer: *mut Producer) {
    if !producer.is_null() {
        drop(unsafe { Box::from_raw(producer) });
    }
}

#[no_mangle]
pub unsafe extern "C" fn vsq_consumer_create(
    queue: *mut Queue,
    consumer_id: u32,
    consumer_out: *mut *mut Consumer,
) -> u32 {
    if queue.is_null()
        || consumer_out.is_null()
        || consumer_id >= unsafe { (*queue).consumer_count }
    {
        return STATUS_INVALID;
    }
    let consumer = Box::new(Consumer { queue });
    unsafe { *consumer_out = Box::into_raw(consumer) };
    STATUS_OK
}

#[no_mangle]
pub unsafe extern "C" fn vsq_consumer_destroy(consumer: *mut Consumer) {
    if !consumer.is_null() {
        drop(unsafe { Box::from_raw(consumer) });
    }
}

#[no_mangle]
pub unsafe extern "C" fn vsq_try_enqueue(
    producer: *mut Producer,
    data: *const u8,
    length: u64,
) -> u32 {
    if producer.is_null() || (data.is_null() && length != 0) {
        return STATUS_INVALID;
    }
    let queue = unsafe { (*producer).queue };
    if queue.is_null() {
        return STATUS_INVALID;
    }
    let Ok(length) = usize::try_from(length) else {
        return STATUS_INVALID;
    };
    if length > unsafe { (*queue).max_value_size } {
        return STATUS_INVALID;
    }

    let Ok(mut values) = (unsafe { (*queue).values.lock() }) else {
        return STATUS_INTERNAL_ERROR;
    };
    if values.len() == unsafe { (*queue).capacity } {
        return STATUS_FULL;
    }

    let mut value = Vec::new();
    if value.try_reserve_exact(length).is_err() {
        return STATUS_INTERNAL_ERROR;
    }
    if length != 0 {
        value.extend_from_slice(unsafe { slice::from_raw_parts(data, length) });
    }
    values.push_back(value);
    STATUS_OK
}

#[no_mangle]
pub unsafe extern "C" fn vsq_try_dequeue(
    consumer: *mut Consumer,
    output: *mut u8,
    output_capacity: u64,
    output_length: *mut u64,
) -> u32 {
    if consumer.is_null() || output_length.is_null() {
        return STATUS_INVALID;
    }
    let queue = unsafe { (*consumer).queue };
    if queue.is_null() {
        return STATUS_INVALID;
    }
    let Ok(output_capacity) = usize::try_from(output_capacity) else {
        return STATUS_INVALID;
    };

    let Ok(mut values) = (unsafe { (*queue).values.lock() }) else {
        return STATUS_INTERNAL_ERROR;
    };
    let Some(value) = values.front() else {
        return STATUS_EMPTY;
    };
    if value.len() > output_capacity || (output.is_null() && !value.is_empty()) {
        return STATUS_INVALID;
    }
    if !value.is_empty() {
        unsafe { ptr::copy_nonoverlapping(value.as_ptr(), output, value.len()) };
    }
    unsafe { *output_length = value.len() as u64 };
    values.pop_front();
    STATUS_OK
}
