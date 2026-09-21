use stack_verus_mpmc::MpmcStack;
use std::env;
use std::process::ExitCode;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Barrier};
use std::thread;
use std::time::{Duration, Instant};

fn benchmark(
    duration: Duration,
    capacity: usize,
    producer_count: usize,
    consumer_count: usize,
) -> Result<(), String> {
    if capacity == 0 || producer_count == 0 || consumer_count == 0 {
        return Err("capacity, producers, and consumers must be positive".to_owned());
    }

    let stack = Arc::new(MpmcStack::new(capacity));
    let stop = Arc::new(AtomicBool::new(false));
    let start = Arc::new(Barrier::new(producer_count + consumer_count + 1));
    let mut producers = Vec::with_capacity(producer_count);
    let mut consumers = Vec::with_capacity(consumer_count);

    for producer in 0..producer_count {
        let stack = Arc::clone(&stack);
        let stop = Arc::clone(&stop);
        let start = Arc::clone(&start);
        producers.push(thread::spawn(move || {
            let mut value = producer as u64;
            let mut completed = 0_u64;
            start.wait();
            while !stop.load(Ordering::Relaxed) {
                match stack.push(value) {
                    Ok(()) => {
                        completed += 1;
                        value = value.wrapping_add(producer_count as u64);
                    }
                    Err(returned) => {
                        value = returned;
                        thread::yield_now();
                    }
                }
            }
            completed
        }));
    }
    for _ in 0..consumer_count {
        let stack = Arc::clone(&stack);
        let stop = Arc::clone(&stop);
        let start = Arc::clone(&start);
        consumers.push(thread::spawn(move || {
            let mut completed = 0_u64;
            start.wait();
            while !stop.load(Ordering::Relaxed) {
                if stack.pop().is_some() {
                    completed += 1;
                } else {
                    thread::yield_now();
                }
            }
            completed
        }));
    }

    start.wait();
    let started = Instant::now();
    thread::sleep(duration);
    stop.store(true, Ordering::Relaxed);
    let mut completed = 0_u64;
    for handle in producers {
        completed += handle
            .join()
            .map_err(|_| "producer thread panicked".to_owned())?;
    }
    for handle in consumers {
        completed += handle
            .join()
            .map_err(|_| "consumer thread panicked".to_owned())?;
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
    let capacity = parse_usize(arguments.next(), "capacity")?;
    let producers = parse_usize(arguments.next(), "producer count")?;
    let consumers = parse_usize(arguments.next(), "consumer count")?;
    if arguments.next().is_some() {
        return Err("unexpected benchmark argument".to_owned());
    }
    benchmark(
        Duration::from_millis(duration_ms as u64),
        capacity,
        producers,
        consumers,
    )
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
