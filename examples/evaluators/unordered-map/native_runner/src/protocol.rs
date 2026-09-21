use crate::abi::{Api, Client, STATUS_INVALID, STATUS_MISSING, STATUS_OK};
use std::fs::File;
use std::io::{ErrorKind, Read, Write};
use std::os::fd::FromRawFd;
use std::thread;

const OPERATION_PUT: u32 = 1;
const OPERATION_GET: u32 = 2;
const OPERATION_REMOVE: u32 = 3;

const RESPONSE_OK: u32 = 1;
const RESPONSE_MISSING: u32 = 2;
const RESPONSE_INVALID: u32 = 3;
const RESPONSE_ERROR: u32 = 4;

struct Request {
    operation: u32,
    key: Vec<u8>,
    value: Vec<u8>,
}

pub struct WorkerConfig {
    pub fd_base: i32,
    pub lane_count: usize,
    pub client_count: u32,
    pub mixed_lane: bool,
    pub max_key_size: usize,
    pub max_value_size: usize,
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
        config.max_key_size as u64,
        config.max_value_size as u64,
        config.client_count,
    )?;
    let mut clients = (0..config.client_count)
        .map(|id| map.create_client(id))
        .collect::<Result<Vec<_>, _>>()?
        .into_iter()
        .map(Some)
        .collect::<Vec<_>>();

    let result = thread::scope(|scope| {
        let mut workers = Vec::with_capacity(config.lane_count);
        for lane in 0..config.lane_count {
            let index = if config.mixed_lane { 0 } else { lane };
            let client = clients[index]
                .take()
                .expect("each lane receives one client handle");
            let fd = config.fd_base + lane as i32;
            let max_key_size = config.max_key_size;
            let max_value_size = config.max_value_size;
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
    let mut output = vec![0_u8; max_value_size];
    while let Some(mut request) = read_request(&mut file, max_key_size, max_value_size)? {
        match request.operation {
            OPERATION_PUT => match client.put(&request.key, &request.value) {
                STATUS_OK => {
                    request.key.fill(0xa5);
                    request.value.fill(0xa5);
                    write_response(&mut file, RESPONSE_OK, &[])?;
                }
                STATUS_INVALID => write_response(&mut file, RESPONSE_INVALID, &[])?,
                status => {
                    write_response(&mut file, RESPONSE_ERROR, &[])?;
                    return Err(format!("put returned invalid ABI status {status}"));
                }
            },
            OPERATION_GET | OPERATION_REMOVE => {
                let remove = request.operation == OPERATION_REMOVE;
                let (status, length) = client.lookup(remove, &request.key, &mut output)?;
                match status {
                    STATUS_OK => write_response(&mut file, RESPONSE_OK, &output[..length])?,
                    STATUS_MISSING => write_response(&mut file, RESPONSE_MISSING, &[])?,
                    STATUS_INVALID => write_response(&mut file, RESPONSE_INVALID, &[])?,
                    status => {
                        write_response(&mut file, RESPONSE_ERROR, &[])?;
                        return Err(format!("lookup returned invalid ABI status {status}"));
                    }
                }
            }
            operation => {
                write_response(&mut file, RESPONSE_ERROR, &[])?;
                return Err(format!("unknown operation {operation}"));
            }
        }
    }
    Ok(())
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
    let key_len = u32::from_le_bytes(header[4..8].try_into().expect("key length field")) as usize;
    let value_len =
        u32::from_le_bytes(header[8..12].try_into().expect("value length field")) as usize;
    let reserved = u32::from_le_bytes(header[12..].try_into().expect("reserved field"));
    if reserved != 0 {
        return Err(format!("request reserved field is {reserved}, want zero"));
    }
    if key_len > max_key_size {
        return Err(format!(
            "request key length {key_len} exceeds maximum {max_key_size}"
        ));
    }
    if value_len > max_value_size {
        return Err(format!(
            "request value length {value_len} exceeds maximum {max_value_size}"
        ));
    }
    if operation != OPERATION_PUT && value_len != 0 {
        return Err("get/remove request contains a value payload".to_string());
    }
    let mut key = vec![0_u8; key_len];
    file.read_exact(&mut key)
        .map_err(|error| format!("read request key: {error}"))?;
    let mut value = vec![0_u8; value_len];
    file.read_exact(&mut value)
        .map_err(|error| format!("read request value: {error}"))?;
    Ok(Some(Request {
        operation,
        key,
        value,
    }))
}

fn write_response(file: &mut File, status: u32, payload: &[u8]) -> Result<(), String> {
    let length = u32::try_from(payload.len())
        .map_err(|_| "response payload does not fit in the protocol length field".to_string())?;
    let mut header = [0_u8; 16];
    header[..4].copy_from_slice(&status.to_le_bytes());
    header[4..8].copy_from_slice(&length.to_le_bytes());
    file.write_all(&header)
        .map_err(|error| format!("write response header: {error}"))?;
    file.write_all(payload)
        .map_err(|error| format!("write response payload: {error}"))?;
    Ok(())
}
