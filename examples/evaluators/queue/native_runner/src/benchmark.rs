use crate::abi::{Api, Consumer, Producer, STATUS_EMPTY, STATUS_FULL, STATUS_OK};
use crate::value::{prepare_payload, validate_payload};
use std::fs;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Barrier, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

const CLOCK_CHECK_INTERVAL: u64 = 64;

#[cfg(target_os = "macos")]
fn configure_benchmark_thread() -> Result<(), String> {
    const QOS_CLASS_USER_INTERACTIVE: u32 = 0x21;

    extern "C" {
        fn pthread_set_qos_class_self_np(qos_class: u32, relative_priority: i32) -> i32;
    }

    let status = unsafe { pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0) };
    if status == 0 {
        Ok(())
    } else {
        Err(format!("set benchmark thread QoS: errno {status}"))
    }
}

#[cfg(not(target_os = "macos"))]
fn configure_benchmark_thread() -> Result<(), String> {
    Ok(())
}

#[derive(Clone)]
pub struct BenchmarkConfig {
    pub scenario: String,
    pub capacity: u64,
    pub value_size: usize,
    pub producer_count: u32,
    pub consumer_count: u32,
    pub warmup: Duration,
    pub duration: Duration,
}

#[derive(Default, Clone, Copy)]
struct Counts {
    enqueued: u64,
    full: u64,
    dequeued: u64,
    empty: u64,
    enqueued_fingerprint: [u64; 2],
    dequeued_fingerprint: [u64; 2],
}

impl Counts {
    fn add(&mut self, other: Counts) {
        self.enqueued = self.enqueued.wrapping_add(other.enqueued);
        self.full = self.full.wrapping_add(other.full);
        self.dequeued = self.dequeued.wrapping_add(other.dequeued);
        self.empty = self.empty.wrapping_add(other.empty);
        for index in 0..2 {
            self.enqueued_fingerprint[index] =
                self.enqueued_fingerprint[index].wrapping_add(other.enqueued_fingerprint[index]);
            self.dequeued_fingerprint[index] =
                self.dequeued_fingerprint[index].wrapping_add(other.dequeued_fingerprint[index]);
        }
    }
}

struct PhaseResult {
    counts: Counts,
    elapsed: Duration,
}

pub fn run_benchmark(api: Api, config: BenchmarkConfig, output_path: &Path) -> Result<(), String> {
    if !config.warmup.is_zero() {
        run_phase(&api, &config, config.warmup)?;
    }
    let result = run_phase(&api, &config, config.duration)?;
    let successful = result.counts.enqueued.wrapping_add(result.counts.dequeued);
    let attempts = successful
        .wrapping_add(result.counts.full)
        .wrapping_add(result.counts.empty);
    let elapsed = result.elapsed.as_secs_f64();
    let json = format!(
        concat!(
            "{{\n",
            "  \"scenario\": \"{}\",\n",
            "  \"enqueued\": {},\n",
            "  \"dropped\": {},\n",
            "  \"dequeued\": {},\n",
            "  \"empty\": {},\n",
            "  \"attempts\": {},\n",
            "  \"duration\": {:.9},\n",
            "  \"total_ops_per_sec\": {:.6},\n",
            "  \"producers\": {},\n",
            "  \"consumers\": {}\n",
            "}}\n"
        ),
        config.scenario,
        result.counts.enqueued,
        result.counts.full,
        result.counts.dequeued,
        result.counts.empty,
        attempts,
        elapsed,
        successful as f64 / elapsed,
        config.producer_count,
        config.consumer_count,
    );
    fs::write(output_path, json)
        .map_err(|error| format!("write benchmark result {}: {error}", output_path.display()))
}

fn run_phase(
    api: &Api,
    config: &BenchmarkConfig,
    duration: Duration,
) -> Result<PhaseResult, String> {
    let queue = api.create_queue(
        config.capacity,
        config.value_size as u64,
        config.producer_count,
        config.consumer_count,
    )?;
    let producers = (0..config.producer_count)
        .map(|id| queue.create_producer(id))
        .collect::<Result<Vec<_>, _>>()?;
    let consumers = (0..config.consumer_count)
        .map(|id| queue.create_consumer(id))
        .collect::<Result<Vec<_>, _>>()?;
    let keys = fingerprint_keys();
    let barrier = Arc::new(Barrier::new(
        config.producer_count as usize + config.consumer_count as usize + 1,
    ));
    let start = Arc::new(OnceLock::new());
    let stop = Arc::new(AtomicBool::new(false));

    let (mut producers, mut consumers, mut counts, elapsed) = thread::scope(|scope| {
        let mut producer_workers = Vec::with_capacity(producers.len());
        for (lane, producer) in producers.into_iter().enumerate() {
            let barrier = barrier.clone();
            let start = start.clone();
            let stop = stop.clone();
            let value_size = config.value_size;
            producer_workers.push(scope.spawn(move || {
                run_producer(
                    producer, lane, value_size, duration, keys, barrier, start, stop,
                )
            }));
        }

        let mut consumer_workers = Vec::with_capacity(consumers.len());
        for consumer in consumers {
            let barrier = barrier.clone();
            let start = start.clone();
            let stop = stop.clone();
            let value_size = config.value_size;
            consumer_workers.push(scope.spawn(move || {
                run_consumer(consumer, value_size, duration, keys, barrier, start, stop)
            }));
        }

        start
            .set(Instant::now())
            .map_err(|_| "benchmark start was already initialized".to_string())?;
        barrier.wait();

        let mut returned_producers = Vec::with_capacity(producer_workers.len());
        let mut returned_consumers = Vec::with_capacity(consumer_workers.len());
        let mut counts = Counts::default();
        let mut first_error = None;
        for worker in producer_workers {
            match worker.join() {
                Ok(Ok((producer, local))) => {
                    returned_producers.push(producer);
                    counts.add(local);
                }
                Ok(Err(error)) => {
                    first_error.get_or_insert(error);
                }
                Err(_) => {
                    first_error.get_or_insert("producer thread panicked".to_string());
                }
            };
        }
        for worker in consumer_workers {
            match worker.join() {
                Ok(Ok((consumer, local))) => {
                    returned_consumers.push(consumer);
                    counts.add(local);
                }
                Ok(Err(error)) => {
                    first_error.get_or_insert(error);
                }
                Err(_) => {
                    first_error.get_or_insert("consumer thread panicked".to_string());
                }
            };
        }
        let elapsed = start.get().expect("benchmark start missing").elapsed();
        if let Some(error) = first_error {
            return Err(error);
        }
        Ok((returned_producers, returned_consumers, counts, elapsed))
    })?;

    let first_consumer = consumers
        .first_mut()
        .ok_or_else(|| "benchmark has no consumer for final drain".to_string())?;
    drain_and_validate(
        first_consumer,
        config.capacity,
        config.value_size,
        keys,
        &mut counts,
    )?;

    consumers.clear();
    producers.clear();
    drop(queue);
    Ok(PhaseResult { counts, elapsed })
}

#[allow(clippy::too_many_arguments)]
fn run_producer(
    mut producer: Producer,
    lane: usize,
    value_size: usize,
    duration: Duration,
    keys: [u64; 2],
    barrier: Arc<Barrier>,
    start: Arc<OnceLock<Instant>>,
    stop: Arc<AtomicBool>,
) -> Result<(Producer, Counts), String> {
    let mut counts = Counts::default();
    let mut payload = vec![0_u8; value_size];
    let mut sequence = 0_u64;
    prepare_payload(&mut payload, (lane as u64) << 56);
    let thread_configuration = configure_benchmark_thread();
    barrier.wait();
    thread_configuration?;
    let deadline = *start.get().expect("benchmark start missing") + duration;
    let mut attempts = 0_u64;
    while !stop.load(Ordering::Relaxed) {
        if clock_check_due(attempts) && Instant::now() >= deadline {
            break;
        }
        sequence = sequence.wrapping_add(1);
        let value = ((lane as u64) << 56) | (sequence & 0x00ff_ffff_ffff_ffff);
        payload[..8].copy_from_slice(&value.to_le_bytes());
        match producer.enqueue(&payload) {
            STATUS_OK => {
                counts.enqueued = counts.enqueued.wrapping_add(1);
                add_fingerprint(&mut counts.enqueued_fingerprint, keys, value);
            }
            STATUS_FULL => counts.full = counts.full.wrapping_add(1),
            status => {
                stop.store(true, Ordering::Relaxed);
                return Err(format!("enqueue returned invalid ABI status {status}"));
            }
        }
        attempts = attempts.wrapping_add(1);
    }
    Ok((producer, counts))
}

#[allow(clippy::too_many_arguments)]
fn run_consumer(
    mut consumer: Consumer,
    value_size: usize,
    duration: Duration,
    keys: [u64; 2],
    barrier: Arc<Barrier>,
    start: Arc<OnceLock<Instant>>,
    stop: Arc<AtomicBool>,
) -> Result<(Consumer, Counts), String> {
    let mut counts = Counts::default();
    let mut output = vec![0_u8; value_size];
    let thread_configuration = configure_benchmark_thread();
    barrier.wait();
    thread_configuration?;
    let deadline = *start.get().expect("benchmark start missing") + duration;
    let mut attempts = 0_u64;
    while !stop.load(Ordering::Relaxed) {
        if clock_check_due(attempts) && Instant::now() >= deadline {
            break;
        }
        let (status, length) = consumer.dequeue(&mut output)?;
        match status {
            STATUS_OK => {
                if length != value_size {
                    stop.store(true, Ordering::Relaxed);
                    return Err(format!(
                        "dequeue returned {length} bytes, expected {value_size}"
                    ));
                }
                let value = validate_payload(&output[..length])?;
                counts.dequeued = counts.dequeued.wrapping_add(1);
                add_fingerprint(&mut counts.dequeued_fingerprint, keys, value);
            }
            STATUS_EMPTY => counts.empty = counts.empty.wrapping_add(1),
            status => {
                stop.store(true, Ordering::Relaxed);
                return Err(format!("dequeue returned invalid ABI status {status}"));
            }
        }
        attempts = attempts.wrapping_add(1);
    }
    Ok((consumer, counts))
}

fn drain_and_validate(
    consumer: &mut Consumer,
    capacity: u64,
    value_size: usize,
    keys: [u64; 2],
    counts: &mut Counts,
) -> Result<(), String> {
    let mut output = vec![0_u8; value_size];
    let mut drained = 0_u64;
    let mut drained_fingerprint = [0_u64; 2];
    loop {
        let (status, length) = consumer.dequeue(&mut output)?;
        match status {
            STATUS_OK => {
                if length != value_size {
                    return Err(format!(
                        "drain returned {length} bytes, expected {value_size}"
                    ));
                }
                drained = drained.wrapping_add(1);
                if drained > capacity {
                    return Err(format!("drained more than queue capacity {capacity}"));
                }
                let value = validate_payload(&output[..length])?;
                add_fingerprint(&mut drained_fingerprint, keys, value);
            }
            STATUS_EMPTY => break,
            status => return Err(format!("drain returned invalid ABI status {status}")),
        }
    }

    if counts.dequeued.wrapping_add(drained) != counts.enqueued {
        return Err(format!(
            "enqueue/dequeue conservation failed: {} successful enqueues, {} returned values",
            counts.enqueued,
            counts.dequeued.wrapping_add(drained)
        ));
    }
    for (index, drained) in drained_fingerprint.iter().enumerate() {
        if counts.dequeued_fingerprint[index].wrapping_add(*drained)
            != counts.enqueued_fingerprint[index]
        {
            return Err(
                "dequeued values do not match the successfully enqueued multiset".to_string(),
            );
        }
    }
    Ok(())
}

#[allow(unknown_lints, clippy::manual_is_multiple_of)]
fn clock_check_due(attempts: u64) -> bool {
    attempts % CLOCK_CHECK_INTERVAL == 0
}

fn fingerprint_keys() -> [u64; 2] {
    let mut bytes = [0_u8; 16];
    if let Ok(mut file) = std::fs::File::open("/dev/urandom") {
        use std::io::Read;
        if file.read_exact(&mut bytes).is_ok() {
            return [
                u64::from_le_bytes(bytes[..8].try_into().expect("first key")),
                u64::from_le_bytes(bytes[8..].try_into().expect("second key")),
            ];
        }
    }
    let fallback = Instant::now().elapsed().as_nanos() as u64;
    [fallback ^ 0x42d3_6d4f_a17c_9b21, !fallback]
}

fn mix_fingerprint(mut value: u64, key: u64) -> u64 {
    value = value.wrapping_add(key).wrapping_add(0x9e37_79b9_7f4a_7c15);
    value = (value ^ (value >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    value = (value ^ (value >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    value ^ (value >> 31)
}

fn add_fingerprint(target: &mut [u64; 2], keys: [u64; 2], value: u64) {
    for index in 0..2 {
        target[index] = target[index].wrapping_add(mix_fingerprint(value, keys[index]));
    }
}
