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
use value::MIN_PAYLOAD_SIZE;

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
    key_size: usize,
    value_size: usize,
    client_count: u32,
    fd_base: Option<i32>,
    lane_count: Option<usize>,
    mixed_lane: bool,
    scenario: Option<String>,
    key_space: Option<u64>,
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

    fn checked_key_size(&self) -> Result<usize, String> {
        if self.key_size == 0 {
            return Err("key size must be greater than zero".to_string());
        }
        Ok(self.key_size)
    }

    fn checked_value_size(&self) -> Result<usize, String> {
        if self.value_size == 0 {
            return Err("value size must be greater than zero".to_string());
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
    let key_size = args.checked_key_size()?;
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
                client_count: args.client_count,
                mixed_lane: args.mixed_lane,
                max_key_size: key_size,
                max_value_size: value_size,
            },
        ),
        Command::Probe => probe::run_probe(api, key_size, value_size, args.client_count),
        Command::Benchmark => {
            if key_size < MIN_PAYLOAD_SIZE || value_size < MIN_PAYLOAD_SIZE {
                return Err(format!(
                    "benchmark key and value sizes must be at least {MIN_PAYLOAD_SIZE}"
                ));
            }
            benchmark::run_benchmark(
                api,
                BenchmarkConfig {
                    scenario: required(args.scenario, "--scenario")?,
                    max_key_size: key_size,
                    max_value_size: value_size,
                    client_count: args.client_count,
                    key_space: required(args.key_space, "--key-space")?,
                    warmup: Duration::from_nanos(required(args.warmup_ns, "--warmup-ns")?),
                    duration: Duration::from_nanos(required(args.duration_ns, "--duration-ns")?),
                },
                &required(args.output, "--output")?,
            )
        }
    }
}

fn parse_args() -> Args {
    let mut args = Args::default();
    {
        let mut parser = ArgumentParser::new();
        parser.set_description("Runs unordered map candidate ABI workers and benchmarks");
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
        parser.refer(&mut args.key_size).required().add_option(
            &["--key-size"],
            Store,
            "maximum copied key size",
        );
        parser.refer(&mut args.value_size).required().add_option(
            &["--value-size"],
            Store,
            "maximum copied value size",
        );
        parser.refer(&mut args.client_count).required().add_option(
            &["--clients"],
            Store,
            "client handle count",
        );
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
            "allow put, get, and remove on the single lane",
        );
        parser.refer(&mut args.scenario).add_option(
            &["--scenario"],
            StoreOption,
            "benchmark concurrency scenario",
        );
        parser.refer(&mut args.key_space).add_option(
            &["--key-space"],
            StoreOption,
            "distinct encoded keys in the benchmark",
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
