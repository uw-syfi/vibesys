mod abi;
mod benchmark;
mod probe;
mod protocol;
mod value;

use abi::Api;
use argparse::{ArgumentParser, Store, StoreOption, StoreTrue};
use benchmark::BenchmarkConfig;
use protocol::WorkerConfig;
use std::path::PathBuf;
use std::process;
use std::str::FromStr;
use std::time::Duration;
use value::MIN_VALUE_SIZE;

#[derive(Clone, Copy)]
enum Command {
    Worker,
    Probe,
    Benchmark,
}

impl FromStr for Command {
    type Err = String;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "worker" => Ok(Self::Worker),
            "probe" => Ok(Self::Probe),
            "benchmark" => Ok(Self::Benchmark),
            _ => Err(format!("unknown command {value:?}")),
        }
    }
}

#[derive(Default)]
struct Args {
    command: Option<Command>,
    use_reference: bool,
    library: Option<PathBuf>,
    capacity: u64,
    value_size: usize,
    producer_count: u32,
    consumer_count: u32,
    fd_base: Option<i32>,
    lane_count: Option<usize>,
    mixed_lane: bool,
    scenario: Option<String>,
    warmup_ns: Option<u64>,
    duration_ns: Option<u64>,
    output: Option<PathBuf>,
}

impl Args {
    fn load_api(&self) -> Result<Api, String> {
        match (self.use_reference, self.library.as_ref()) {
            (true, None) => Ok(Api::reference()),
            (false, Some(path)) => Api::load(path),
            _ => Err("expected exactly one of --reference or --library PATH".to_string()),
        }
    }

    fn checked_value_size(&self) -> Result<usize, String> {
        if self.value_size < MIN_VALUE_SIZE {
            return Err(format!("value size must be at least {MIN_VALUE_SIZE}"));
        }
        Ok(self.value_size)
    }
}

fn main() {
    if let Err(error) = run() {
        eprintln!("FAIL - {error}");
        process::exit(1);
    }
}

fn run() -> Result<(), String> {
    let args = parse_args();
    let api = args.load_api()?;
    let value_size = args.checked_value_size()?;

    match args
        .command
        .expect("argparse enforces the required command")
    {
        Command::Worker => protocol::run_worker(
            api,
            WorkerConfig {
                fd_base: required(args.fd_base, "--fd-base")?,
                lane_count: required(args.lane_count, "--lanes")?,
                producer_count: args.producer_count,
                consumer_count: args.consumer_count,
                mixed_lane: args.mixed_lane,
                capacity: args.capacity,
                value_size,
            },
        ),
        Command::Probe => probe::run_probe(
            api,
            args.capacity,
            value_size,
            args.producer_count,
            args.consumer_count,
        ),
        Command::Benchmark => benchmark::run_benchmark(
            api,
            BenchmarkConfig {
                scenario: required(args.scenario, "--scenario")?,
                capacity: args.capacity,
                value_size,
                producer_count: args.producer_count,
                consumer_count: args.consumer_count,
                warmup: Duration::from_nanos(required(args.warmup_ns, "--warmup-ns")?),
                duration: Duration::from_nanos(required(args.duration_ns, "--duration-ns")?),
            },
            &required(args.output, "--output")?,
        ),
    }
}

fn parse_args() -> Args {
    let mut args = Args::default();
    {
        let mut parser = ArgumentParser::new();
        parser.set_description("Runs queue candidate ABI workers and benchmarks");
        parser.refer(&mut args.command).required().add_argument(
            "command",
            StoreOption,
            "worker, probe, or benchmark",
        );
        parser.refer(&mut args.use_reference).add_option(
            &["--reference"],
            StoreTrue,
            "use the built-in reference candidate",
        );
        parser.refer(&mut args.library).add_option(
            &["--library"],
            StoreOption,
            "candidate shared library path",
        );
        parser.refer(&mut args.capacity).required().add_option(
            &["--capacity"],
            Store,
            "queue capacity in items",
        );
        parser.refer(&mut args.value_size).required().add_option(
            &["--value-size"],
            Store,
            "maximum copied value size",
        );
        parser
            .refer(&mut args.producer_count)
            .required()
            .add_option(&["--producers"], Store, "producer count");
        parser
            .refer(&mut args.consumer_count)
            .required()
            .add_option(&["--consumers"], Store, "consumer count");
        parser.refer(&mut args.fd_base).add_option(
            &["--fd-base"],
            StoreOption,
            "first inherited lane file descriptor",
        );
        parser.refer(&mut args.lane_count).add_option(
            &["--lanes"],
            StoreOption,
            "number of correctness lanes",
        );
        parser.refer(&mut args.mixed_lane).add_option(
            &["--mixed-lane"],
            StoreTrue,
            "allow enqueue and dequeue on the single lane",
        );
        parser.refer(&mut args.scenario).add_option(
            &["--scenario"],
            StoreOption,
            "queue concurrency scenario",
        );
        parser.refer(&mut args.warmup_ns).add_option(
            &["--warmup-ns"],
            StoreOption,
            "warmup duration in nanoseconds",
        );
        parser.refer(&mut args.duration_ns).add_option(
            &["--duration-ns"],
            StoreOption,
            "measured duration in nanoseconds",
        );
        parser.refer(&mut args.output).add_option(
            &["--output"],
            StoreOption,
            "benchmark result path",
        );
        parser.parse_args_or_exit();
    }
    args
}

fn required<T>(value: Option<T>, name: &str) -> Result<T, String> {
    value.ok_or_else(|| format!("{name} is required for this command"))
}
