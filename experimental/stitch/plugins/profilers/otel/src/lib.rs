//! OpenTelemetry OTLP JSON and JSONL importers, and `OTel` capture on Kubernetes.
#![forbid(unsafe_code)]

pub mod k8s;
mod semantic;

use serde::Deserialize;
use std::collections::HashMap;
use std::fs::File;
use std::io::{BufRead, BufReader, Read};
use std::path::Path;
use stitch_core::{Activity, ActivityKind, Interval, Observation, Origin, SpanDetail, SpanLink};
use stitch_importer_api::{ImportError, Importer, InputSpec};

pub struct OtlpJsonImporter;
pub struct OtlpJsonlImporter;

fn invalid(format: &'static str, path: &Path, detail: impl Into<String>) -> ImportError {
    ImportError::Invalid {
        format,
        path: path.to_owned(),
        detail: detail.into(),
    }
}

fn open(path: &Path) -> Result<File, ImportError> {
    File::open(path).map_err(|source| ImportError::Io {
        path: path.to_owned(),
        source,
    })
}

/// One `ExportTraceServiceRequest` in the OTLP JSON encoding. Fields this
/// importer does not use are ignored, as the OTLP JSON mapping requires.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct OtlpDocument {
    resource_spans: Option<Vec<ResourceSpans>>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ResourceSpans {
    #[serde(default)]
    resource: Resource,
    #[serde(default, alias = "instrumentationLibrarySpans")]
    scope_spans: Vec<ScopeSpans>,
}

#[derive(Default, Deserialize)]
struct Resource {
    #[serde(default)]
    attributes: Vec<KeyValue>,
}

#[derive(Deserialize)]
struct ScopeSpans {
    #[serde(default)]
    spans: Vec<RawSpan>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct RawSpan {
    #[serde(default)]
    trace_id: String,
    #[serde(default)]
    span_id: String,
    #[serde(default)]
    parent_span_id: String,
    #[serde(default)]
    name: String,
    #[serde(default)]
    kind: serde_json::Value,
    start_time_unix_nano: Option<serde_json::Value>,
    end_time_unix_nano: Option<serde_json::Value>,
    #[serde(default)]
    attributes: Vec<KeyValue>,
    #[serde(default)]
    status: Option<Status>,
    #[serde(default)]
    links: Vec<RawLink>,
}

#[derive(Deserialize)]
struct Status {
    #[serde(default)]
    code: serde_json::Value,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct RawLink {
    #[serde(default)]
    trace_id: String,
    #[serde(default)]
    span_id: String,
}

#[derive(Deserialize)]
struct KeyValue {
    #[serde(default)]
    key: String,
    #[serde(default)]
    value: serde_json::Value,
}

/// The scalar value of an OTLP `AnyValue`; arrays and maps are not used.
#[derive(Clone, Debug, PartialEq)]
enum AttributeValue {
    Text(String),
    Bool(bool),
}

type Attributes = HashMap<String, AttributeValue>;

fn attributes(items: Vec<KeyValue>) -> Attributes {
    let mut out = HashMap::with_capacity(items.len());
    for item in items {
        for key in ["stringValue", "intValue", "doubleValue", "boolValue"] {
            let value = match &item.value[key] {
                serde_json::Value::String(text) => AttributeValue::Text(text.clone()),
                serde_json::Value::Number(number) => AttributeValue::Text(number.to_string()),
                serde_json::Value::Bool(flag) => AttributeValue::Bool(*flag),
                _ => continue,
            };
            out.insert(item.key, value);
            break;
        }
    }
    out
}

fn timestamp(value: Option<&serde_json::Value>) -> Option<u64> {
    match value? {
        serde_json::Value::String(text) => text.parse().ok(),
        serde_json::Value::Number(number) => number.as_u64(),
        _ => None,
    }
}

/// Translates OTLP documents into span observations. A repeated span identity
/// gets a distinct observation ID so the graph keeps both copies and analyses
/// can exclude the trace as malformed instead of failing the whole import.
struct SpanCollector<'a> {
    format: &'static str,
    input: &'a InputSpec,
    artifact: String,
    seen: HashMap<String, u32>,
    out: Vec<Observation>,
}

impl<'a> SpanCollector<'a> {
    fn new(format: &'static str, input: &'a InputSpec) -> Self {
        Self {
            format,
            input,
            artifact: input.path.display().to_string(),
            seen: HashMap::new(),
            out: Vec::new(),
        }
    }

    fn add_document(
        &mut self,
        document: OtlpDocument,
        line: Option<usize>,
    ) -> Result<(), ImportError> {
        let (location, prefix) = match line {
            Some(line) => (format!("line {line}: "), format!("{line}:")),
            None => (String::new(), String::new()),
        };
        let resources = document.resource_spans.ok_or_else(|| {
            invalid(
                self.format,
                &self.input.path,
                format!("{location}missing resourceSpans"),
            )
        })?;
        for (resource_index, resource) in resources.into_iter().enumerate() {
            let service = match attributes(resource.resource.attributes).remove("service.name") {
                Some(AttributeValue::Text(name)) if !name.is_empty() => Some(name),
                _ => None,
            };
            for (scope_index, scope) in resource.scope_spans.into_iter().enumerate() {
                for (span_index, span) in scope.spans.into_iter().enumerate() {
                    let record = format!("{prefix}{resource_index}/{scope_index}/{span_index}");
                    self.add_span(span, service.as_ref(), record)?;
                }
            }
        }
        Ok(())
    }

    fn add_span(
        &mut self,
        span: RawSpan,
        service: Option<&String>,
        record: String,
    ) -> Result<(), ImportError> {
        let fail = |detail: &str| {
            invalid(
                self.format,
                &self.input.path,
                format!("span {record}: {detail}"),
            )
        };
        if span.trace_id.is_empty() {
            return Err(fail("missing or empty traceId"));
        }
        if span.span_id.is_empty() {
            return Err(fail("missing or empty spanId"));
        }
        let start_ns = timestamp(span.start_time_unix_nano.as_ref())
            .ok_or_else(|| fail("invalid startTimeUnixNano"))?;
        let end_ns = timestamp(span.end_time_unix_nano.as_ref())
            .ok_or_else(|| fail("invalid endTimeUnixNano"))?;
        if end_ns < start_ns {
            return Err(fail("endTimeUnixNano precedes startTimeUnixNano"));
        }
        let mut id = format!("otel:{}:{}", span.trace_id, span.span_id);
        let repeats = self.seen.entry(id.clone()).or_insert(0);
        *repeats += 1;
        if *repeats > 1 {
            id = format!("{id}#{repeats}");
        }
        let attributes = attributes(span.attributes);
        let status_code = match span.status.map(|status| status.code) {
            Some(serde_json::Value::String(code)) => code,
            Some(serde_json::Value::Number(code)) => code.to_string(),
            _ => String::new(),
        };
        let detail = SpanDetail {
            kind: semantic::span_kind(&span.kind),
            operation: semantic::operation(&span.name, &attributes),
            failed: semantic::failed(&status_code, &attributes),
            db_system: semantic::first_text(&attributes, &["db.system.name", "db.system"]),
            db_operation: semantic::first_text(&attributes, &["db.operation.name", "db.operation"]),
            links: span
                .links
                .into_iter()
                .map(|link| SpanLink {
                    trace_id: link.trace_id,
                    span_id: link.span_id,
                })
                .collect(),
        };
        self.out.push(Observation::Activity(Activity {
            id,
            kind: ActivityKind::Span,
            name: if span.name.is_empty() {
                "unknown".into()
            } else {
                span.name
            },
            interval: Interval {
                start_ns,
                end_ns,
                clock: self.input.clock.clone(),
            },
            service: service.cloned(),
            trace_id: Some(span.trace_id),
            span_id: Some(span.span_id),
            parent_span_id: (!span.parent_span_id.is_empty()).then_some(span.parent_span_id),
            pid: None,
            tid: None,
            source_file: None,
            source_line: None,
            origin: Origin {
                source: "otel".into(),
                artifact: self.artifact.clone(),
                record,
            },
            span: Some(Box::new(detail)),
        }));
        Ok(())
    }
}

impl Importer for OtlpJsonImporter {
    fn format(&self) -> &'static str {
        "otlp_json"
    }

    fn import(&self, input: &InputSpec) -> Result<Vec<Observation>, ImportError> {
        let mut text = String::new();
        open(&input.path)?
            .read_to_string(&mut text)
            .map_err(|source| ImportError::Io {
                path: input.path.clone(),
                source,
            })?;
        let document: OtlpDocument = serde_json::from_str(text.trim_start_matches('\u{feff}'))
            .map_err(|e| invalid("otlp_json", &input.path, e.to_string()))?;
        let mut collector = SpanCollector::new("otlp_json", input);
        collector.add_document(document, None)?;
        Ok(collector.out)
    }
}

impl Importer for OtlpJsonlImporter {
    fn format(&self) -> &'static str {
        "otlp_jsonl"
    }

    /// Import one OTLP JSON document per line, as the `OTel` Collector file
    /// exporter writes them. Lines are decoded one at a time, so memory grows
    /// with the number of spans rather than with the size of the file's text.
    fn import(&self, input: &InputSpec) -> Result<Vec<Observation>, ImportError> {
        let mut collector = SpanCollector::new("otlp_jsonl", input);
        for (index, line) in BufReader::new(open(&input.path)?).lines().enumerate() {
            let line = line.map_err(|source| ImportError::Io {
                path: input.path.clone(),
                source,
            })?;
            let line = line.trim_start_matches('\u{feff}');
            if line.trim().is_empty() {
                continue;
            }
            let document: OtlpDocument = serde_json::from_str(line).map_err(|e| {
                invalid(
                    "otlp_jsonl",
                    &input.path,
                    format!("line {}: {e}", index + 1),
                )
            })?;
            collector.add_document(document, Some(index + 1))?;
        }
        Ok(collector.out)
    }
}
