use crate::abi::{Api, Consumer, Producer, STATUS_EMPTY, STATUS_FULL, STATUS_OK};
use std::fs::File;
use std::io::{ErrorKind, Read, Write};
use std::os::fd::FromRawFd;
use std::thread;

const OPERATION_ENQUEUE: u32 = 1;
const OPERATION_DEQUEUE: u32 = 2;

const RESPONSE_ENQUEUED: u32 = 1;
const RESPONSE_FULL: u32 = 2;
const RESPONSE_VALUE: u32 = 3;
const RESPONSE_EMPTY: u32 = 4;
const RESPONSE_ERROR: u32 = 5;

struct Request {
    operation: u32,
    payload: Vec<u8>,
}

pub struct WorkerConfig {
    pub fd_base: i32,
    pub lane_count: usize,
    pub producer_count: u32,
    pub consumer_count: u32,
    pub mixed_lane: bool,
    pub capacity: u64,
    pub value_size: usize,
}

pub fn run_worker(api: Api, config: WorkerConfig) -> Result<(), String> {
    if config.lane_count == 0 {
        return Err("worker requires at least one lane".to_string());
    }
    if config.mixed_lane && config.lane_count != 1 {
        return Err("mixed correctness mode requires exactly one lane".to_string());
    }
    if !config.mixed_lane
        && config.lane_count != (config.producer_count + config.consumer_count) as usize
    {
        return Err("lane count does not match producer and consumer counts".to_string());
    }

    let queue = api.create_queue(
        config.capacity,
        config.value_size as u64,
        config.producer_count,
        config.consumer_count,
    )?;
    let mut producers = (0..config.producer_count)
        .map(|id| queue.create_producer(id))
        .collect::<Result<Vec<_>, _>>()?
        .into_iter()
        .map(Some)
        .collect::<Vec<_>>();
    let mut consumers = (0..config.consumer_count)
        .map(|id| queue.create_consumer(id))
        .collect::<Result<Vec<_>, _>>()?
        .into_iter()
        .map(Some)
        .collect::<Vec<_>>();

    let result = thread::scope(|scope| {
        let mut workers = Vec::with_capacity(config.lane_count);
        for lane in 0..config.lane_count {
            let producer = if config.mixed_lane || lane < config.producer_count as usize {
                let index = if config.mixed_lane { 0 } else { lane };
                producers[index].take()
            } else {
                None
            };
            let consumer = if config.mixed_lane || lane >= config.producer_count as usize {
                let index = if config.mixed_lane {
                    0
                } else {
                    lane - config.producer_count as usize
                };
                consumers[index].take()
            } else {
                None
            };
            let fd = config.fd_base + lane as i32;
            let value_size = config.value_size;
            workers.push(scope.spawn(move || {
                let file = unsafe { File::from_raw_fd(fd) };
                serve_lane(file, producer, consumer, value_size)
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
    drop(queue);
    result
}

fn serve_lane(
    mut file: File,
    mut producer: Option<Producer>,
    mut consumer: Option<Consumer>,
    value_size: usize,
) -> Result<(), String> {
    let mut output = vec![0_u8; value_size];
    while let Some(mut request) = read_request(&mut file, value_size)? {
        match request.operation {
            OPERATION_ENQUEUE => {
                let Some(producer) = producer.as_mut() else {
                    write_response(&mut file, RESPONSE_ERROR, &[])?;
                    return Err("enqueue sent to a consumer-only lane".to_string());
                };
                match producer.enqueue(&request.payload) {
                    STATUS_OK => {
                        request.payload.fill(0xa5);
                        write_response(&mut file, RESPONSE_ENQUEUED, &[])?;
                    }
                    STATUS_FULL => write_response(&mut file, RESPONSE_FULL, &[])?,
                    status => {
                        write_response(&mut file, RESPONSE_ERROR, &[])?;
                        return Err(format!("enqueue returned invalid ABI status {status}"));
                    }
                }
            }
            OPERATION_DEQUEUE => {
                let Some(consumer) = consumer.as_mut() else {
                    write_response(&mut file, RESPONSE_ERROR, &[])?;
                    return Err("dequeue sent to a producer-only lane".to_string());
                };
                let (status, length) = consumer.dequeue(&mut output)?;
                match status {
                    STATUS_OK => write_response(&mut file, RESPONSE_VALUE, &output[..length])?,
                    STATUS_EMPTY => write_response(&mut file, RESPONSE_EMPTY, &[])?,
                    status => {
                        write_response(&mut file, RESPONSE_ERROR, &[])?;
                        return Err(format!("dequeue returned invalid ABI status {status}"));
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

fn read_request(file: &mut File, max_value_size: usize) -> Result<Option<Request>, String> {
    let mut header = [0_u8; 16];
    match file.read(&mut header[..1]) {
        Ok(0) => return Ok(None),
        Ok(1) => {}
        Ok(_) => unreachable!(),
        Err(error) if error.kind() == ErrorKind::Interrupted => {
            return read_request(file, max_value_size)
        }
        Err(error) => return Err(format!("read request header: {error}")),
    }
    file.read_exact(&mut header[1..])
        .map_err(|error| format!("read request header: {error}"))?;
    let operation = u32::from_le_bytes(header[..4].try_into().expect("operation field"));
    let length = u32::from_le_bytes(header[4..8].try_into().expect("length field")) as usize;
    let reserved = u64::from_le_bytes(header[8..].try_into().expect("reserved field"));
    if reserved != 0 {
        return Err(format!("request reserved field is {reserved}, want zero"));
    }
    if length > max_value_size {
        return Err(format!(
            "request payload length {length} exceeds maximum {max_value_size}"
        ));
    }
    if operation == OPERATION_DEQUEUE && length != 0 {
        return Err("dequeue request contains a payload".to_string());
    }
    let mut payload = vec![0_u8; length];
    file.read_exact(&mut payload)
        .map_err(|error| format!("read request payload: {error}"))?;
    Ok(Some(Request { operation, payload }))
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
