use std::env;
use std::process::ExitCode;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Barrier};
use std::thread;
use std::time::{Duration, Instant};
use ordered_map_verus::OrderedMap;

fn benchmark(duration: Duration, clients: usize, key_space: u64) -> Result<(), String> {
    if clients == 0 || key_space == 0 {
        return Err("clients and key-space must be positive".to_owned());
    }

    let map = Arc::new(OrderedMap::new());
    let stop = Arc::new(AtomicBool::new(false));
    let start = Arc::new(Barrier::new(clients + 1));
    let mut workers = Vec::with_capacity(clients);

    for lane in 0..clients {
        let map = Arc::clone(&map);
        let stop = Arc::clone(&stop);
        let start = Arc::clone(&start);
        workers.push(thread::spawn(move || {
            let mut completed = 0_u64;
            let mut sequence = 0_u64;
            start.wait();
            while !stop.load(Ordering::Relaxed) {
                sequence = sequence.wrapping_add(1);
                let key = sequence.wrapping_add((lane as u64).wrapping_mul(31)) % key_space;
                match sequence % 20 {
                    0 | 1 | 2 => {
                        let _ = map.remove(key);
                    }
                    3 | 4 | 5 | 6 | 7 => {
                        let _ = map.put(key, sequence);
                    }
                    8 | 9 | 10 => {
                        let _ = map.successor(key);
                    }
                    11 | 12 | 13 => {
                        let _ = map.range(key, key.saturating_add(32), 8);
                    }
                    _ => {
                        let _ = map.get(key);
                    }
                }
                completed += 1;
            }
            completed
        }));
    }

    start.wait();
    let started = Instant::now();
    thread::sleep(duration);
    stop.store(true, Ordering::Relaxed);
    let mut completed = 0_u64;
    for handle in workers {
        completed += handle
            .join()
            .map_err(|_| "client thread panicked".to_owned())?;
    }
    let elapsed = started.elapsed().as_secs_f64();
    let throughput = completed as f64 / elapsed;
    if !throughput.is_finite() {
        return Err("benchmark produced a non-finite throughput".to_owned());
    }
    println!("total_ops_per_sec={throughput}");
    Ok(())
}

fn parse_usize(value: Option<String>, name: &str) -> Result<usize, String> {
    value
        .ok_or_else(|| format!("missing {name}"))?
        .parse()
        .map_err(|_| format!("invalid {name}"))
}

fn run() -> Result<(), String> {
    let mut arguments = env::args().skip(1);
    let duration_ms = parse_usize(arguments.next(), "duration milliseconds")?;
    let clients = parse_usize(arguments.next(), "client count")?;
    let key_space = parse_usize(arguments.next(), "key space")? as u64;
    if arguments.next().is_some() {
        return Err("unexpected benchmark argument".to_owned());
    }
    benchmark(Duration::from_millis(duration_ms as u64), clients, key_space)
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("FAIL - {error}");
            ExitCode::FAILURE
        }
    }
}
