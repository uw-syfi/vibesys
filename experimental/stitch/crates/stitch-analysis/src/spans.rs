//! The span view of a graph and the measurement windows that select spans.

use crate::AnalysisError;
use stitch_core::{ActivityKind, Graph, SpanKind, SpanLink, TimeWindow};

/// One span, borrowed from the graph, with the fields trace analysis uses.
pub(crate) struct SpanRef<'g> {
    pub trace_id: &'g str,
    pub span_id: &'g str,
    /// Empty for a root span, as in OTLP.
    pub parent_id: &'g str,
    pub service: &'g str,
    pub operation: &'g str,
    pub kind: SpanKind,
    pub start: u64,
    pub end: u64,
    pub failed: bool,
    pub db_system: Option<&'g str>,
    pub db_operation: Option<&'g str>,
    pub links: &'g [SpanLink],
}

pub(crate) fn spans(graph: &Graph) -> Vec<SpanRef<'_>> {
    graph
        .activities
        .iter()
        .filter(|a| a.kind == ActivityKind::Span)
        .filter_map(|a| {
            let detail = a.span.as_deref();
            Some(SpanRef {
                trace_id: a.trace_id.as_deref()?,
                span_id: a.span_id.as_deref()?,
                parent_id: a.parent_span_id.as_deref().unwrap_or(""),
                service: a.service.as_deref().unwrap_or("unknown"),
                operation: detail.map_or(a.name.as_str(), |d| d.operation.as_str()),
                kind: detail.map_or(SpanKind::Internal, |d| d.kind),
                start: a.interval.start_ns,
                end: a.interval.end_ns,
                failed: detail.is_some_and(|d| d.failed),
                db_system: detail.and_then(|d| d.db_system.as_deref()),
                db_operation: detail.and_then(|d| d.db_operation.as_deref()),
                links: detail.map_or(&[][..], |d| d.links.as_slice()),
            })
        })
        .collect()
}

/// Which of the graph's windows an analysis covers.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub enum WindowSelection {
    /// Every window; a graph without windows is analyzed in full.
    #[default]
    All,
    /// One window, by label, or `window-N` (1-based) for an unlabeled one.
    Label(String),
}

/// A window with the name it is reported under.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct NamedWindow {
    pub label: String,
    pub start_unix_nanos: u64,
    pub end_unix_nanos: u64,
}

impl NamedWindow {
    pub(crate) fn contains(&self, start: u64, end: u64) -> bool {
        start >= self.start_unix_nanos && end <= self.end_unix_nanos
    }

    pub(crate) fn overlaps(&self, start: u64, end: u64) -> bool {
        end > self.start_unix_nanos && start < self.end_unix_nanos
    }
}

/// The selected windows; an unwindowed graph yields one unbounded window.
pub(crate) fn select_windows(
    windows: &[TimeWindow],
    selection: &WindowSelection,
) -> Result<(Vec<NamedWindow>, bool), AnalysisError> {
    let named: Vec<NamedWindow> = windows
        .iter()
        .enumerate()
        .map(|(index, window)| NamedWindow {
            label: window
                .label
                .clone()
                .unwrap_or_else(|| format!("window-{}", index + 1)),
            start_unix_nanos: window.start_unix_nanos,
            end_unix_nanos: window.end_unix_nanos,
        })
        .collect();
    match selection {
        WindowSelection::All if named.is_empty() => Ok((
            vec![NamedWindow {
                label: "all".into(),
                start_unix_nanos: 0,
                end_unix_nanos: u64::MAX,
            }],
            false,
        )),
        WindowSelection::All => Ok((named, true)),
        WindowSelection::Label(label) => {
            let available: Vec<&str> = named.iter().map(|w| w.label.as_str()).collect();
            let chosen = named
                .iter()
                .find(|w| &w.label == label)
                .cloned()
                .ok_or_else(|| {
                    AnalysisError::UnknownWindow(
                        label.clone(),
                        if available.is_empty() {
                            "the graph has no windows".into()
                        } else {
                            format!("available: {}", available.join(", "))
                        },
                    )
                })?;
            Ok((vec![chosen], true))
        }
    }
}
