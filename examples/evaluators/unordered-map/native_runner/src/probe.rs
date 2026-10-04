use crate::abi::{Api, Client, STATUS_INTERNAL_ERROR, STATUS_INVALID, STATUS_MISSING, STATUS_OK};
use std::ptr;

const SENTINEL_BYTE: u8 = 0xa7;
const SENTINEL_LENGTH: u64 = u64::MAX;

pub fn run_probe(
    api: Api,
    max_key_size: usize,
    max_value_size: usize,
    client_count: u32,
) -> Result<(), String> {
    check_bad_pointers(&api, max_key_size, max_value_size, client_count)?;

    let map = api.create_map(max_key_size as u64, max_value_size as u64, client_count)?;
    let mut clients = (0..client_count)
        .map(|id| map.create_client(id))
        .collect::<Result<Vec<_>, _>>()?;
    let client = clients
        .first_mut()
        .ok_or_else(|| "ABI probe requires a client".to_string())?;

    check_missing_output_is_unchanged(client, max_key_size, max_value_size)?;

    for (tag, key_len) in probe_lengths(max_key_size).into_iter().enumerate() {
        for value_len in probe_lengths(max_value_size) {
            check_copied_put_get(client, key_len, value_len, max_value_size, tag as u8)?;
        }
    }

    check_replace_and_remove(client, max_key_size, max_value_size)?;
    check_invalid_output_retains_mapping(client, max_key_size, max_value_size)?;
    check_missing_output_is_unchanged(client, max_key_size, max_value_size)?;
    drop(clients);
    drop(map);
    Ok(())
}

fn check_bad_pointers(
    api: &Api,
    max_key_size: usize,
    max_value_size: usize,
    client_count: u32,
) -> Result<(), String> {
    let status = api.create_map_raw(
        max_key_size as u64,
        max_value_size as u64,
        client_count,
        ptr::null_mut(),
    );
    require_status(status, STATUS_INVALID, "map_create with a null output")?;

    let status = api.create_map_raw(0, max_value_size as u64, client_count, &mut ptr::null_mut());
    require_status(status, STATUS_INVALID, "map_create with a zero key size")?;

    let map = api.create_map(max_key_size as u64, max_value_size as u64, client_count)?;
    let status = map.create_client_raw(0, ptr::null_mut());
    require_status(status, STATUS_INVALID, "client_create with a null output")?;
    let status = map.create_client_raw(client_count, &mut ptr::null_mut());
    require_status(
        status,
        STATUS_INVALID,
        "client_create with an out-of-range id",
    )?;

    let mut client = map.create_client(0)?;
    let key = payload(max_key_size.clamp(1, 8), 0x11);
    let value = payload(max_value_size.clamp(1, 8), 0x22);
    let status = client.put_raw(
        ptr::null(),
        key.len() as u64,
        value.as_ptr(),
        value.len() as u64,
    );
    require_status(status, STATUS_INVALID, "put with a null key")?;
    let status = client.put_raw(
        key.as_ptr(),
        key.len() as u64,
        ptr::null(),
        value.len() as u64,
    );
    require_status(status, STATUS_INVALID, "put with a null value")?;

    let mut output_length = SENTINEL_LENGTH;
    let mut output = vec![SENTINEL_BYTE; value.len().max(1)];
    let status = client.get_raw(
        key.as_ptr(),
        key.len() as u64,
        output.as_mut_ptr(),
        output.len() as u64,
        ptr::null_mut(),
    );
    require_status(status, STATUS_INVALID, "get with a null output length")?;
    let status = client.get_raw(
        ptr::null(),
        key.len() as u64,
        output.as_mut_ptr(),
        output.len() as u64,
        &mut output_length,
    );
    require_status(status, STATUS_INVALID, "get with a null key")?;
    if output_length != SENTINEL_LENGTH {
        return Err("invalid get modified output length".to_string());
    }

    require_status(
        client.put(&key, &value),
        STATUS_OK,
        "put before null output get",
    )?;
    output_length = SENTINEL_LENGTH;
    let status = client.get_raw(
        key.as_ptr(),
        key.len() as u64,
        ptr::null_mut(),
        value.len() as u64,
        &mut output_length,
    );
    require_status(status, STATUS_INVALID, "get with a null output buffer")?;
    if output_length != SENTINEL_LENGTH {
        return Err("null-output get modified output length".to_string());
    }
    let (status, length) = client.get(&key, &mut output)?;
    require_status(status, STATUS_OK, "get after null-output retry")?;
    if length != value.len() || output[..length] != value {
        return Err("null-output get did not retain the mapping".to_string());
    }
    Ok(())
}

fn check_copied_put_get(
    client: &mut Client,
    key_len: usize,
    value_len: usize,
    max_value_size: usize,
    tag: u8,
) -> Result<(), String> {
    let mut key = payload(key_len, tag.wrapping_add(1));
    let mut value = payload(value_len, tag.wrapping_add(2));
    let expected_key = key.clone();
    let expected_value = value.clone();
    require_status(
        client.put(&key, &value),
        STATUS_OK,
        &format!("put key {key_len} value {value_len}"),
    )?;
    key.fill(0x5a);
    value.fill(0x5a);

    let mut output = vec![SENTINEL_BYTE; max_value_size.max(1)];
    let mut output_length = SENTINEL_LENGTH;
    let status = client.get_raw(
        expected_key.as_ptr(),
        expected_key.len() as u64,
        output.as_mut_ptr(),
        output.len() as u64,
        &mut output_length,
    );
    require_status(
        status,
        STATUS_OK,
        &format!("get key {key_len} value {value_len}"),
    )?;
    if output_length != value_len as u64 {
        return Err(format!(
            "get key {key_len} value {value_len} reported output length {output_length}"
        ));
    }
    if output[..value_len] != expected_value {
        return Err(format!(
            "get key {key_len} value {value_len} returned corrupted bytes"
        ));
    }
    if output[value_len..]
        .iter()
        .any(|byte| *byte != SENTINEL_BYTE)
    {
        return Err(format!(
            "get key {key_len} value {value_len} wrote beyond the returned value"
        ));
    }
    Ok(())
}

fn check_replace_and_remove(
    client: &mut Client,
    max_key_size: usize,
    max_value_size: usize,
) -> Result<(), String> {
    let key = payload(max_key_size.clamp(1, 8), 0x31);
    let first = payload(max_value_size.clamp(1, 8), 0x32);
    let second = payload(max_value_size.clamp(1, 16), 0x33);
    require_status(client.put(&key, &first), STATUS_OK, "put before replace")?;
    require_status(client.put(&key, &second), STATUS_OK, "put replace")?;

    let mut output = vec![SENTINEL_BYTE; max_value_size.max(1)];
    let (status, length) = client.get(&key, &mut output)?;
    require_status(status, STATUS_OK, "get after replace")?;
    if output[..length] != second {
        return Err("replace did not retain the latest value".to_string());
    }

    output.fill(SENTINEL_BYTE);
    let (status, length) = client.remove(&key, &mut output)?;
    require_status(status, STATUS_OK, "remove after replace")?;
    if output[..length] != second {
        return Err("remove returned a stale value".to_string());
    }
    check_missing_lookup(client, &key, &mut output, false, "get after remove")?;
    check_missing_lookup(client, &key, &mut output, true, "remove after remove")?;
    Ok(())
}

fn check_invalid_output_retains_mapping(
    client: &mut Client,
    max_key_size: usize,
    max_value_size: usize,
) -> Result<(), String> {
    let key = payload(max_key_size.clamp(1, 8), 0xf1);
    let length = max_value_size.clamp(1, 257);
    let expected = payload(length, 0xf2);
    require_status(
        client.put(&key, &expected),
        STATUS_OK,
        "put before undersized lookup",
    )?;

    for remove in [false, true] {
        let mut output = vec![SENTINEL_BYTE; length];
        let unchanged = output.clone();
        let mut output_length = SENTINEL_LENGTH;
        let status = if remove {
            client.remove_raw(
                key.as_ptr(),
                key.len() as u64,
                output.as_mut_ptr(),
                (length - 1) as u64,
                &mut output_length,
            )
        } else {
            client.get_raw(
                key.as_ptr(),
                key.len() as u64,
                output.as_mut_ptr(),
                (length - 1) as u64,
                &mut output_length,
            )
        };
        let label = if remove {
            "undersized remove"
        } else {
            "undersized get"
        };
        require_status(status, STATUS_INVALID, label)?;
        if output != unchanged || output_length != SENTINEL_LENGTH {
            return Err(format!("{label} modified caller-owned output"));
        }
    }

    let mut output = vec![SENTINEL_BYTE; length];
    let (status, got) = client.get(&key, &mut output)?;
    require_status(status, STATUS_OK, "get after undersized retry")?;
    if got != length || output != expected {
        return Err("undersized lookup did not retain the mapping".to_string());
    }
    let (status, got) = client.remove(&key, &mut output)?;
    require_status(status, STATUS_OK, "remove after undersized retry")?;
    if got != length || output[..got] != expected {
        return Err("undersized remove did not retain the mapping".to_string());
    }
    Ok(())
}

fn check_missing_output_is_unchanged(
    client: &mut Client,
    max_key_size: usize,
    max_value_size: usize,
) -> Result<(), String> {
    let key = payload(max_key_size.clamp(1, 9), 0x44);
    let mut output = vec![SENTINEL_BYTE; max_value_size.clamp(1, 257)];
    check_missing_lookup(client, &key, &mut output, false, "missing get")?;
    check_missing_lookup(client, &key, &mut output, true, "missing remove")?;
    Ok(())
}

fn check_missing_lookup(
    client: &mut Client,
    key: &[u8],
    output: &mut [u8],
    remove: bool,
    operation: &str,
) -> Result<(), String> {
    output.fill(SENTINEL_BYTE);
    let expected = output.to_vec();
    let mut output_length = SENTINEL_LENGTH;
    let status = if remove {
        client.remove_raw(
            key.as_ptr(),
            key.len() as u64,
            output.as_mut_ptr(),
            output.len() as u64,
            &mut output_length,
        )
    } else {
        client.get_raw(
            key.as_ptr(),
            key.len() as u64,
            output.as_mut_ptr(),
            output.len() as u64,
            &mut output_length,
        )
    };
    require_status(status, STATUS_MISSING, operation)?;
    if output != expected || output_length != SENTINEL_LENGTH {
        return Err(format!("{operation} modified caller-owned output"));
    }
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
    } else if actual == STATUS_INTERNAL_ERROR {
        Err(format!(
            "{operation} returned ABI internal error, expected {expected}"
        ))
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
        for (key_size, value_size, clients) in [(1, 8, 2), (7, 257, 2), (8, 64, 4)] {
            run_probe(Api::reference(), key_size, value_size, clients).unwrap();
        }
    }
}
