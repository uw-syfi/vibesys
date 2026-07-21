use crate::abi::{
    Api, STATUS_EMPTY, STATUS_FULL, STATUS_INTERNAL_ERROR, STATUS_INVALID, STATUS_OK,
};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;

const SENTINEL_BYTE: u8 = 0xa7;
const SENTINEL_LENGTH: u64 = u64::MAX;
const CONCURRENT_PROBE_ITEMS: usize = 2_000;

pub fn run_probe(
    api: Api,
    capacity: u64,
    max_value_size: usize,
    producer_count: u32,
    consumer_count: u32,
) -> Result<(), String> {
    let queue = api.create_queue(
        capacity,
        max_value_size as u64,
        producer_count,
        consumer_count,
    )?;
    let mut producers = (0..producer_count)
        .map(|id| queue.create_producer(id))
        .collect::<Result<Vec<_>, _>>()?;
    let mut consumers = (0..consumer_count)
        .map(|id| queue.create_consumer(id))
        .collect::<Result<Vec<_>, _>>()?;
    let producer = producers
        .first_mut()
        .ok_or_else(|| "ABI probe requires a producer".to_string())?;
    let consumer = consumers
        .first_mut()
        .ok_or_else(|| "ABI probe requires a consumer".to_string())?;

    check_empty_output_is_unchanged(consumer, max_value_size)?;

    for (tag, length) in probe_lengths(max_value_size).into_iter().enumerate() {
        let mut input = payload(length, tag as u8);
        let expected = input.clone();
        require_status(
            producer.enqueue(&input),
            STATUS_OK,
            &format!("enqueue length {length}"),
        )?;
        input.fill(0x5a);

        let mut output = vec![SENTINEL_BYTE; max_value_size.max(1)];
        let mut output_length = SENTINEL_LENGTH;
        let status =
            consumer.dequeue_raw(output.as_mut_ptr(), output.len() as u64, &mut output_length);
        require_status(status, STATUS_OK, &format!("dequeue length {length}"))?;
        if output_length != length as u64 {
            return Err(format!(
                "dequeue length {length} reported output length {output_length}"
            ));
        }
        if output[..length] != expected {
            return Err(format!("dequeue length {length} returned corrupted bytes"));
        }
        if output[length..].iter().any(|byte| *byte != SENTINEL_BYTE) {
            return Err(format!(
                "dequeue length {length} wrote beyond the returned value"
            ));
        }
    }

    check_invalid_output_retains_value(producer, consumer, max_value_size)?;
    check_empty_output_is_unchanged(consumer, max_value_size)?;
    if consumer_count >= 2 {
        check_concurrent_undersized_dequeues(&api, producer_count, consumer_count)?;
    }
    Ok(())
}

fn concurrent_payload(sequence: usize) -> Vec<u8> {
    let length = if sequence % 2 == 0 { 16 } else { 64 };
    let mut value = vec![0_u8; length];
    value[..8].copy_from_slice(&(sequence as u64).to_le_bytes());
    for (index, byte) in value[8..].iter_mut().enumerate() {
        *byte = (sequence as u8)
            .wrapping_mul(31)
            .wrapping_add((index as u8).wrapping_mul(17))
            .wrapping_add(0x6d);
    }
    value
}

fn validate_concurrent_payload(value: &[u8]) -> Result<usize, String> {
    if value.len() != 16 && value.len() != 64 {
        return Err(format!(
            "concurrent probe returned {} bytes, expected 16 or 64",
            value.len()
        ));
    }
    let sequence = u64::from_le_bytes(value[..8].try_into().expect("sequence prefix")) as usize;
    if sequence >= CONCURRENT_PROBE_ITEMS || concurrent_payload(sequence) != value {
        return Err("concurrent probe returned a fabricated or corrupted value".to_string());
    }
    Ok(sequence)
}

fn record_concurrent_value(
    seen: &Mutex<Vec<bool>>,
    consumed: &AtomicUsize,
    value: &[u8],
) -> Result<(), String> {
    let sequence = validate_concurrent_payload(value)?;
    let mut observed = seen
        .lock()
        .map_err(|_| "concurrent probe observation lock was poisoned".to_string())?;
    if observed[sequence] {
        return Err(format!(
            "concurrent probe returned sequence {sequence} more than once"
        ));
    }
    observed[sequence] = true;
    consumed.fetch_add(1, Ordering::Release);
    Ok(())
}

fn check_concurrent_undersized_dequeues(
    api: &Api,
    producer_count: u32,
    consumer_count: u32,
) -> Result<(), String> {
    let queue = api.create_queue(8, 64, producer_count, consumer_count)?;
    let producer = queue.create_producer(0)?;
    let short_consumer = queue.create_consumer(0)?;
    let full_consumer = queue.create_consumer(1)?;
    let seen = Arc::new(Mutex::new(vec![false; CONCURRENT_PROBE_ITEMS]));
    let consumed = Arc::new(AtomicUsize::new(0));

    thread::scope(|scope| {
        let producer_worker = scope.spawn(move || {
            let mut producer = producer;
            for sequence in 0..CONCURRENT_PROBE_ITEMS {
                let value = concurrent_payload(sequence);
                loop {
                    match producer.enqueue(&value) {
                        STATUS_OK => break,
                        STATUS_FULL => thread::yield_now(),
                        status => {
                            return Err(format!(
                                "concurrent probe enqueue returned ABI status {status}"
                            ));
                        }
                    }
                }
            }
            Ok(())
        });

        let short_seen = seen.clone();
        let short_consumed = consumed.clone();
        let short_worker = scope.spawn(move || {
            let mut consumer = short_consumer;
            let mut output = [SENTINEL_BYTE; 32];
            while short_consumed.load(Ordering::Acquire) < CONCURRENT_PROBE_ITEMS {
                output.fill(SENTINEL_BYTE);
                let unchanged = output;
                let mut output_length = SENTINEL_LENGTH;
                let status = consumer.dequeue_raw(
                    output.as_mut_ptr(),
                    output.len() as u64,
                    &mut output_length,
                );
                match status {
                    STATUS_OK => {
                        if output_length != 16 {
                            return Err(format!(
                                "short concurrent dequeue returned length {output_length}"
                            ));
                        }
                        record_concurrent_value(
                            &short_seen,
                            &short_consumed,
                            &output[..output_length as usize],
                        )?;
                    }
                    STATUS_INVALID | STATUS_EMPTY => {
                        if output != unchanged || output_length != SENTINEL_LENGTH {
                            return Err(
                                "failed concurrent dequeue modified caller output".to_string()
                            );
                        }
                        thread::yield_now();
                    }
                    STATUS_INTERNAL_ERROR => {
                        return Err(
                            "concurrent probe candidate reported internal error".to_string()
                        );
                    }
                    status => {
                        return Err(format!(
                            "short concurrent dequeue returned ABI status {status}"
                        ));
                    }
                }
            }
            Ok(())
        });

        let full_seen = seen.clone();
        let full_consumed = consumed.clone();
        let full_worker = scope.spawn(move || {
            let mut consumer = full_consumer;
            let mut output = [0_u8; 64];
            while full_consumed.load(Ordering::Acquire) < CONCURRENT_PROBE_ITEMS {
                let (status, length) = consumer.dequeue(&mut output)?;
                match status {
                    STATUS_OK => {
                        record_concurrent_value(&full_seen, &full_consumed, &output[..length])?
                    }
                    STATUS_EMPTY => thread::yield_now(),
                    status => {
                        return Err(format!(
                            "full concurrent dequeue returned ABI status {status}"
                        ));
                    }
                }
            }
            Ok(())
        });

        producer_worker
            .join()
            .map_err(|_| "concurrent probe producer panicked".to_string())??;
        short_worker
            .join()
            .map_err(|_| "concurrent probe short consumer panicked".to_string())??;
        full_worker
            .join()
            .map_err(|_| "concurrent probe full consumer panicked".to_string())??;
        Ok::<(), String>(())
    })?;

    let observed = seen
        .lock()
        .map_err(|_| "concurrent probe observation lock was poisoned".to_string())?;
    if observed.iter().any(|value| !value) {
        return Err("concurrent probe lost one or more enqueued values".to_string());
    }
    Ok(())
}

fn probe_lengths(max_value_size: usize) -> Vec<usize> {
    let mut lengths = vec![0, 1, 7, 8, 9, max_value_size / 2, max_value_size];
    lengths.retain(|length| *length <= max_value_size);
    lengths.sort_unstable();
    lengths.dedup();
    lengths
}

fn payload(length: usize, tag: u8) -> Vec<u8> {
    (0..length)
        .map(|index| {
            tag.wrapping_mul(31)
                .wrapping_add((index as u8).wrapping_mul(17))
                .wrapping_add(0x4d)
        })
        .collect()
}

fn check_empty_output_is_unchanged(
    consumer: &mut crate::abi::Consumer,
    max_value_size: usize,
) -> Result<(), String> {
    let mut output = vec![SENTINEL_BYTE; max_value_size.clamp(1, 257)];
    let expected = output.clone();
    let mut output_length = SENTINEL_LENGTH;
    let status = consumer.dequeue_raw(output.as_mut_ptr(), output.len() as u64, &mut output_length);
    require_status(status, STATUS_EMPTY, "empty dequeue")?;
    if output != expected || output_length != SENTINEL_LENGTH {
        return Err("empty dequeue modified caller-owned output".to_string());
    }
    Ok(())
}

fn check_invalid_output_retains_value(
    producer: &mut crate::abi::Producer,
    consumer: &mut crate::abi::Consumer,
    max_value_size: usize,
) -> Result<(), String> {
    let length = max_value_size.clamp(1, 257);
    let expected = payload(length, 0xf1);
    require_status(
        producer.enqueue(&expected),
        STATUS_OK,
        "enqueue before undersized dequeue",
    )?;

    let mut output = vec![SENTINEL_BYTE; length];
    let unchanged = output.clone();
    let mut output_length = SENTINEL_LENGTH;
    let status = consumer.dequeue_raw(output.as_mut_ptr(), (length - 1) as u64, &mut output_length);
    require_status(status, STATUS_INVALID, "undersized dequeue")?;
    if output != unchanged || output_length != SENTINEL_LENGTH {
        return Err("undersized dequeue modified caller-owned output".to_string());
    }

    output.fill(SENTINEL_BYTE);
    let status = consumer.dequeue_raw(output.as_mut_ptr(), output.len() as u64, &mut output_length);
    require_status(status, STATUS_OK, "dequeue after undersized retry")?;
    if output_length != length as u64 || output != expected {
        return Err("undersized dequeue did not retain the oldest value".to_string());
    }
    Ok(())
}

fn require_status(actual: u32, expected: u32, operation: &str) -> Result<(), String> {
    if actual == expected {
        Ok(())
    } else {
        Err(format!(
            "{operation} returned ABI status {actual}, expected {expected}"
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::run_probe;
    use crate::abi::Api;

    #[test]
    fn reference_passes_abi_profiles() {
        for (capacity, value_size) in [(1, 8), (7, 257), (3, 1 << 20)] {
            run_probe(Api::reference(), capacity, value_size, 2, 2).unwrap();
        }
    }
}
