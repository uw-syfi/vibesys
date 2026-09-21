use crate::abi::{Api, Client, STATUS_MISSING, STATUS_OK};
use crate::value::{prepare_payload, validate_payload};
use std::fs;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Barrier, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

const CLOCK_CHECK_INTERVAL: u64 = 64;
const WRITER_REMOVE_INTERVAL: u64 = 5;

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
    pub max_key_size: usize,
    pub max_value_size: usize,
    pub client_count: u32,
    pub key_space: u64,
    pub warmup: Duration,
    pub duration: Duration,
}

#[derive(Default, Clone, Copy)]
struct Counts {
    puts: u64,
    gets: u64,
    removes: u64,
    hits: u64,
    misses: u64,
}

impl Counts {
    fn add(&mut self, other: Counts) {
        self.puts = self.puts.wrapping_add(other.puts);
        self.gets = self.gets.wrapping_add(other.gets);
        self.removes = self.removes.wrapping_add(other.removes);
        self.hits = self.hits.wrapping_add(other.hits);
        self.misses = self.misses.wrapping_add(other.misses);
    }

    fn total_ops(self) -> u64 {
        self.puts.wrapping_add(self.gets).wrapping_add(self.removes)
    }
}

struct PhaseResult {
    counts: Counts,
    elapsed: Duration,
}

enum ClientRole {
    Writer,
    Reader,
    Mixed,
}

pub fn run_benchmark(api: Api, config: BenchmarkConfig, output_path: &Path) -> Result<(), String> {
    if config.key_space == 0 {
        return Err("key space must be greater than zero".to_string());
    }
    if !config.warmup.is_zero() {
        run_phase(&api, &config, config.warmup)?;
    }
    let result = run_phase(&api, &config, config.duration)?;
    let total = result.counts.total_ops();
    let elapsed = result.elapsed.as_secs_f64();
    if result.counts.hits.wrapping_add(result.counts.misses)
        != result.counts.gets.wrapping_add(result.counts.removes)
    {
        return Err("benchmark reported inconsistent hit and miss counts".to_string());
    }
    let json = format!(
        concat!(
            "{{\n",
            "  \"scenario\": \"{}\",\n",
            "  \"duration\": {:.9},\n",
            "  \"total_ops_per_sec\": {:.6},\n",
            "  \"clients\": {},\n",
            "  \"puts\": {},\n",
            "  \"gets\": {},\n",
            "  \"removes\": {},\n",
            "  \"hits\": {},\n",
            "  \"misses\": {}\n",
            "}}\n"
        ),
        config.scenario,
        elapsed,
        total as f64 / elapsed,
        config.client_count,
        result.counts.puts,
        result.counts.gets,
        result.counts.removes,
        result.counts.hits,
        result.counts.misses,
    );
    fs::write(output_path, json)
        .map_err(|error| format!("write benchmark result {}: {error}", output_path.display()))
}

fn run_phase(
    api: &Api,
    config: &BenchmarkConfig,
    duration: Duration,
) -> Result<PhaseResult, String> {
    let map = api.create_map(
        config.max_key_size as u64,
        config.max_value_size as u64,
        config.client_count,
    )?;
    let clients = (0..config.client_count)
        .map(|id| map.create_client(id))
        .collect::<Result<Vec<_>, _>>()?;
    let barrier = Arc::new(Barrier::new(config.client_count as usize + 1));
    let start = Arc::new(OnceLock::new());
    let stop = Arc::new(AtomicBool::new(false));

    let (mut clients, counts, elapsed) = thread::scope(|scope| {
        let mut workers = Vec::with_capacity(clients.len());
        for (lane, client) in clients.into_iter().enumerate() {
            let barrier = barrier.clone();
            let start = start.clone();
            let stop = stop.clone();
            let role = client_role(&config.scenario, lane, config.client_count as usize)?;
            let key_size = config.max_key_size;
            let value_size = config.max_value_size;
            let key_space = config.key_space;
            workers.push(scope.spawn(move || {
                run_client(
                    client, lane, role, key_size, value_size, key_space, duration, barrier, start,
                    stop,
                )
            }));
        }

        start
            .set(Instant::now())
            .map_err(|_| "benchmark start was already initialized".to_string())?;
        barrier.wait();

        let mut returned = Vec::with_capacity(workers.len());
        let mut counts = Counts::default();
        let mut first_error = None;
        for worker in workers {
            match worker.join() {
                Ok(Ok((client, local))) => {
                    returned.push(client);
                    counts.add(local);
                }
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
        Ok((returned, counts, elapsed))
    })?;

    clients.clear();
    drop(map);
    Ok(PhaseResult { counts, elapsed })
}

fn client_role(scenario: &str, lane: usize, client_count: usize) -> Result<ClientRole, String> {
    match scenario {
        "swmr" => {
            if client_count == 0 {
                return Err("swmr requires at least one client".to_string());
            }
            if lane == 0 {
                Ok(ClientRole::Writer)
            } else {
                Ok(ClientRole::Reader)
            }
        }
        "mw" => Ok(ClientRole::Mixed),
        other => Err(format!("unsupported benchmark scenario {other}")),
    }
}

#[allow(clippy::too_many_arguments)]
fn run_client(
    mut client: Client,
    lane: usize,
    role: ClientRole,
    key_size: usize,
    value_size: usize,
    key_space: u64,
    duration: Duration,
    barrier: Arc<Barrier>,
    start: Arc<OnceLock<Instant>>,
    stop: Arc<AtomicBool>,
) -> Result<(Client, Counts), String> {
    let mut counts = Counts::default();
    let mut key = vec![0_u8; key_size];
    let mut value = vec![0_u8; value_size];
    let mut output = vec![0_u8; value_size];
    let thread_configuration = configure_benchmark_thread(lane);
    barrier.wait();
    thread_configuration?;
    let deadline = *start.get().expect("benchmark start missing") + duration;
    let mut attempts = 0_u64;
    while !stop.load(Ordering::Relaxed) {
        if clock_check_due(attempts) && Instant::now() >= deadline {
            break;
        }
        let sequence = attempts.wrapping_add(1);
        let key_id = key_id(lane, sequence, key_space);
        prepare_payload(&mut key, key_id);
        match role {
            ClientRole::Writer => {
                if sequence % WRITER_REMOVE_INTERVAL == 0 {
                    lookup_op(&mut client, true, &key, &mut output, &mut counts, &stop)?;
                } else {
                    put_op(
                        &mut client,
                        &key,
                        &mut value,
                        key_id,
                        sequence,
                        &mut counts,
                        &stop,
                    )?;
                }
            }
            ClientRole::Reader => {
                lookup_op(&mut client, false, &key, &mut output, &mut counts, &stop)?;
            }
            ClientRole::Mixed => {
                // 70% get, 20% put, 10% remove.
                match sequence % 10 {
                    0 => lookup_op(&mut client, true, &key, &mut output, &mut counts, &stop)?,
                    1 | 2 => put_op(
                        &mut client,
                        &key,
                        &mut value,
                        key_id,
                        sequence,
                        &mut counts,
                        &stop,
                    )?,
                    _ => lookup_op(&mut client, false, &key, &mut output, &mut counts, &stop)?,
                }
            }
        }
        attempts = attempts.wrapping_add(1);
    }
    Ok((client, counts))
}

fn key_id(lane: usize, sequence: u64, key_space: u64) -> u64 {
    (sequence.wrapping_add((lane as u64).wrapping_mul(31))) % key_space
}

fn put_op(
    client: &mut Client,
    key: &[u8],
    value: &mut [u8],
    key_id: u64,
    sequence: u64,
    counts: &mut Counts,
    stop: &AtomicBool,
) -> Result<(), String> {
    prepare_payload(value, (key_id << 32) | (sequence & 0xffff_ffff));
    match client.put(key, value) {
        STATUS_OK => {
            counts.puts = counts.puts.wrapping_add(1);
            Ok(())
        }
        status => {
            stop.store(true, Ordering::Relaxed);
            Err(format!("put returned invalid ABI status {status}"))
        }
    }
}

fn lookup_op(
    client: &mut Client,
    remove: bool,
    key: &[u8],
    output: &mut [u8],
    counts: &mut Counts,
    stop: &AtomicBool,
) -> Result<(), String> {
    let (status, length) = client.lookup(remove, key, output)?;
    match status {
        STATUS_OK => {
            validate_payload(&output[..length])?;
            if remove {
                counts.removes = counts.removes.wrapping_add(1);
            } else {
                counts.gets = counts.gets.wrapping_add(1);
            }
            counts.hits = counts.hits.wrapping_add(1);
            Ok(())
        }
        STATUS_MISSING => {
            if remove {
                counts.removes = counts.removes.wrapping_add(1);
            } else {
                counts.gets = counts.gets.wrapping_add(1);
            }
            counts.misses = counts.misses.wrapping_add(1);
            Ok(())
        }
        status => {
            stop.store(true, Ordering::Relaxed);
            Err(format!("lookup returned invalid ABI status {status}"))
        }
    }
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
