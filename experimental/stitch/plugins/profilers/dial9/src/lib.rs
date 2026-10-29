//! Native dial9 and provisional dial9 bridge importers.
#![forbid(unsafe_code)]
use serde::Deserialize;
use std::path::Path;
use stitch_core::{Activity, ActivityKind, Interval, Observation, Origin};
use stitch_importer_api::{ImportError, Importer, InputSpec};

pub struct Dial9BridgeImporter;
pub struct Dial9NativeImporter;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PollRecord {
    task_id: String,
    poll_id: String,
    service: Option<String>,
    trace_id: Option<String>,
    parent_span_id: Option<String>,
    pid: u32,
    tid: u32,
    start_ns: u64,
    end_ns: u64,
    name: String,
    source_file: Option<String>,
    source_line: Option<u32>,
}

impl Importer for Dial9BridgeImporter {
    fn format(&self) -> &'static str {
        "dial9_bridge_jsonl"
    }

    fn import(&self, input: &InputSpec) -> Result<Vec<Observation>, ImportError> {
        let mut out = Vec::new();
        for (index, line) in read_text(&input.path)?.lines().enumerate() {
            if line.trim().is_empty() {
                continue;
            }
            let poll: PollRecord = serde_json::from_str(line).map_err(|e| {
                invalid(
                    "dial9_bridge_jsonl",
                    &input.path,
                    format!("line {}: {e}", index + 1),
                )
            })?;
            out.push(Observation::Activity(Activity {
                id: format!(
                    "dial9:{}:{}:{}",
                    input.path.display(),
                    poll.task_id,
                    poll.poll_id
                ),
                kind: ActivityKind::TaskPoll,
                name: poll.name,
                interval: Interval {
                    start_ns: poll.start_ns,
                    end_ns: poll.end_ns,
                    clock: input.clock.clone(),
                },
                service: poll.service,
                trace_id: poll.trace_id,
                span_id: None,
                parent_span_id: poll.parent_span_id,
                pid: Some(poll.pid),
                tid: Some(poll.tid),
                source_file: poll.source_file,
                source_line: poll.source_line,
                origin: Origin {
                    source: "dial9_bridge".into(),
                    artifact: input.path.display().to_string(),
                    record: (index + 1).to_string(),
                },
            }));
        }
        Ok(out)
    }
}

#[derive(Deserialize)]
struct NativeRegionRecord {
    timestamp_ns: u64,
    start_ns: u64,
    pid: u32,
    tid: u32,
    region_id: u64,
    trace_id: String,
    parent_span_id: String,
    service: String,
    name: String,
    source_file: String,
    source_line: u32,
}

impl Importer for Dial9NativeImporter {
    fn format(&self) -> &'static str {
        "dial9_stitch_bin"
    }

    fn import(&self, input: &InputSpec) -> Result<Vec<Observation>, ImportError> {
        let data = std::fs::read(&input.path).map_err(|source| ImportError::Io {
            path: input.path.clone(),
            source,
        })?;
        let mut decoder = dial9_trace_format::decoder::Decoder::new(&data)
            .ok_or_else(|| invalid("dial9_stitch_bin", &input.path, "invalid dial9 header"))?;
        let mut out = Vec::new();
        decoder
            .try_for_each_event(|event| -> Result<(), String> {
                if event.name != "StitchCpuRegion" {
                    return Ok(());
                }
                let region: NativeRegionRecord = event.deserialize().map_err(|e| e.to_string())?;
                out.push(Observation::Activity(Activity {
                    id: format!("dial9:{}:{}", input.path.display(), region.region_id),
                    kind: ActivityKind::CpuRegion,
                    name: region.name,
                    interval: Interval {
                        start_ns: region.start_ns,
                        end_ns: region.timestamp_ns,
                        clock: input.clock.clone(),
                    },
                    service: Some(region.service),
                    trace_id: Some(region.trace_id),
                    span_id: None,
                    parent_span_id: Some(region.parent_span_id),
                    pid: Some(region.pid),
                    tid: Some(region.tid),
                    source_file: Some(region.source_file),
                    source_line: Some(region.source_line),
                    origin: Origin {
                        source: "dial9".into(),
                        artifact: input.path.display().to_string(),
                        record: out.len().to_string(),
                    },
                }));
                Ok(())
            })
            .map_err(|e| invalid("dial9_stitch_bin", &input.path, e.to_string()))?;
        Ok(out)
    }
}

fn read_text(path: &Path) -> Result<String, ImportError> {
    std::fs::read_to_string(path).map_err(|source| ImportError::Io {
        path: path.to_owned(),
        source,
    })
}
fn invalid(format: &'static str, path: &Path, detail: impl Into<String>) -> ImportError {
    ImportError::Invalid {
        format,
        path: path.to_owned(),
        detail: detail.into(),
    }
}
