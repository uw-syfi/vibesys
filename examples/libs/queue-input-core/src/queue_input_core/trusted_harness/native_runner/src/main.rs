mod abi;
mod benchmark;
mod protocol;
mod value;

use abi::Api;
use benchmark::BenchmarkConfig;
use protocol::WorkerConfig;
use std::collections::HashMap;
use std::env;
use std::path::{Path, PathBuf};
use std::process;
use std::time::Duration;
use value::MIN_VALUE_SIZE;

fn main() {
    if let Err(error) = run() {
        eprintln!("FAIL - {error}");
        process::exit(1);
    }
}

fn run() -> Result<(), String> {
    let mut args = env::args().skip(1);
    let command = args
        .next()
        .ok_or_else(|| "expected worker or benchmark command".to_string())?;
    let values = parse_flags(args.collect())?;
    match command.as_str() {
        "worker" => {
            validate_flags(
                &values,
                &[
                    "reference",
                    "library",
                    "fd-base",
                    "lanes",
                    "producers",
                    "consumers",
                    "mixed-lane",
                    "capacity",
                    "value-size",
                ],
            )?;
            protocol::run_worker(
                load_api(&values)?,
                WorkerConfig {
                    fd_base: parse(&values, "fd-base")?,
                    lane_count: parse(&values, "lanes")?,
                    producer_count: parse(&values, "producers")?,
                    consumer_count: parse(&values, "consumers")?,
                    mixed_lane: parse::<u32>(&values, "mixed-lane")? != 0,
                    capacity: parse(&values, "capacity")?,
                    value_size: value_size(&values)?,
                },
            )
        }
        "benchmark" => {
            validate_flags(
                &values,
                &[
                    "reference",
                    "library",
                    "scenario",
                    "capacity",
                    "value-size",
                    "producers",
                    "consumers",
                    "warmup-ns",
                    "duration-ns",
                    "output",
                ],
            )?;
            benchmark::run_benchmark(
                load_api(&values)?,
                BenchmarkConfig {
                    scenario: required(&values, "scenario")?.to_string(),
                    capacity: parse(&values, "capacity")?,
                    value_size: value_size(&values)?,
                    producer_count: parse(&values, "producers")?,
                    consumer_count: parse(&values, "consumers")?,
                    warmup: Duration::from_nanos(parse(&values, "warmup-ns")?),
                    duration: Duration::from_nanos(parse(&values, "duration-ns")?),
                },
                Path::new(required(&values, "output")?),
            )
        }
        _ => Err(format!("unknown command {command:?}")),
    }
}

fn validate_flags(values: &HashMap<String, String>, allowed: &[&str]) -> Result<(), String> {
    for name in values.keys() {
        if !allowed.contains(&name.as_str()) {
            return Err(format!("unknown flag --{name}"));
        }
    }
    Ok(())
}

fn parse_flags(args: Vec<String>) -> Result<HashMap<String, String>, String> {
    if has_odd_length(&args) {
        return Err("runner flags must be --name value pairs".to_string());
    }
    let mut values = HashMap::new();
    for pair in args.chunks_exact(2) {
        let name = pair[0]
            .strip_prefix("--")
            .ok_or_else(|| format!("expected flag, got {:?}", pair[0]))?;
        if values.insert(name.to_string(), pair[1].clone()).is_some() {
            return Err(format!("flag --{name} was supplied more than once"));
        }
    }
    Ok(values)
}

#[allow(clippy::manual_is_multiple_of)]
fn has_odd_length<T>(values: &[T]) -> bool {
    values.len() % 2 != 0
}

fn load_api(values: &HashMap<String, String>) -> Result<Api, String> {
    match (values.get("reference"), values.get("library")) {
        (Some(value), None) if value == "1" => Ok(Api::reference()),
        (None, Some(path)) => Api::load(&PathBuf::from(path)),
        _ => Err("expected exactly one of --reference 1 or --library PATH".to_string()),
    }
}

fn required<'a>(values: &'a HashMap<String, String>, name: &str) -> Result<&'a str, String> {
    values
        .get(name)
        .map(String::as_str)
        .ok_or_else(|| format!("--{name} is required"))
}

fn parse<T>(values: &HashMap<String, String>, name: &str) -> Result<T, String>
where
    T: std::str::FromStr,
    T::Err: std::fmt::Display,
{
    required(values, name)?
        .parse()
        .map_err(|error| format!("invalid --{name}: {error}"))
}

fn value_size(values: &HashMap<String, String>) -> Result<usize, String> {
    let size: usize = parse(values, "value-size")?;
    if size < MIN_VALUE_SIZE {
        return Err(format!("value size must be at least {MIN_VALUE_SIZE}"));
    }
    Ok(size)
}
