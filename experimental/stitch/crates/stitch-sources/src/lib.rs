//! Source-specific translation into Stitch's shared evidence contract.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use stitch_core::{Activity, ActivityKind, CpuSample, Interval, Observation, Origin};
use thiserror::Error;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InputSpec {
    pub format: String,
    pub path: PathBuf,
    pub clock: String,
}

#[derive(Debug, Error)]
pub enum ImportError {
    #[error("cannot read {path}: {source}")]
    Io {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("invalid {format} artifact {path}: {detail}")]
    Invalid {
        format: &'static str,
        path: PathBuf,
        detail: String,
    },
    #[error("unknown importer {0}")]
    UnknownFormat(String),
    #[error("duplicate importer registration {0}")]
    DuplicateFormat(String),
}

/// Narrow role interface: an importer translates one artifact format and does
/// not control the capture lifecycle.
pub trait Importer {
    fn format(&self) -> &'static str;
    fn import(&self, input: &InputSpec) -> Result<Vec<Observation>, ImportError>;
}

pub struct OtlpJsonImporter;
pub struct PerfScriptImporter;
pub struct Dial9BridgeImporter;
pub struct GpuBridgeImporter;

#[derive(Default)]
pub struct ImporterRegistry {
    importers: BTreeMap<&'static str, Box<dyn Importer>>,
}

impl ImporterRegistry {
    pub fn register(&mut self, importer: impl Importer + 'static) -> Result<(), ImportError> {
        let format = importer.format();
        if self.importers.contains_key(format) {
            return Err(ImportError::DuplicateFormat(format.into()));
        }
        self.importers.insert(format, Box::new(importer));
        Ok(())
    }

    pub fn import(&self, input: &InputSpec) -> Result<Vec<Observation>, ImportError> {
        let importer = self
            .importers
            .get(input.format.as_str())
            .ok_or_else(|| ImportError::UnknownFormat(input.format.clone()))?;
        importer.import(input)
    }
}

pub fn builtin_importers() -> ImporterRegistry {
    let mut registry = ImporterRegistry::default();
    registry
        .register(OtlpJsonImporter)
        .expect("unique built-in importer");
    registry
        .register(PerfScriptImporter)
        .expect("unique built-in importer");
    registry
        .register(Dial9BridgeImporter)
        .expect("unique built-in importer");
    registry
        .register(GpuBridgeImporter)
        .expect("unique built-in importer");
    registry
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

fn required_str<'a>(
    value: &'a serde_json::Value,
    key: &str,
    format: &'static str,
    path: &Path,
) -> Result<&'a str, ImportError> {
    value[key]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| invalid(format, path, format!("missing or empty {key}")))
}

fn timestamp(value: &serde_json::Value, key: &str, path: &Path) -> Result<u64, ImportError> {
    value[key]
        .as_str()
        .and_then(|s| s.parse().ok())
        .or_else(|| value[key].as_u64())
        .ok_or_else(|| invalid("otlp_json", path, format!("invalid {key}")))
}

fn perf_time_ns(raw: &str) -> Option<u64> {
    let raw = raw.trim_end_matches(':');
    let (seconds, fraction) = raw.split_once('.')?;
    if fraction.is_empty() || fraction.len() > 9 || !fraction.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let seconds: u64 = seconds.parse().ok()?;
    let nanos: u64 = fraction.parse().ok()?;
    seconds
        .checked_mul(1_000_000_000)?
        .checked_add(nanos * 10_u64.pow((9 - fraction.len()) as u32))
}

fn finish_perf_sample(
    out: &mut Vec<Observation>,
    current: &mut Option<CpuSample>,
    path: &Path,
) -> Result<(), ImportError> {
    if let Some(sample) = current.take() {
        if sample.frames.is_empty() {
            return Err(invalid(
                "perf_script",
                path,
                format!(
                    "sample at line {} has no frames; export with the ip field and call graphs",
                    sample.origin.record
                ),
            ));
        }
        out.push(Observation::CpuSample(sample));
    }
    Ok(())
}

impl Importer for OtlpJsonImporter {
    fn format(&self) -> &'static str {
        "otlp_json"
    }

    fn import(&self, input: &InputSpec) -> Result<Vec<Observation>, ImportError> {
        let value: serde_json::Value = serde_json::from_str(&read_text(&input.path)?)
            .map_err(|e| invalid("otlp_json", &input.path, e.to_string()))?;
        let resources = value["resourceSpans"]
            .as_array()
            .ok_or_else(|| invalid("otlp_json", &input.path, "missing resourceSpans array"))?;
        let mut out = Vec::new();
        for (resource_index, resource) in resources.iter().enumerate() {
            let service = resource["resource"]["attributes"]
                .as_array()
                .and_then(|attributes| {
                    attributes.iter().find_map(|attribute| {
                        (attribute["key"] == "service.name")
                            .then(|| attribute["value"]["stringValue"].as_str())
                            .flatten()
                    })
                })
                .map(str::to_owned);
            let scopes = resource["scopeSpans"]
                .as_array()
                .ok_or_else(|| invalid("otlp_json", &input.path, "missing scopeSpans array"))?;
            for (scope_index, scope) in scopes.iter().enumerate() {
                let spans = scope["spans"]
                    .as_array()
                    .ok_or_else(|| invalid("otlp_json", &input.path, "missing spans array"))?;
                for (span_index, span) in spans.iter().enumerate() {
                    let trace_id = required_str(span, "traceId", "otlp_json", &input.path)?;
                    let span_id = required_str(span, "spanId", "otlp_json", &input.path)?;
                    let start_ns = timestamp(span, "startTimeUnixNano", &input.path)?;
                    let end_ns = timestamp(span, "endTimeUnixNano", &input.path)?;
                    out.push(Observation::Activity(Activity {
                        id: format!("otel:{trace_id}:{span_id}"),
                        kind: ActivityKind::Span,
                        name: required_str(span, "name", "otlp_json", &input.path)?.into(),
                        interval: Interval {
                            start_ns,
                            end_ns,
                            clock: input.clock.clone(),
                        },
                        service: service.clone(),
                        trace_id: Some(trace_id.into()),
                        span_id: Some(span_id.into()),
                        parent_span_id: span["parentSpanId"]
                            .as_str()
                            .filter(|s| !s.is_empty())
                            .map(str::to_owned),
                        pid: None,
                        tid: None,
                        source_file: None,
                        source_line: None,
                        origin: Origin {
                            source: "otel".into(),
                            artifact: input.path.display().to_string(),
                            record: format!("{resource_index}/{scope_index}/{span_index}"),
                        },
                    }));
                }
            }
        }
        Ok(out)
    }
}

impl Importer for PerfScriptImporter {
    fn format(&self) -> &'static str {
        "perf_script"
    }

    fn import(&self, input: &InputSpec) -> Result<Vec<Observation>, ImportError> {
        let file = File::open(&input.path).map_err(|source| ImportError::Io {
            path: input.path.clone(),
            source,
        })?;
        let mut out = Vec::new();
        let mut current: Option<CpuSample> = None;
        for (index, line) in BufReader::new(file).lines().enumerate() {
            let line = line.map_err(|source| ImportError::Io {
                path: input.path.clone(),
                source,
            })?;
            if line.starts_with('#') {
                continue;
            }
            if line.trim().is_empty() {
                finish_perf_sample(&mut out, &mut current, &input.path)?;
                continue;
            }
            if line.starts_with(char::is_whitespace) {
                let sample = current.as_mut().ok_or_else(|| {
                    invalid(
                        "perf_script",
                        &input.path,
                        format!("line {} has a frame without a sample", index + 1),
                    )
                })?;
                let frame = line.trim();
                let frame = frame
                    .split_once(char::is_whitespace)
                    .map(|(_, symbol)| symbol.trim())
                    .filter(|symbol| !symbol.is_empty())
                    .unwrap_or(frame);
                sample.frames.push(frame.to_owned());
                continue;
            }
            finish_perf_sample(&mut out, &mut current, &input.path)?;
            // perf script prints pid/tid as one field, followed by a header
            // and indented call-chain frames. Older one-line exports are also
            // accepted so existing manifests remain readable.
            let mut fields = line.split_whitespace();
            let first = fields.next().unwrap_or_default();
            let (pid, tid, time_token) = if let Some((pid, tid)) = first.split_once('/') {
                (pid.parse().ok(), tid.parse().ok(), fields.next())
            } else {
                (
                    first.parse().ok(),
                    fields.next().and_then(|s| s.parse().ok()),
                    fields.next(),
                )
            };
            let time = time_token.and_then(perf_time_ns);
            let (Some(pid), Some(tid), Some(time)) = (pid, tid, time) else {
                return Err(invalid(
                    "perf_script",
                    &input.path,
                    format!("line {} has unexpected perf script fields", index + 1),
                ));
            };
            let symbol = fields.collect::<Vec<_>>().join(" ");
            current = Some(CpuSample {
                id: format!("perf:{}:{}", input.path.display(), index + 1),
                pid,
                tid,
                at_ns: time,
                clock: input.clock.clone(),
                frames: if symbol.is_empty() {
                    Vec::new()
                } else {
                    vec![symbol]
                },
                origin: Origin {
                    source: "perf".into(),
                    artifact: input.path.display().to_string(),
                    record: (index + 1).to_string(),
                },
            });
        }
        finish_perf_sample(&mut out, &mut current, &input.path)?;
        Ok(out)
    }
}

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
#[serde(deny_unknown_fields)]
struct GpuRecord {
    id: String,
    name: String,
    service: Option<String>,
    trace_id: Option<String>,
    parent_span_id: Option<String>,
    start_ns: u64,
    end_ns: u64,
    device: String,
}

impl Importer for GpuBridgeImporter {
    fn format(&self) -> &'static str {
        "gpu_bridge_jsonl"
    }

    fn import(&self, input: &InputSpec) -> Result<Vec<Observation>, ImportError> {
        let mut out = Vec::new();
        for (index, line) in read_text(&input.path)?.lines().enumerate() {
            if line.trim().is_empty() {
                continue;
            }
            let event: GpuRecord = serde_json::from_str(line).map_err(|e| {
                invalid(
                    "gpu_bridge_jsonl",
                    &input.path,
                    format!("line {}: {e}", index + 1),
                )
            })?;
            out.push(Observation::Activity(Activity {
                id: format!("gpu:{}:{}", input.path.display(), event.id),
                kind: ActivityKind::GpuKernel,
                name: format!("{} [{}]", event.name, event.device),
                interval: Interval {
                    start_ns: event.start_ns,
                    end_ns: event.end_ns,
                    clock: input.clock.clone(),
                },
                service: event.service,
                trace_id: event.trace_id,
                span_id: None,
                parent_span_id: event.parent_span_id,
                pid: None,
                tid: None,
                source_file: None,
                source_line: None,
                origin: Origin {
                    source: "gpu_bridge".into(),
                    artifact: input.path.display().to_string(),
                    record: (index + 1).to_string(),
                },
            }));
        }
        Ok(out)
    }
}
