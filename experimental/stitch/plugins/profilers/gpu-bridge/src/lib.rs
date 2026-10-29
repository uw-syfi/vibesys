//! Provisional GPU bridge importer.
#![forbid(unsafe_code)]
use serde::Deserialize;
use std::path::Path;
use stitch_core::{Activity, ActivityKind, Interval, Observation, Origin};
use stitch_importer_api::{ImportError, Importer, InputSpec};

pub struct GpuBridgeImporter;

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
