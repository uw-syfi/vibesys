use crate::abi::{Api, Client, STATUS_MISSING, STATUS_OK};
use crate::value::{encode_key, prepare_payload};
use std::fs;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Barrier, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

const CLOCK_CHECK_INTERVAL: u64 = 64;
const KEY_UNIVERSE: u64 = 256;
const RANGE_MAX_ITEMS: u64 = 8;

#[cfg(target_os = "macos")]
fn configure_benchmark_thread(_worker_index: usize) -> Result<(), String> {
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

#[cfg(target_os = "linux")]
fn configure_benchmark_thread(worker_index: usize) -> Result<(), String> {
    pin_current_thread(worker_index)
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
fn configure_benchmark_thread(_worker_index: usize) -> Result<(), String> {
    Ok(())
}

#[cfg(target_os = "linux")]
fn pin_current_thread(worker_index: usize) -> Result<(), String> {
    const CPU_SETSIZE: usize = 1024;
    let mut allowed = [0_u64; CPU_SETSIZE / 64];
    let bytes = std::mem::size_of_val(&allowed);
    if unsafe { sched_getaffinity(0, bytes, allowed.as_mut_ptr()) } != 0 {
        return Err(format!(
            "sched_getaffinity: {}",
            std::io::Error::last_os_error()
        ));
    }
    let mut cpus = Vec::new();
    for cpu in 0..CPU_SETSIZE {
        if allowed[cpu / 64] & (1_u64 << (cpu % 64)) != 0 {
            cpus.push(cpu);
        }
    }
    if cpus.is_empty() {
        return Err("process CPU affinity mask is empty".to_string());
    }
    let cpu = cpus[worker_index % cpus.len()];
    let mut mask = [0_u64; CPU_SETSIZE / 64];
    mask[cpu / 64] |= 1_u64 << (cpu % 64);
    if unsafe { sched_setaffinity(0, bytes, mask.as_ptr()) } != 0 {
        return Err(format!(
            "sched_setaffinity cpu {cpu}: {}",
            std::io::Error::last_os_error()
        ));
    }
    Ok(())
}

#[cfg(target_os = "linux")]
extern "C" {
    fn sched_getaffinity(pid: i32, cpusetsize: usize, mask: *mut u64) -> i32;
    fn sched_setaffinity(pid: i32, cpusetsize: usize, mask: *const u64) -> i32;
}

#[derive(Clone)]
pub struct BenchmarkConfig {
    pub scenario: String,
    pub max_key_size: u64,
    pub max_value_size: u64,
    pub client_count: u32,
    pub warmup: Duration,
    pub duration: Duration,
}

#[derive(Default, Clone, Copy)]
struct Counts {
    puts: u64,
    gets: u64,
    removes: u64,
    successors: u64,
    ranges: u64,
    missing: u64,
    attempts: u64,
}

impl Counts {
    fn add(&mut self, other: Counts) {
        self.puts = self.puts.wrapping_add(other.puts);
        self.gets = self.gets.wrapping_add(other.gets);
        self.removes = self.removes.wrapping_add(other.removes);
        self.successors = self.successors.wrapping_add(other.successors);
        self.ranges = self.ranges.wrapping_add(other.ranges);
        self.missing = self.missing.wrapping_add(other.missing);
        self.attempts = self.attempts.wrapping_add(other.attempts);
    }

    fn completed(&self) -> u64 {
        self.puts
            .wrapping_add(self.gets)
            .wrapping_add(self.removes)
            .wrapping_add(self.successors)
            .wrapping_add(self.ranges)
            .wrapping_add(self.missing)
    }
}

struct PhaseResult {
    counts: Counts,
    elapsed: Duration,
}

enum Mix {
    Writer,
    Reader,
    Mixed,
}

pub fn run_benchmark(api: Api, config: BenchmarkConfig, output_path: &Path) -> Result<(), String> {
    if !config.warmup.is_zero() {
        run_phase(&api, &config, config.warmup)?;
    }
    let result = run_phase(&api, &config, config.duration)?;
    let successful = result.counts.completed();
    let elapsed = result.elapsed.as_secs_f64();
    let writers = writer_count(&config.scenario, config.client_count)?;
    let json = format!(
        concat!(
            "{{\n",
            "  \"scenario\": \"{}\",\n",
            "  \"puts\": {},\n",
            "  \"gets\": {},\n",
            "  \"removes\": {},\n",
            "  \"successors\": {},\n",
            "  \"ranges\": {},\n",
            "  \"missing\": {},\n",
            "  \"attempts\": {},\n",
            "  \"duration\": {:.9},\n",
            "  \"total_ops_per_sec\": {:.6},\n",
            "  \"clients\": {},\n",
            "  \"writers\": {},\n",
            "  \"readers\": {}\n",
            "}}\n"
        ),
        config.scenario,
        result.counts.puts,
        result.counts.gets,
        result.counts.removes,
        result.counts.successors,
        result.counts.ranges,
        result.counts.missing,
        result.counts.attempts,
        elapsed,
        successful as f64 / elapsed,
        config.client_count,
        writers,
        config.client_count - writers,
    );
    fs::write(output_path, json)
        .map_err(|error| format!("write benchmark result {}: {error}", output_path.display()))
}

fn writer_count(scenario: &str, client_count: u32) -> Result<u32, String> {
    match scenario {
        "swmr" => Ok(1),
        "mw" => Ok(client_count),
        _ => Err(format!("unsupported scenario {scenario:?}")),
    }
}

fn mix_for(scenario: &str, client_id: u32) -> Result<Mix, String> {
    match scenario {
        "swmr" if client_id == 0 => Ok(Mix::Writer),
        "swmr" => Ok(Mix::Reader),
        "mw" => Ok(Mix::Mixed),
        _ => Err(format!("unsupported scenario {scenario:?}")),
    }
}

fn run_phase(
    api: &Api,
    config: &BenchmarkConfig,
    duration: Duration,
) -> Result<PhaseResult, String> {
    let map = api.create_map(
        config.max_key_size,
        config.max_value_size,
        config.client_count,
    )?;
    let clients = (0..config.client_count)
        .map(|id| map.create_client(id))
        .collect::<Result<Vec<_>, _>>()?;
    let barrier = Arc::new(Barrier::new(config.client_count as usize + 1));
    let start = Arc::new(OnceLock::new());
    let stop = Arc::new(AtomicBool::new(false));

    let (counts, elapsed) = thread::scope(|scope| {
        let mut workers = Vec::with_capacity(clients.len());
        for (lane, client) in clients.into_iter().enumerate() {
            let barrier = barrier.clone();
            let start = start.clone();
            let stop = stop.clone();
            let mix = mix_for(&config.scenario, lane as u32)?;
            let max_key_size = config.max_key_size as usize;
            let max_value_size = config.max_value_size as usize;
            workers.push(scope.spawn(move || {
                run_client(
                    client,
                    lane as u32,
                    mix,
                    max_key_size,
                    max_value_size,
                    duration,
                    barrier,
                    start,
                    stop,
                )
            }));
        }

        start
            .set(Instant::now())
            .map_err(|_| "benchmark start was already initialized".to_string())?;
        barrier.wait();

        let mut counts = Counts::default();
        let mut first_error = None;
        for worker in workers {
            match worker.join() {
                Ok(Ok(local)) => counts.add(local),
                Ok(Err(error)) => {
                    first_error.get_or_insert(error);
                }
                Err(_) => {
                    first_error.get_or_insert("benchmark thread panicked".to_string());
                }
            };
        }
        let elapsed = start.get().expect("benchmark start missing").elapsed();
        if let Some(error) = first_error {
            return Err(error);
        }
        Ok((counts, elapsed))
    })?;

    drop(map);
    Ok(PhaseResult { counts, elapsed })
}

#[allow(clippy::too_many_arguments)]
fn run_client(
    mut client: Client,
    lane: u32,
    mix: Mix,
    max_key_size: usize,
    max_value_size: usize,
    duration: Duration,
    barrier: Arc<Barrier>,
    start: Arc<OnceLock<Instant>>,
    stop: Arc<AtomicBool>,
) -> Result<Counts, String> {
    let mut counts = Counts::default();
    let mut value = vec![0_u8; max_value_size];
    let mut output = vec![0_u8; max_value_size];
    let mut key_out = vec![0_u8; max_key_size];
    let mut val_out = vec![0_u8; max_value_size];
    let key_stride = max_key_size;
    let val_stride = max_value_size;
    let mut range_keys = vec![0_u8; key_stride * RANGE_MAX_ITEMS as usize];
    let mut range_vals = vec![0_u8; val_stride * RANGE_MAX_ITEMS as usize];
    let mut lengths_key = vec![0_u64; RANGE_MAX_ITEMS as usize];
    let mut lengths_val = vec![0_u64; RANGE_MAX_ITEMS as usize];
    prepare_payload(&mut value, (lane as u64) << 56);
    let thread_configuration = configure_benchmark_thread(lane as usize);
    barrier.wait();
    thread_configuration?;
    let deadline = *start.get().expect("benchmark start missing") + duration;
    let mut attempts = 0_u64;
    while !stop.load(Ordering::Relaxed) {
        if clock_check_due(attempts) && Instant::now() >= deadline {
            break;
        }
        let slot = (lane as u64)
            .wrapping_mul(0x9e37_79b9_7f4a_7c15)
            .wrapping_add(attempts)
            % KEY_UNIVERSE;
        let key = encode_key(slot, max_key_size);
        let choice = attempts % 100;
        let outcome = match mix {
            Mix::Writer if choice < 70 => put_op(&mut client, &key, &value, &mut counts),
            Mix::Writer => remove_op(&mut client, &key, &mut output, &mut counts),
            Mix::Reader if choice < 70 => get_op(&mut client, &key, &mut output, &mut counts),
            Mix::Reader if choice < 85 => {
                successor_op(&mut client, &key, &mut key_out, &mut val_out, &mut counts)
            }
            Mix::Reader => range_op(
                &mut client,
                &key,
                max_key_size,
                key_stride,
                val_stride,
                &mut range_keys,
                &mut range_vals,
                &mut lengths_key,
                &mut lengths_val,
                &mut counts,
            ),
            Mix::Mixed if choice < 30 => get_op(&mut client, &key, &mut output, &mut counts),
            Mix::Mixed if choice < 55 => put_op(&mut client, &key, &value, &mut counts),
            Mix::Mixed if choice < 70 => remove_op(&mut client, &key, &mut output, &mut counts),
            Mix::Mixed if choice < 85 => {
                successor_op(&mut client, &key, &mut key_out, &mut val_out, &mut counts)
            }
            Mix::Mixed => range_op(
                &mut client,
                &key,
                max_key_size,
                key_stride,
                val_stride,
                &mut range_keys,
                &mut range_vals,
                &mut lengths_key,
                &mut lengths_val,
                &mut counts,
            ),
        };
        if let Err(error) = outcome {
            stop.store(true, Ordering::Relaxed);
            return Err(error);
        }
        attempts = attempts.wrapping_add(1);
        counts.attempts = counts.attempts.wrapping_add(1);
    }
    Ok(counts)
}

fn put_op(
    client: &mut Client,
    key: &[u8],
    value: &[u8],
    counts: &mut Counts,
) -> Result<(), String> {
    match client.put(key, value) {
        STATUS_OK => {
            counts.puts = counts.puts.wrapping_add(1);
            Ok(())
        }
        status => Err(format!("put returned invalid ABI status {status}")),
    }
}

fn get_op(
    client: &mut Client,
    key: &[u8],
    output: &mut [u8],
    counts: &mut Counts,
) -> Result<(), String> {
    record_lookup(client.lookup(false, key, output)?, false, counts)
}

fn remove_op(
    client: &mut Client,
    key: &[u8],
    output: &mut [u8],
    counts: &mut Counts,
) -> Result<(), String> {
    record_lookup(client.lookup(true, key, output)?, true, counts)
}

fn record_lookup(result: (u32, usize), remove: bool, counts: &mut Counts) -> Result<(), String> {
    match result.0 {
        STATUS_OK => {
            if remove {
                counts.removes = counts.removes.wrapping_add(1);
            } else {
                counts.gets = counts.gets.wrapping_add(1);
            }
            Ok(())
        }
        STATUS_MISSING => {
            counts.missing = counts.missing.wrapping_add(1);
            Ok(())
        }
        status => Err(format!("lookup returned invalid ABI status {status}")),
    }
}

fn successor_op(
    client: &mut Client,
    key: &[u8],
    key_out: &mut [u8],
    val_out: &mut [u8],
    counts: &mut Counts,
) -> Result<(), String> {
    let mut key_len = 0;
    let mut val_len = 0;
    let status = client.neighbor_raw(
        true,
        key,
        key_out.as_mut_ptr(),
        key_out.len() as u64,
        &mut key_len,
        val_out.as_mut_ptr(),
        val_out.len() as u64,
        &mut val_len,
    );
    match status {
        STATUS_OK => {
            counts.successors = counts.successors.wrapping_add(1);
            Ok(())
        }
        STATUS_MISSING => {
            counts.missing = counts.missing.wrapping_add(1);
            Ok(())
        }
        status => Err(format!("successor returned invalid ABI status {status}")),
    }
}

#[allow(clippy::too_many_arguments)]
fn range_op(
    client: &mut Client,
    start: &[u8],
    max_key_size: usize,
    key_stride: usize,
    val_stride: usize,
    keys_out: &mut [u8],
    vals_out: &mut [u8],
    lengths_key: &mut [u64],
    lengths_val: &mut [u64],
    counts: &mut Counts,
) -> Result<(), String> {
    let mut end = encode_key(
        (u64::from_le_bytes(pad_slot(start)) + 16) % KEY_UNIVERSE,
        max_key_size,
    );
    if end.as_slice() <= start {
        end = encode_key(KEY_UNIVERSE.saturating_sub(1), max_key_size);
        if end.as_slice() <= start {
            end = start.to_vec();
            if let Some(last) = end.last_mut() {
                *last = last.saturating_add(1);
            }
        }
    }
    let mut count = 0;
    let mut remaining = 0;
    let status = client.range_raw(
        start,
        &end,
        keys_out.as_mut_ptr(),
        key_stride as u64,
        vals_out.as_mut_ptr(),
        val_stride as u64,
        lengths_key.as_mut_ptr(),
        lengths_val.as_mut_ptr(),
        RANGE_MAX_ITEMS,
        &mut count,
        &mut remaining,
    );
    match status {
        STATUS_OK => {
            counts.ranges = counts.ranges.wrapping_add(1);
            Ok(())
        }
        status => Err(format!("range returned invalid ABI status {status}")),
    }
}

fn pad_slot(key: &[u8]) -> [u8; 8] {
    let mut bytes = [0_u8; 8];
    let copy = key.len().min(8);
    bytes[..copy].copy_from_slice(&key[..copy]);
    bytes
}

#[allow(unknown_lints, clippy::manual_is_multiple_of)]
fn clock_check_due(attempts: u64) -> bool {
    attempts % CLOCK_CHECK_INTERVAL == 0
}

#[cfg(all(test, target_os = "linux"))]
mod pin_tests {
    use super::*;

    const CPU_SETSIZE: usize = 1024;

    fn affinity_mask() -> [u64; CPU_SETSIZE / 64] {
        let mut mask = [0_u64; CPU_SETSIZE / 64];
        let bytes = std::mem::size_of_val(&mask);
        assert_eq!(
            unsafe { sched_getaffinity(0, bytes, mask.as_mut_ptr()) },
            0,
            "sched_getaffinity: {}",
            std::io::Error::last_os_error()
        );
        mask
    }

    fn cpus_in(mask: &[u64; CPU_SETSIZE / 64]) -> Vec<usize> {
        (0..CPU_SETSIZE)
            .filter(|cpu| mask[cpu / 64] & (1_u64 << (cpu % 64)) != 0)
            .collect()
    }

    fn set_affinity(mask: &[u64; CPU_SETSIZE / 64]) {
        let bytes = std::mem::size_of_val(mask);
        assert_eq!(
            unsafe { sched_setaffinity(0, bytes, mask.as_ptr()) },
            0,
            "sched_setaffinity: {}",
            std::io::Error::last_os_error()
        );
    }

    struct RestoreAffinity([u64; CPU_SETSIZE / 64]);

    impl Drop for RestoreAffinity {
        fn drop(&mut self) {
            set_affinity(&self.0);
        }
    }

    #[test]
    fn pins_current_thread_to_indexed_cpu_from_process_mask() {
        let original = affinity_mask();
        let _restore = RestoreAffinity(original);
        let allowed = cpus_in(&original);
        assert!(!allowed.is_empty());
        let indexes: Vec<usize> = if allowed.len() == 1 {
            vec![0]
        } else {
            vec![0, 1]
        };
        for worker_index in indexes {
            set_affinity(&original);
            pin_current_thread(worker_index).expect("pin worker");
            assert_eq!(
                cpus_in(&affinity_mask()),
                vec![allowed[worker_index % allowed.len()]]
            );
        }
    }
}
