use crate::abi::{Api, Client, STATUS_INVALID, STATUS_MISSING, STATUS_OK};
use std::fs::File;
use std::io::{ErrorKind, Read, Write};
use std::os::fd::FromRawFd;
use std::thread;

const OPERATION_PUT: u32 = 1;
const OPERATION_GET: u32 = 2;
const OPERATION_REMOVE: u32 = 3;
const OPERATION_MIN: u32 = 4;
const OPERATION_MAX: u32 = 5;
const OPERATION_PREDECESSOR: u32 = 6;
const OPERATION_SUCCESSOR: u32 = 7;
const OPERATION_RANGE: u32 = 8;

const RESPONSE_OK: u32 = 1;
const RESPONSE_MISSING: u32 = 2;
const RESPONSE_ERROR: u32 = 3;

struct Request {
    operation: u32,
    extra: u32,
    key: Vec<u8>,
    value: Vec<u8>,
}

pub struct WorkerConfig {
    pub fd_base: i32,
    pub lane_count: usize,
    pub client_count: u32,
    pub mixed_lane: bool,
    pub max_key_size: u64,
    pub max_value_size: u64,
}

pub fn run_worker(api: Api, config: WorkerConfig) -> Result<(), String> {
    if config.lane_count == 0 {
        return Err("worker requires at least one lane".to_string());
    }
    if config.client_count == 0 {
        return Err("worker requires at least one client".to_string());
    }
    if config.mixed_lane && config.lane_count != 1 {
        return Err("mixed correctness mode requires exactly one lane".to_string());
    }
    if config.mixed_lane && config.client_count != 1 {
        return Err("mixed correctness mode requires exactly one client".to_string());
    }
    if !config.mixed_lane && config.lane_count != config.client_count as usize {
        return Err("lane count does not match client count".to_string());
    }

    let map = api.create_map(
        config.max_key_size,
        config.max_value_size,
        config.client_count,
    )?;
    let mut clients = (0..config.client_count)
        .map(|id| map.create_client(id))
        .collect::<Result<Vec<_>, _>>()?
        .into_iter()
        .map(Some)
        .collect::<Vec<_>>();
    let mut lane_clients = Vec::with_capacity(config.lane_count);
    for (lane, client) in clients.iter_mut().enumerate().take(config.lane_count) {
        lane_clients.push(
            client
                .take()
                .ok_or_else(|| format!("lane {lane} has no client"))?,
        );
    }
    let max_key_size = config.max_key_size as usize;
    let max_value_size = config.max_value_size as usize;
    let result = thread::scope(|scope| {
        let mut workers = Vec::with_capacity(config.lane_count);
        for (lane, client) in lane_clients.into_iter().enumerate() {
            let fd = config.fd_base + lane as i32;
            workers.push(scope.spawn(move || {
                let file = unsafe { File::from_raw_fd(fd) };
                serve_lane(file, client, max_key_size, max_value_size)
                    .map_err(|error| format!("lane {lane}: {error}"))
            }));
        }

        let mut combined = Ok(());
        for worker in workers {
            match worker.join() {
                Ok(Ok(())) => {}
                Ok(Err(error)) => combined = Err(error),
                Err(_) => combined = Err("correctness lane panicked".to_string()),
            }
        }
        combined
    });
    drop(map);
    result
}

fn serve_lane(
    mut file: File,
    mut client: Client,
    max_key_size: usize,
    max_value_size: usize,
) -> Result<(), String> {
    while let Some(mut request) = read_request(&mut file, max_key_size, max_value_size)? {
        match request.operation {
            OPERATION_PUT => match client.put(&request.key, &request.value) {
                STATUS_OK => {
                    request.key.fill(0xa5);
                    request.value.fill(0xa5);
                    write_response(&mut file, RESPONSE_OK, 0, &[], &[])?;
                }
                STATUS_INVALID => {
                    write_response(&mut file, RESPONSE_ERROR, 0, &[], &[])?;
                    return Err("put returned INVALID".to_string());
                }
                status => {
                    write_response(&mut file, RESPONSE_ERROR, 0, &[], &[])?;
                    return Err(format!("put returned invalid ABI status {status}"));
                }
            },
            OPERATION_GET | OPERATION_REMOVE => {
                let mut output = vec![0_u8; max_value_size];
                let (status, length) = client.lookup(
                    request.operation == OPERATION_REMOVE,
                    &request.key,
                    &mut output,
                )?;
                write_lookup_response(&mut file, status, &output[..length])?;
            }
            OPERATION_MIN | OPERATION_MAX => {
                write_ordered_response(
                    &mut file,
                    &mut client,
                    request.operation == OPERATION_MAX,
                    max_key_size,
                    max_value_size,
                )?;
            }
            OPERATION_PREDECESSOR | OPERATION_SUCCESSOR => {
                write_neighbor_response(
                    &mut file,
                    &mut client,
                    request.operation == OPERATION_SUCCESSOR,
                    &request.key,
                    max_key_size,
                    max_value_size,
                )?;
            }
            OPERATION_RANGE => write_range_response(
                &mut file,
                &mut client,
                &request.key,
                &request.value,
                request.extra as u64,
                max_key_size,
                max_value_size,
            )?,
            operation => {
                write_response(&mut file, RESPONSE_ERROR, 0, &[], &[])?;
                return Err(format!("unknown operation {operation}"));
            }
        }
    }
    Ok(())
}

fn write_lookup_response(file: &mut File, status: u32, value: &[u8]) -> Result<(), String> {
    match status {
        STATUS_OK => write_response(file, RESPONSE_OK, 0, &[], value),
        STATUS_MISSING => write_response(file, RESPONSE_MISSING, 0, &[], &[]),
        status => {
            write_response(file, RESPONSE_ERROR, 0, &[], &[])?;
            Err(format!("lookup returned invalid ABI status {status}"))
        }
    }
}

fn write_ordered_response(
    file: &mut File,
    client: &mut Client,
    max: bool,
    max_key_size: usize,
    max_value_size: usize,
) -> Result<(), String> {
    let mut key_out = vec![0_u8; max_key_size];
    let mut val_out = vec![0_u8; max_value_size];
    let mut key_len = u64::MAX;
    let mut val_len = u64::MAX;
    let status = client.ordered_raw(
        max,
        key_out.as_mut_ptr(),
        key_out.len() as u64,
        &mut key_len,
        val_out.as_mut_ptr(),
        val_out.len() as u64,
        &mut val_len,
    );
    write_pair_response(file, status, &key_out, key_len, &val_out, val_len)
}

fn write_neighbor_response(
    file: &mut File,
    client: &mut Client,
    successor: bool,
    key: &[u8],
    max_key_size: usize,
    max_value_size: usize,
) -> Result<(), String> {
    let mut key_out = vec![0_u8; max_key_size];
    let mut val_out = vec![0_u8; max_value_size];
    let mut key_len = u64::MAX;
    let mut val_len = u64::MAX;
    let status = client.neighbor_raw(
        successor,
        key,
        key_out.as_mut_ptr(),
        key_out.len() as u64,
        &mut key_len,
        val_out.as_mut_ptr(),
        val_out.len() as u64,
        &mut val_len,
    );
    write_pair_response(file, status, &key_out, key_len, &val_out, val_len)
}

fn write_pair_response(
    file: &mut File,
    status: u32,
    key_out: &[u8],
    key_len: u64,
    val_out: &[u8],
    val_len: u64,
) -> Result<(), String> {
    match status {
        STATUS_OK => {
            if key_len as usize > key_out.len() || val_len as usize > val_out.len() {
                write_response(file, RESPONSE_ERROR, 0, &[], &[])?;
                return Err("ordered op returned a length larger than the output".to_string());
            }
            write_response(
                file,
                RESPONSE_OK,
                0,
                &key_out[..key_len as usize],
                &val_out[..val_len as usize],
            )
        }
        STATUS_MISSING => write_response(file, RESPONSE_MISSING, 0, &[], &[]),
        status => {
            write_response(file, RESPONSE_ERROR, 0, &[], &[])?;
            Err(format!("ordered op returned invalid ABI status {status}"))
        }
    }
}

fn write_range_response(
    file: &mut File,
    client: &mut Client,
    start: &[u8],
    end: &[u8],
    max_items: u64,
    max_key_size: usize,
    max_value_size: usize,
) -> Result<(), String> {
    let max_items_usize = usize::try_from(max_items)
        .map_err(|_| "range max_items does not fit in usize".to_string())?;
    let key_stride = max_key_size;
    let val_stride = max_value_size;
    let mut keys_out = vec![0_u8; key_stride.saturating_mul(max_items_usize)];
    let mut vals_out = vec![0_u8; val_stride.saturating_mul(max_items_usize)];
    let mut lengths_key = vec![0_u64; max_items_usize];
    let mut lengths_val = vec![0_u64; max_items_usize];
    let mut count = u64::MAX;
    let mut remaining = u64::MAX;
    let status = client.range_raw(
        start,
        end,
        keys_out.as_mut_ptr(),
        key_stride as u64,
        vals_out.as_mut_ptr(),
        val_stride as u64,
        lengths_key.as_mut_ptr(),
        lengths_val.as_mut_ptr(),
        max_items,
        &mut count,
        &mut remaining,
    );
    match status {
        STATUS_OK => {
            if remaining > 1 {
                write_response(file, RESPONSE_ERROR, 0, &[], &[])?;
                return Err(format!("range remaining flag is {remaining}"));
            }
            if count as usize > max_items_usize {
                write_response(file, RESPONSE_ERROR, 0, &[], &[])?;
                return Err("range count exceeds max_items".to_string());
            }
            let mut packed = Vec::new();
            packed.extend_from_slice(&count.to_le_bytes());
            for index in 0..count as usize {
                let key_len = lengths_key[index] as usize;
                let val_len = lengths_val[index] as usize;
                if key_len > key_stride || val_len > val_stride {
                    write_response(file, RESPONSE_ERROR, 0, &[], &[])?;
                    return Err("range item exceeds stride".to_string());
                }
                packed.extend_from_slice(&(key_len as u64).to_le_bytes());
                packed.extend_from_slice(&(val_len as u64).to_le_bytes());
                packed
                    .extend_from_slice(&keys_out[index * key_stride..index * key_stride + key_len]);
                packed
                    .extend_from_slice(&vals_out[index * val_stride..index * val_stride + val_len]);
            }
            write_response(file, RESPONSE_OK, remaining as u32, &[], &packed)
        }
        STATUS_INVALID => {
            write_response(file, RESPONSE_ERROR, 0, &[], &[])?;
            Err("range returned INVALID".to_string())
        }
        status => {
            write_response(file, RESPONSE_ERROR, 0, &[], &[])?;
            Err(format!("range returned invalid ABI status {status}"))
        }
    }
}

fn read_request(
    file: &mut File,
    max_key_size: usize,
    max_value_size: usize,
) -> Result<Option<Request>, String> {
    let mut header = [0_u8; 16];
    match file.read(&mut header[..1]) {
        Ok(0) => return Ok(None),
        Ok(1) => {}
        Ok(_) => unreachable!(),
        Err(error) if error.kind() == ErrorKind::Interrupted => {
            return read_request(file, max_key_size, max_value_size)
        }
        Err(error) => return Err(format!("read request header: {error}")),
    }
    file.read_exact(&mut header[1..])
        .map_err(|error| format!("read request header: {error}"))?;
    let operation = u32::from_le_bytes(header[..4].try_into().expect("operation field"));
    let key_len = u32::from_le_bytes(header[4..8].try_into().expect("key length")) as usize;
    let value_len = u32::from_le_bytes(header[8..12].try_into().expect("value length")) as usize;
    let extra = u32::from_le_bytes(header[12..].try_into().expect("extra field"));
    if key_len > max_key_size {
        return Err(format!(
            "request key length {key_len} exceeds maximum {max_key_size}"
        ));
    }
    if value_len > max_value_size && operation != OPERATION_RANGE {
        return Err(format!(
            "request value length {value_len} exceeds maximum {max_value_size}"
        ));
    }
    if operation == OPERATION_RANGE && value_len > max_key_size {
        return Err(format!(
            "range end length {value_len} exceeds maximum key size {max_key_size}"
        ));
    }
    let mut key = vec![0_u8; key_len];
    file.read_exact(&mut key)
        .map_err(|error| format!("read request key: {error}"))?;
    let mut value = vec![0_u8; value_len];
    file.read_exact(&mut value)
        .map_err(|error| format!("read request value: {error}"))?;
    Ok(Some(Request {
        operation,
        extra,
        key,
        value,
    }))
}

fn write_response(
    file: &mut File,
    status: u32,
    extra: u32,
    key: &[u8],
    value: &[u8],
) -> Result<(), String> {
    let key_len = u32::try_from(key.len())
        .map_err(|_| "response key does not fit in the protocol length field".to_string())?;
    let value_len = u32::try_from(value.len())
        .map_err(|_| "response value does not fit in the protocol length field".to_string())?;
    let mut header = [0_u8; 16];
    header[..4].copy_from_slice(&status.to_le_bytes());
    header[4..8].copy_from_slice(&key_len.to_le_bytes());
    header[8..12].copy_from_slice(&value_len.to_le_bytes());
    header[12..].copy_from_slice(&extra.to_le_bytes());
    file.write_all(&header)
        .map_err(|error| format!("write response header: {error}"))?;
    file.write_all(key)
        .map_err(|error| format!("write response key: {error}"))?;
    file.write_all(value)
        .map_err(|error| format!("write response value: {error}"))?;
    Ok(())
}
