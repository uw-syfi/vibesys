//! Distributed-trace analysis of a Stitch graph: latency tables, the
//! aggregated call graph per root endpoint, the wall-clock critical path, and
//! representative traces, plus their bounded text rendering.
#![forbid(unsafe_code)]

mod render;
mod spans;
mod stats;
mod traces;

pub use render::{render, RenderOptions, RenderedFile};
pub use spans::{NamedWindow, WindowSelection};
pub use stats::Distribution;
pub use traces::CRITICAL_PATH_ALGORITHM;

use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use stitch_core::{Graph, SpanKind};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum AnalysisError {
    #[error("unknown window {0:?}; {1}")]
    UnknownWindow(String, String),
    #[error("the graph has no spans inside the selected windows")]
    NoSpans,
    #[error("invalid render options: {0}")]
    Options(String),
    #[error("internal analysis error: {0}")]
    Internal(String),
}

/// One row of a latency table.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct LatencyRow {
    pub name: String,
    #[serde(flatten)]
    pub latency: Distribution,
}

/// Span latency by service, by operation and by datastore call, over every
/// span inside the selected windows.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct LatencyReport {
    pub span_count: usize,
    pub trace_count: usize,
    pub error_count: usize,
    pub services: Vec<LatencyRow>,
    pub spans: Vec<LatencyRow>,
    pub datastores: Vec<LatencyRow>,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct WindowQuality {
    pub label: String,
    pub captured_traces: usize,
    pub eligible_traces: usize,
    pub excluded_traces: usize,
}

/// How many traces were complete enough to analyze, and why others were not.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct TraceQuality {
    pub captured_traces: usize,
    pub eligible_traces: usize,
    pub excluded_traces: usize,
    pub exclusion_reasons: BTreeMap<String, usize>,
    pub matched_client_server_pairs: usize,
    pub unmatched_client_spans: usize,
    pub unmatched_server_spans: usize,
    pub async_relationships: usize,
    pub windows: Vec<WindowQuality>,
}

/// The client side of a call collapsed into its remote server node.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Hop {
    pub client_service: String,
    pub client_operation: String,
    /// Client span duration: the caller's view of the call.
    pub client: Distribution,
    /// Client minus server duration: network, queueing and serialization.
    pub network: Distribution,
}

/// One node of a root endpoint's aggregated call graph, keyed by its path.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct GraphNode {
    pub id: String,
    pub parent_id: Option<String>,
    pub path: String,
    pub service: String,
    pub operation: String,
    pub kind: SpanKind,
    pub calls_per_request: f64,
    pub median_start_offset_ms: f64,
    pub inclusive: Distribution,
    /// Inclusive time minus the union of direct children's intervals.
    pub exclusive: Distribution,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hop: Option<Hop>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct GraphEdge {
    pub from: String,
    pub to: String,
    /// `sync`, `async` (producer/consumer) or `link`.
    pub relationship: String,
    pub count: usize,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct CriticalContribution {
    pub node_id: String,
    pub path: String,
    pub service: String,
    pub operation: String,
    /// Critical time per trace in which this node was on the path.
    pub contribution: Distribution,
    /// Critical time summed over traces, divided by all root requests.
    pub per_request_ms: f64,
    /// Fraction of all root wall time attributed to this node.
    pub share: f64,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct CriticalSegment {
    pub node_id: String,
    pub service: String,
    pub operation: String,
    pub offset_ms: f64,
    pub duration_ms: f64,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct CriticalPathSummary {
    pub algorithm: String,
    pub scope: String,
    pub trace_count: usize,
    pub async_relationships_excluded: usize,
    pub duration: Distribution,
    pub nodes: Vec<CriticalContribution>,
    pub representative_trace_id: String,
    pub representative_duration_ms: f64,
    pub representative_segments: Vec<CriticalSegment>,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct WaterfallSpan {
    pub depth: usize,
    /// The call-graph node, absent for a client span collapsed into its server.
    pub node_id: Option<String>,
    pub service: String,
    pub operation: String,
    pub kind: SpanKind,
    pub failed: bool,
    pub offset_ms: f64,
    pub duration_ms: f64,
}

/// One whole trace, chosen as closest to a latency percentile.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct RepresentativeTrace {
    pub label: String,
    pub trace_id: String,
    pub duration_ms: f64,
    pub spans: Vec<WaterfallSpan>,
}

/// The aggregated graph of all eligible traces that share a root endpoint.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct RootGraph {
    pub service: String,
    pub operation: String,
    pub trace_count: usize,
    pub error_count: usize,
    pub latency: Distribution,
    pub nodes: Vec<GraphNode>,
    pub edges: Vec<GraphEdge>,
    pub critical_path: CriticalPathSummary,
    pub representatives: Vec<RepresentativeTrace>,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Analysis {
    /// The selected windows; empty when the graph has none.
    pub windows: Vec<NamedWindow>,
    pub report: LatencyReport,
    pub quality: TraceQuality,
    /// Root endpoints, most requested first.
    pub roots: Vec<RootGraph>,
}

fn rows(groups: HashMap<String, Vec<stats::Sample>>) -> Vec<LatencyRow> {
    let mut rows: Vec<LatencyRow> = groups
        .into_iter()
        .map(|(name, samples)| LatencyRow {
            name,
            latency: Distribution::of(&samples),
        })
        .collect();
    rows.sort_by(|a, b| {
        b.latency
            .p95_ms
            .total_cmp(&a.latency.p95_ms)
            .then_with(|| a.name.cmp(&b.name))
    });
    rows
}

fn latency_report(spans: &[&spans::SpanRef], windows: &[NamedWindow]) -> LatencyReport {
    let mut services: HashMap<String, Vec<stats::Sample>> = HashMap::new();
    let mut operations: HashMap<String, Vec<stats::Sample>> = HashMap::new();
    let mut datastores: HashMap<String, Vec<stats::Sample>> = HashMap::new();
    let mut traces = BTreeSet::new();
    let mut errors = 0;
    let mut count = 0;
    for span in spans {
        if !windows.iter().any(|w| w.contains(span.start, span.end)) {
            continue;
        }
        count += 1;
        traces.insert(span.trace_id);
        errors += usize::from(span.failed);
        let sample = stats::Sample {
            ms: stats::duration_ms(span.start, span.end),
            failed: span.failed,
        };
        services
            .entry(span.service.to_owned())
            .or_default()
            .push(sample);
        operations
            .entry(format!("{}: {}", span.service, span.operation))
            .or_default()
            .push(sample);
        if let (SpanKind::Client, Some(system)) = (span.kind, span.db_system) {
            let operation = span.db_operation.unwrap_or(span.operation);
            datastores
                .entry(format!("{} → {system}: {operation}", span.service))
                .or_default()
                .push(sample);
        }
    }
    LatencyReport {
        span_count: count,
        trace_count: traces.len(),
        error_count: errors,
        services: rows(services),
        spans: rows(operations),
        datastores: rows(datastores),
    }
}

/// Analyze the spans of `graph` that fall inside the selected windows.
///
/// A trace joins the call graph only if it is complete: one root span inside
/// a window, every span inside that same window and nested in its parent, and
/// no repeated span IDs or cycles. Excluded traces are counted by reason.
///
/// # Errors
///
/// Returns an error for an unknown window label, when no span lies inside the
/// selected windows, or on an internal invariant violation.
pub fn analyze(graph: &Graph, selection: &WindowSelection) -> Result<Analysis, AnalysisError> {
    let (windows, bounded) = spans::select_windows(&graph.windows, selection)?;
    let all = spans::spans(graph);
    let selected: Vec<&spans::SpanRef> = all
        .iter()
        .filter(|s| windows.iter().any(|w| w.overlaps(s.start, s.end)))
        .collect();
    let report = latency_report(&selected, &windows);
    if report.span_count == 0 {
        return Err(AnalysisError::NoSpans);
    }
    let mut by_trace: BTreeMap<&str, Vec<&spans::SpanRef>> = BTreeMap::new();
    for span in selected {
        by_trace.entry(span.trace_id).or_default().push(span);
    }
    let mut builder = traces::TraceGraphBuilder::new(&windows);
    for (id, spans) in by_trace {
        builder
            .add(id, spans, &windows)
            .map_err(AnalysisError::Internal)?;
    }
    let (quality, mut roots) = builder.finish();
    roots.sort_by(|a, b| {
        b.trace_count
            .cmp(&a.trace_count)
            .then_with(|| b.latency.p95_ms.total_cmp(&a.latency.p95_ms))
            .then_with(|| (&a.service, &a.operation).cmp(&(&b.service, &b.operation)))
    });
    Ok(Analysis {
        windows: if bounded { windows } else { Vec::new() },
        report,
        quality,
        roots,
    })
}
