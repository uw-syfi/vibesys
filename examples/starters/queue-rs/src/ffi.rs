// Pointer validity and lifecycle requirements are defined by CANDIDATE_CONTRACT.md.
#![allow(clippy::missing_safety_doc)]

use super::*;
use std::slice;

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
    if queue_out.is_null() {
        return STATUS_INVALID;
    }
    let queue = match Queue::new(capacity, max_value_size, producer_count, consumer_count) {
        Ok(queue) => queue,
        Err(status) => return status,
    };

    unsafe { queue_out.write(Box::into_raw(Box::new(queue))) };
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
    let Some(queue) = (unsafe { queue.as_ref() }) else {
        return STATUS_INVALID;
    };
    if producer_out.is_null() {
        return STATUS_INVALID;
    }
    let producer = match queue.producer(producer_id) {
        Ok(producer) => producer,
        Err(status) => return status,
    };

    unsafe { producer_out.write(Box::into_raw(Box::new(producer))) };
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
    let Some(queue) = (unsafe { queue.as_ref() }) else {
        return STATUS_INVALID;
    };
    if consumer_out.is_null() {
        return STATUS_INVALID;
    }
    let consumer = match queue.consumer(consumer_id) {
        Ok(consumer) => consumer,
        Err(status) => return status,
    };

    unsafe { consumer_out.write(Box::into_raw(Box::new(consumer))) };
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
    let Some(producer) = (unsafe { producer.as_ref() }) else {
        return STATUS_INVALID;
    };
    let Ok(length) = usize::try_from(length) else {
        return STATUS_INVALID;
    };
    let data = if length == 0 {
        &[]
    } else {
        if data.is_null() {
            return STATUS_INVALID;
        }
        unsafe { slice::from_raw_parts(data, length) }
    };

    producer.try_enqueue(data)
}

#[no_mangle]
pub unsafe extern "C" fn vsq_try_dequeue(
    consumer: *mut Consumer,
    output: *mut u8,
    output_capacity: u64,
    output_length: *mut u64,
) -> u32 {
    let Some(consumer) = (unsafe { consumer.as_ref() }) else {
        return STATUS_INVALID;
    };
    if output_length.is_null() {
        return STATUS_INVALID;
    }
    let Ok(output_capacity) = usize::try_from(output_capacity) else {
        return STATUS_INVALID;
    };
    let output = if output_capacity == 0 {
        &mut []
    } else {
        if output.is_null() {
            return STATUS_INVALID;
        }
        unsafe { slice::from_raw_parts_mut(output, output_capacity) }
    };

    match consumer.try_dequeue(output) {
        Ok(length) => {
            unsafe { output_length.write(length as u64) };
            STATUS_OK
        }
        Err(status) => status,
    }
}
