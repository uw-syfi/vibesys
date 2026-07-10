use crate::abi::{Api, STATUS_EMPTY, STATUS_INVALID, STATUS_OK};

const SENTINEL_BYTE: u8 = 0xa7;
const SENTINEL_LENGTH: u64 = u64::MAX;

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
