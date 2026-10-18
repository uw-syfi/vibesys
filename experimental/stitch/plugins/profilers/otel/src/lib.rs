//! OpenTelemetry OTLP JSON and JSONL importers.
#![forbid(unsafe_code)]
use std::path::Path;
use stitch_core::{Activity, ActivityKind, Interval, Observation, Origin};
use stitch_importer_api::{ImportError, Importer, InputSpec};

pub struct OtlpJsonImporter;
pub struct OtlpJsonlImporter;

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

impl Importer for OtlpJsonImporter {
    fn format(&self) -> &'static str {
        "otlp_json"
    }

    fn import(&self, input: &InputSpec) -> Result<Vec<Observation>, ImportError> {
        let value: serde_json::Value = serde_json::from_str(&read_text(&input.path)?)
            .map_err(|e| invalid("otlp_json", &input.path, e.to_string()))?;
        import_otlp_value(&value, input)
    }
}

impl Importer for OtlpJsonlImporter {
    fn format(&self) -> &'static str {
        "otlp_jsonl"
    }

    fn import(&self, input: &InputSpec) -> Result<Vec<Observation>, ImportError> {
        let mut resources = Vec::new();
        for (index, line) in read_text(&input.path)?.lines().enumerate() {
            if line.trim().is_empty() {
                continue;
            }
            let mut document: serde_json::Value = serde_json::from_str(line).map_err(|e| {
                invalid(
                    "otlp_jsonl",
                    &input.path,
                    format!("line {}: {e}", index + 1),
                )
            })?;
            let spans = document["resourceSpans"].as_array_mut().ok_or_else(|| {
                invalid(
                    "otlp_jsonl",
                    &input.path,
                    format!("line {}: missing resourceSpans", index + 1),
                )
            })?;
            resources.append(spans);
        }
        import_otlp_value(&serde_json::json!({"resourceSpans": resources}), input)
    }
}

fn import_otlp_value(
    value: &serde_json::Value,
    input: &InputSpec,
) -> Result<Vec<Observation>, ImportError> {
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
