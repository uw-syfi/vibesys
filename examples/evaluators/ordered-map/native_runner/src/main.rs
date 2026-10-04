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
    max_key_size: u64,
    max_value_size: u64,
    client_count: u32,
    fd_base: Option<i32>,
    lane_count: Option<usize>,
    mixed_lane: bool,
    scenario: Option<String>,
    key_space: Option<u64>,
    range_span: Option<u64>,
    read_ratio: Option<u32>,
    write_ratio: Option<u32>,
    delete_ratio: Option<u32>,
    range_ratio: Option<u32>,
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
    if args.max_key_size == 0 || args.max_value_size == 0 {
        return Err("max key size and max value size must be greater than zero".to_string());
    }

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
                max_key_size: args.max_key_size,
                max_value_size: args.max_value_size,
            },
        ),
        Command::Probe => probe::run_probe(
            api,
            args.max_key_size,
            args.max_value_size,
            args.client_count,
        ),
        Command::Benchmark => benchmark::run_benchmark(
            api,
            BenchmarkConfig {
                scenario: required(args.scenario.clone(), "--scenario")?,
                max_key_size: args.max_key_size,
                max_value_size: args.max_value_size,
                client_count: args.client_count,
                key_space: required(args.key_space, "--key-space")?,
                range_span: required(args.range_span, "--range-span")?,
                ratios: ratios_from(&args)?,
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
        parser.set_description("Runs ordered map candidate ABI workers and benchmarks");
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
        parser.refer(&mut args.max_key_size).required().add_option(
            &["--max-key-size"],
            Store,
            "maximum copied key size",
        );
        parser
            .refer(&mut args.max_value_size)
            .required()
            .add_option(&["--max-value-size"], Store, "maximum copied value size");
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
            "allow every ordered-map operation on the single lane",
        );
        parser.refer(&mut args.scenario).add_option(
            &["--scenario"],
            StoreOption,
            "map concurrency scenario",
        );
        parser.refer(&mut args.key_space).add_option(
            &["--key-space"],
            StoreOption,
            "distinct encoded benchmark keys",
        );
        parser.refer(&mut args.range_span).add_option(
            &["--range-span"],
            StoreOption,
            "keys between a range start and its end",
        );
        parser.refer(&mut args.read_ratio).add_option(
            &["--read-ratio"],
            StoreOption,
            "get weight when overriding the scenario mix",
        );
        parser.refer(&mut args.write_ratio).add_option(
            &["--write-ratio"],
            StoreOption,
            "put weight when overriding the scenario mix",
        );
        parser.refer(&mut args.delete_ratio).add_option(
            &["--delete-ratio"],
            StoreOption,
            "remove weight when overriding the scenario mix",
        );
        parser.refer(&mut args.range_ratio).add_option(
            &["--range-ratio"],
            StoreOption,
            "range weight when overriding the scenario mix",
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

fn ratios_from(args: &Args) -> Result<Option<benchmark::Ratios>, String> {
    let values = [
        args.read_ratio,
        args.write_ratio,
        args.delete_ratio,
        args.range_ratio,
    ];
    let set = values.iter().filter(|value| value.is_some()).count();
    if set == 0 {
        return Ok(None);
    }
    if set != 4 {
        return Err(
            "read-ratio, write-ratio, delete-ratio, and range-ratio must be set together"
                .to_string(),
        );
    }
    let read = args.read_ratio.expect("counted");
    let write = args.write_ratio.expect("counted");
    let delete = args.delete_ratio.expect("counted");
    let range = args.range_ratio.expect("counted");
    if read > 100 || write > 100 || delete > 100 || range > 100 {
        return Err("operation ratios must be in [0, 100]".to_string());
    }
    if read + write + delete + range != 100 {
        return Err("operation ratios must sum to 100".to_string());
    }
    Ok(Some(benchmark::Ratios {
        read,
        write,
        delete,
        range,
    }))
}
