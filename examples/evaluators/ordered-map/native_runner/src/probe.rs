use crate::abi::{Api, Client, STATUS_INVALID, STATUS_MISSING, STATUS_OK};

const SENTINEL_BYTE: u8 = 0xa7;
const SENTINEL_LENGTH: u64 = u64::MAX;

pub fn run_probe(
    api: Api,
    max_key_size: u64,
    max_value_size: u64,
    client_count: u32,
) -> Result<(), String> {
    if client_count == 0 {
        return Err("ABI probe requires a client".to_string());
    }
    let max_key = max_key_size as usize;
    let max_value = max_value_size as usize;
    {
        let map = api.create_map(max_key_size, max_value_size, client_count)?;
        let mut clients = (0..client_count)
            .map(|id| map.create_client(id))
            .collect::<Result<Vec<_>, _>>()?;
        let client = clients
            .first_mut()
            .ok_or_else(|| "ABI probe requires a client".to_string())?;

        check_empty_min_max_unchanged(client, max_key, max_value)?;
        check_missing_predecessor_successor(client, max_key, max_value)?;
        check_range_empty(client, max_key, max_value)?;

        for (tag, length) in probe_lengths(max_key.min(max_value))
            .into_iter()
            .enumerate()
        {
            let mut key = payload(length.min(max_key), (tag as u8).wrapping_add(1));
            let mut value = payload(length.min(max_value), (tag as u8).wrapping_add(17));
            let expected_key = key.clone();
            let expected_value = value.clone();
            require_status(
                client.put(&key, &value),
                STATUS_OK,
                &format!("put length {length}"),
            )?;
            key.fill(0x5a);
            value.fill(0x5a);

            let mut output = vec![SENTINEL_BYTE; max_value.max(1)];
            let mut output_length = SENTINEL_LENGTH;
            let status = client.get_raw(
                &expected_key,
                output.as_mut_ptr(),
                output.len() as u64,
                &mut output_length,
            );
            require_status(status, STATUS_OK, &format!("get length {length}"))?;
            if output_length != expected_value.len() as u64 {
                return Err(format!(
                    "get length {length} reported output length {output_length}"
                ));
            }
            if output[..expected_value.len()] != expected_value {
                return Err(format!("get length {length} returned corrupted bytes"));
            }
            if output[expected_value.len()..]
                .iter()
                .any(|byte| *byte != SENTINEL_BYTE)
            {
                return Err(format!(
                    "get length {length} wrote beyond the returned value"
                ));
            }
        }

        check_copy_lifetime(client, max_key, max_value)?;
        check_undersized_output_retains_mapping(client, max_key, max_value)?;
        check_oversize_query_keys(client, max_key, max_value)?;
    }
    check_ordered_neighbors_and_range(&api, max_key, max_value, client_count)?;
    check_range_truncation_remaining(&api, max_key, max_value, client_count)?;
    check_range_invalid_stride(&api, max_key, max_value, client_count)?;
    Ok(())
}

fn probe_lengths(max_size: usize) -> Vec<usize> {
    let mut lengths = vec![0, 1, 7, 8, 9, max_size / 2, max_size];
    lengths.retain(|length| *length <= max_size);
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

fn require_status(actual: u32, expected: u32, operation: &str) -> Result<(), String> {
    if actual == expected {
        Ok(())
    } else {
        Err(format!(
            "{operation} returned ABI status {actual}, expected {expected}"
        ))
    }
}

fn check_empty_min_max_unchanged(
    client: &mut Client,
    max_key_size: usize,
    max_value_size: usize,
) -> Result<(), String> {
    for max in [false, true] {
        let mut key_out = vec![SENTINEL_BYTE; max_key_size.max(1)];
        let mut val_out = vec![SENTINEL_BYTE; max_value_size.max(1)];
        let expected_key = key_out.clone();
        let expected_val = val_out.clone();
        let mut key_len = SENTINEL_LENGTH;
        let mut val_len = SENTINEL_LENGTH;
        let status = client.ordered_raw(
            max,
            key_out.as_mut_ptr(),
            key_out.len() as u64,
            &mut key_len,
            val_out.as_mut_ptr(),
            val_out.len() as u64,
            &mut val_len,
        );
        let name = if max { "empty max" } else { "empty min" };
        require_status(status, STATUS_MISSING, name)?;
        if key_out != expected_key
            || val_out != expected_val
            || key_len != SENTINEL_LENGTH
            || val_len != SENTINEL_LENGTH
        {
            return Err(format!("{name} modified caller-owned output"));
        }
    }
    Ok(())
}

fn check_missing_predecessor_successor(
    client: &mut Client,
    max_key_size: usize,
    max_value_size: usize,
) -> Result<(), String> {
    let query = payload(max_key_size.clamp(1, 3), 0x11);
    for successor in [false, true] {
        let mut key_out = vec![SENTINEL_BYTE; max_key_size.max(1)];
        let mut val_out = vec![SENTINEL_BYTE; max_value_size.max(1)];
        let expected_key = key_out.clone();
        let expected_val = val_out.clone();
        let mut key_len = SENTINEL_LENGTH;
        let mut val_len = SENTINEL_LENGTH;
        let status = client.neighbor_raw(
            successor,
            &query,
            key_out.as_mut_ptr(),
            key_out.len() as u64,
            &mut key_len,
            val_out.as_mut_ptr(),
            val_out.len() as u64,
            &mut val_len,
        );
        let name = if successor {
            "missing successor"
        } else {
            "missing predecessor"
        };
        require_status(status, STATUS_MISSING, name)?;
        if key_out != expected_key
            || val_out != expected_val
            || key_len != SENTINEL_LENGTH
            || val_len != SENTINEL_LENGTH
        {
            return Err(format!("{name} modified caller-owned output"));
        }
    }
    Ok(())
}

fn check_range_empty(
    client: &mut Client,
    max_key_size: usize,
    max_value_size: usize,
) -> Result<(), String> {
    let start = payload(1.min(max_key_size), 0x21);
    let end = payload(1.min(max_key_size), 0x22);
    probe_empty_range(
        client,
        &start,
        &end,
        max_key_size,
        max_value_size,
        "range over empty map",
    )?;
    probe_empty_range(
        client,
        &start,
        &start,
        max_key_size,
        max_value_size,
        "equal-bound range over empty map",
    )?;
    probe_empty_range(
        client,
        &end,
        &start,
        max_key_size,
        max_value_size,
        "reversed range over empty map",
    )?;
    Ok(())
}

fn probe_empty_range(
    client: &mut Client,
    start: &[u8],
    end: &[u8],
    max_key_size: usize,
    max_value_size: usize,
    name: &str,
) -> Result<(), String> {
    let key_stride = max_key_size.max(1);
    let val_stride = max_value_size.max(1);
    let mut keys_out = vec![SENTINEL_BYTE; key_stride];
    let mut vals_out = vec![SENTINEL_BYTE; val_stride];
    let expected_keys = keys_out.clone();
    let expected_vals = vals_out.clone();
    let mut lengths_key = vec![SENTINEL_LENGTH; 1];
    let mut lengths_val = vec![SENTINEL_LENGTH; 1];
    let mut count = SENTINEL_LENGTH;
    let mut remaining = SENTINEL_LENGTH;
    let status = client.range_raw(
        start,
        end,
        keys_out.as_mut_ptr(),
        key_stride as u64,
        vals_out.as_mut_ptr(),
        val_stride as u64,
        lengths_key.as_mut_ptr(),
        lengths_val.as_mut_ptr(),
        1,
        &mut count,
        &mut remaining,
    );
    require_status(status, STATUS_OK, name)?;
    if count != 0 || remaining != 0 {
        return Err(format!(
            "{name} reported count {count} remaining {remaining}, expected 0 and 0"
        ));
    }
    if keys_out != expected_keys
        || vals_out != expected_vals
        || lengths_key[0] != SENTINEL_LENGTH
        || lengths_val[0] != SENTINEL_LENGTH
    {
        return Err(format!("{name} modified caller-owned output"));
    }
    Ok(())
}

fn check_oversize_query_keys(
    client: &mut Client,
    max_key_size: usize,
    max_value_size: usize,
) -> Result<(), String> {
    let key = payload(max_key_size.clamp(1, 3), 0x71);
    let value = payload(max_value_size.clamp(1, 5), 0x72);
    require_status(
        client.put(&key, &value),
        STATUS_OK,
        "put before oversize queries",
    )?;
    let oversized = payload(max_key_size + 1, 0x73);

    let mut output = vec![SENTINEL_BYTE; max_value_size.max(1)];
    let unchanged_output = output.clone();
    let mut output_length = SENTINEL_LENGTH;
    let status = client.get_raw(
        &oversized,
        output.as_mut_ptr(),
        output.len() as u64,
        &mut output_length,
    );
    require_status(status, STATUS_INVALID, "oversize get")?;
    if output != unchanged_output || output_length != SENTINEL_LENGTH {
        return Err("oversize get modified caller-owned output".to_string());
    }

    output.fill(SENTINEL_BYTE);
    output_length = SENTINEL_LENGTH;
    let status = client.remove_raw(
        &oversized,
        output.as_mut_ptr(),
        output.len() as u64,
        &mut output_length,
    );
    require_status(status, STATUS_INVALID, "oversize remove")?;
    if output != unchanged_output || output_length != SENTINEL_LENGTH {
        return Err("oversize remove modified caller-owned output".to_string());
    }

    let mut key_out = vec![SENTINEL_BYTE; max_key_size.max(1)];
    let mut val_out = vec![SENTINEL_BYTE; max_value_size.max(1)];
    let expected_key = key_out.clone();
    let expected_val = val_out.clone();
    for successor in [false, true] {
        let mut key_len = SENTINEL_LENGTH;
        let mut val_len = SENTINEL_LENGTH;
        key_out.fill(SENTINEL_BYTE);
        val_out.fill(SENTINEL_BYTE);
        let status = client.neighbor_raw(
            successor,
            &oversized,
            key_out.as_mut_ptr(),
            key_out.len() as u64,
            &mut key_len,
            val_out.as_mut_ptr(),
            val_out.len() as u64,
            &mut val_len,
        );
        let name = if successor {
            "oversize successor"
        } else {
            "oversize predecessor"
        };
        require_status(status, STATUS_INVALID, name)?;
        if key_out != expected_key
            || val_out != expected_val
            || key_len != SENTINEL_LENGTH
            || val_len != SENTINEL_LENGTH
        {
            return Err(format!("{name} modified caller-owned output"));
        }
    }

    let valid_end = payload(1.min(max_key_size), 0x22);
    check_oversize_range(
        client,
        &oversized,
        &valid_end,
        max_key_size,
        max_value_size,
        "oversize range start",
    )?;
    check_oversize_range(
        client,
        &valid_end,
        &oversized,
        max_key_size,
        max_value_size,
        "oversize range end",
    )?;

    output.fill(SENTINEL_BYTE);
    let (status, length) = client.lookup(false, &key, &mut output)?;
    require_status(status, STATUS_OK, "get after oversize queries")?;
    if output[..length] != value {
        return Err("oversize queries did not leave the mapping in place".to_string());
    }
    Ok(())
}

fn check_oversize_range(
    client: &mut Client,
    start: &[u8],
    end: &[u8],
    max_key_size: usize,
    max_value_size: usize,
    name: &str,
) -> Result<(), String> {
    let key_stride = max_key_size.max(1);
    let val_stride = max_value_size.max(1);
    let mut keys_out = vec![SENTINEL_BYTE; key_stride];
    let mut vals_out = vec![SENTINEL_BYTE; val_stride];
    let expected_keys = keys_out.clone();
    let expected_vals = vals_out.clone();
    let mut lengths_key = vec![SENTINEL_LENGTH; 1];
    let mut lengths_val = vec![SENTINEL_LENGTH; 1];
    let mut count = SENTINEL_LENGTH;
    let mut remaining = SENTINEL_LENGTH;
    let status = client.range_raw(
        start,
        end,
        keys_out.as_mut_ptr(),
        key_stride as u64,
        vals_out.as_mut_ptr(),
        val_stride as u64,
        lengths_key.as_mut_ptr(),
        lengths_val.as_mut_ptr(),
        1,
        &mut count,
        &mut remaining,
    );
    require_status(status, STATUS_INVALID, name)?;
    if keys_out != expected_keys
        || vals_out != expected_vals
        || lengths_key[0] != SENTINEL_LENGTH
        || lengths_val[0] != SENTINEL_LENGTH
        || count != SENTINEL_LENGTH
        || remaining != SENTINEL_LENGTH
    {
        return Err(format!("{name} modified caller-owned output"));
    }
    Ok(())
}

fn check_copy_lifetime(
    client: &mut Client,
    max_key_size: usize,
    max_value_size: usize,
) -> Result<(), String> {
    let mut key = payload(max_key_size.clamp(1, 5), 0x31);
    let mut value = payload(max_value_size.clamp(1, 9), 0x32);
    let expected_key = key.clone();
    let expected_value = value.clone();
    require_status(client.put(&key, &value), STATUS_OK, "copy-lifetime put")?;
    key.fill(0x3c);
    value.fill(0x3c);
    let mut output = vec![SENTINEL_BYTE; max_value_size.max(1)];
    let (status, length) = client.lookup(false, &expected_key, &mut output)?;
    require_status(status, STATUS_OK, "copy-lifetime get")?;
    if output[..length] != expected_value {
        return Err("put did not retain an independent copy of the value".to_string());
    }
    Ok(())
}

fn check_undersized_output_retains_mapping(
    client: &mut Client,
    max_key_size: usize,
    max_value_size: usize,
) -> Result<(), String> {
    let key = payload(max_key_size.clamp(1, 4), 0x41);
    let value = payload(max_value_size.clamp(2, 257), 0xf1);
    require_status(
        client.put(&key, &value),
        STATUS_OK,
        "put before undersized get",
    )?;

    let mut output = vec![SENTINEL_BYTE; value.len()];
    let unchanged = output.clone();
    let mut output_length = SENTINEL_LENGTH;
    let status = client.get_raw(
        &key,
        output.as_mut_ptr(),
        (value.len() - 1) as u64,
        &mut output_length,
    );
    require_status(status, STATUS_INVALID, "undersized get")?;
    if output != unchanged || output_length != SENTINEL_LENGTH {
        return Err("undersized get modified caller-owned output".to_string());
    }

    output.fill(SENTINEL_BYTE);
    let status = client.get_raw(
        &key,
        output.as_mut_ptr(),
        output.len() as u64,
        &mut output_length,
    );
    require_status(status, STATUS_OK, "get after undersized retry")?;
    if output_length != value.len() as u64 || output[..value.len()] != value {
        return Err("undersized get did not retain the mapping".to_string());
    }
    Ok(())
}

fn distinct_keys(max_key_size: usize) -> Result<[Vec<u8>; 3], String> {
    let mut keys = [
        payload(max_key_size.clamp(1, 3), 0x51),
        payload(max_key_size.clamp(1, 3), 0x52),
        payload(max_key_size.clamp(1, 3), 0x53),
    ];
    if keys[0] == keys[1] || keys[1] == keys[2] || keys[0] == keys[2] {
        if max_key_size == 0 {
            return Err("cannot construct distinct keys".to_string());
        }
        keys[0] = vec![0x01];
        keys[1] = vec![0x02];
        keys[2] = vec![0x03];
        for key in &mut keys {
            key.resize(max_key_size.clamp(1, 3), 0);
        }
        if keys[0] == keys[1] {
            keys[0][0] = 1;
            keys[1][0] = 2;
            keys[2][0] = 3;
        }
    }
    keys.sort();
    Ok(keys)
}

struct ThreeKeyMap {
    client: Client,
    keys: [Vec<u8>; 3],
    values: [Vec<u8>; 3],
    _map: crate::abi::Map,
}

fn with_three_keys(
    api: &Api,
    max_key_size: usize,
    max_value_size: usize,
    client_count: u32,
) -> Result<ThreeKeyMap, String> {
    let map = api.create_map(max_key_size as u64, max_value_size as u64, client_count)?;
    let mut client = map.create_client(0)?;
    let keys = distinct_keys(max_key_size)?;
    let values = [
        payload(max_value_size.clamp(1, 2), 0x61),
        payload(max_value_size.clamp(1, 2), 0x62),
        payload(max_value_size.clamp(1, 2), 0x63),
    ];
    for (key, value) in keys.iter().zip(values.iter()) {
        require_status(client.put(key, value), STATUS_OK, "ordered-probe put")?;
    }
    Ok(ThreeKeyMap {
        _map: map,
        client,
        keys,
        values,
    })
}

fn check_ordered_neighbors_and_range(
    api: &Api,
    max_key_size: usize,
    max_value_size: usize,
    client_count: u32,
) -> Result<(), String> {
    let mut populated = with_three_keys(api, max_key_size, max_value_size, client_count)?;
    let client = &mut populated.client;
    let keys = &populated.keys;
    let values = &populated.values;

    let mut key_out = vec![0_u8; max_key_size.max(1)];
    let mut val_out = vec![0_u8; max_value_size.max(1)];
    let mut key_len = 0;
    let mut val_len = 0;
    let status = client.ordered_raw(
        false,
        key_out.as_mut_ptr(),
        key_out.len() as u64,
        &mut key_len,
        val_out.as_mut_ptr(),
        val_out.len() as u64,
        &mut val_len,
    );
    require_status(status, STATUS_OK, "min")?;
    if key_out[..key_len as usize] != keys[0] || val_out[..val_len as usize] != values[0] {
        return Err("min did not return the least key".to_string());
    }
    let status = client.ordered_raw(
        true,
        key_out.as_mut_ptr(),
        key_out.len() as u64,
        &mut key_len,
        val_out.as_mut_ptr(),
        val_out.len() as u64,
        &mut val_len,
    );
    require_status(status, STATUS_OK, "max")?;
    if key_out[..key_len as usize] != keys[2] {
        return Err("max did not return the greatest key".to_string());
    }

    let status = client.neighbor_raw(
        true,
        &keys[0],
        key_out.as_mut_ptr(),
        key_out.len() as u64,
        &mut key_len,
        val_out.as_mut_ptr(),
        val_out.len() as u64,
        &mut val_len,
    );
    require_status(status, STATUS_OK, "successor")?;
    if key_out[..key_len as usize] != keys[1] {
        return Err("successor did not return the next key".to_string());
    }
    let status = client.neighbor_raw(
        false,
        &keys[2],
        key_out.as_mut_ptr(),
        key_out.len() as u64,
        &mut key_len,
        val_out.as_mut_ptr(),
        val_out.len() as u64,
        &mut val_len,
    );
    require_status(status, STATUS_OK, "predecessor")?;
    if key_out[..key_len as usize] != keys[1] {
        return Err("predecessor did not return the previous key".to_string());
    }

    check_range_contents(client, keys, values, max_key_size, max_value_size)?;
    Ok(())
}

fn check_range_contents(
    client: &mut Client,
    keys: &[Vec<u8>; 3],
    values: &[Vec<u8>; 3],
    max_key_size: usize,
    max_value_size: usize,
) -> Result<(), String> {
    let key_stride = max_key_size.max(1);
    let val_stride = max_value_size.max(1);
    let mut keys_out = vec![SENTINEL_BYTE; key_stride * 3];
    let mut vals_out = vec![SENTINEL_BYTE; val_stride * 3];
    let mut lengths_key = vec![SENTINEL_LENGTH; 3];
    let mut lengths_val = vec![SENTINEL_LENGTH; 3];
    let mut count = SENTINEL_LENGTH;
    let mut remaining = SENTINEL_LENGTH;
    let status = client.range_raw(
        &keys[0],
        &keys[2],
        keys_out.as_mut_ptr(),
        key_stride as u64,
        vals_out.as_mut_ptr(),
        val_stride as u64,
        lengths_key.as_mut_ptr(),
        lengths_val.as_mut_ptr(),
        3,
        &mut count,
        &mut remaining,
    );
    require_status(status, STATUS_OK, "multi-key range")?;
    if count != 2 || remaining != 0 {
        return Err(format!(
            "half-open range reported count {count} remaining {remaining}, expected 2 and 0"
        ));
    }
    for index in 0..2 {
        let key_len = lengths_key[index] as usize;
        let val_len = lengths_val[index] as usize;
        if keys_out[index * key_stride..index * key_stride + key_len] != keys[index]
            || vals_out[index * val_stride..index * val_stride + val_len] != values[index]
        {
            return Err("range items were not copied in increasing key order".to_string());
        }
    }

    keys_out.fill(SENTINEL_BYTE);
    count = SENTINEL_LENGTH;
    remaining = SENTINEL_LENGTH;
    let status = client.range_raw(
        &keys[1],
        &keys[1],
        keys_out.as_mut_ptr(),
        key_stride as u64,
        vals_out.as_mut_ptr(),
        val_stride as u64,
        lengths_key.as_mut_ptr(),
        lengths_val.as_mut_ptr(),
        3,
        &mut count,
        &mut remaining,
    );
    require_status(status, STATUS_OK, "empty interval range")?;
    if count != 0 || remaining != 0 {
        return Err("empty interval range was not empty".to_string());
    }
    probe_empty_range(
        client,
        &keys[2],
        &keys[0],
        max_key_size,
        max_value_size,
        "reversed populated range",
    )?;

    keys_out.fill(SENTINEL_BYTE);
    count = SENTINEL_LENGTH;
    remaining = SENTINEL_LENGTH;
    let mut end = keys[0].clone();
    end.push(0);
    let start = keys[0].clone();
    let status = client.range_raw(
        &start,
        if end.len() <= max_key_size {
            &end
        } else {
            &keys[1]
        },
        keys_out.as_mut_ptr(),
        key_stride as u64,
        vals_out.as_mut_ptr(),
        val_stride as u64,
        lengths_key.as_mut_ptr(),
        lengths_val.as_mut_ptr(),
        3,
        &mut count,
        &mut remaining,
    );
    require_status(status, STATUS_OK, "singleton range")?;
    if count != 1 || remaining != 0 {
        return Err(format!(
            "singleton range reported count {count} remaining {remaining}"
        ));
    }
    Ok(())
}

fn check_range_truncation_remaining(
    api: &Api,
    max_key_size: usize,
    max_value_size: usize,
    client_count: u32,
) -> Result<(), String> {
    let mut populated = with_three_keys(api, max_key_size, max_value_size, client_count)?;
    let client = &mut populated.client;
    let keys = &populated.keys;
    let key_stride = max_key_size.max(1);
    let val_stride = max_value_size.max(1);
    let mut keys_out = vec![SENTINEL_BYTE; key_stride];
    let mut vals_out = vec![SENTINEL_BYTE; val_stride];
    let mut lengths_key = vec![SENTINEL_LENGTH; 1];
    let mut lengths_val = vec![SENTINEL_LENGTH; 1];
    let mut count = SENTINEL_LENGTH;
    let mut remaining = SENTINEL_LENGTH;
    let status = client.range_raw(
        &keys[0],
        &[0xff],
        keys_out.as_mut_ptr(),
        key_stride as u64,
        vals_out.as_mut_ptr(),
        val_stride as u64,
        lengths_key.as_mut_ptr(),
        lengths_val.as_mut_ptr(),
        1,
        &mut count,
        &mut remaining,
    );
    require_status(status, STATUS_OK, "truncated range")?;
    if count != 1 || remaining != 1 {
        return Err(format!(
            "truncated range reported count {count} remaining {remaining}, expected 1 and 1"
        ));
    }
    Ok(())
}

fn check_range_invalid_stride(
    api: &Api,
    max_key_size: usize,
    max_value_size: usize,
    client_count: u32,
) -> Result<(), String> {
    let mut populated = with_three_keys(api, max_key_size, max_value_size, client_count)?;
    let client = &mut populated.client;
    let keys = &populated.keys;
    if keys[0].is_empty() {
        return Ok(());
    }
    let mut keys_out = vec![SENTINEL_BYTE; 4];
    let mut vals_out = vec![SENTINEL_BYTE; max_value_size.max(1)];
    let expected_keys = keys_out.clone();
    let expected_vals = vals_out.clone();
    let mut lengths_key = vec![SENTINEL_LENGTH; 1];
    let mut lengths_val = vec![SENTINEL_LENGTH; 1];
    let mut count = SENTINEL_LENGTH;
    let mut remaining = SENTINEL_LENGTH;
    let status = client.range_raw(
        &keys[0],
        &[0xff],
        keys_out.as_mut_ptr(),
        0,
        vals_out.as_mut_ptr(),
        max_value_size.max(1) as u64,
        lengths_key.as_mut_ptr(),
        lengths_val.as_mut_ptr(),
        1,
        &mut count,
        &mut remaining,
    );
    require_status(status, STATUS_INVALID, "range with undersized stride")?;
    if keys_out != expected_keys
        || vals_out != expected_vals
        || count != SENTINEL_LENGTH
        || remaining != SENTINEL_LENGTH
        || lengths_key[0] != SENTINEL_LENGTH
        || lengths_val[0] != SENTINEL_LENGTH
    {
        return Err("invalid range modified caller-owned output".to_string());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::run_probe;
    use crate::abi::Api;

    #[test]
    fn reference_passes_abi_profiles() {
        for (max_key_size, max_value_size) in [(1_u64, 8), (8, 257), (3, 64)] {
            run_probe(Api::reference(), max_key_size, max_value_size, 2).unwrap();
        }
    }
}
