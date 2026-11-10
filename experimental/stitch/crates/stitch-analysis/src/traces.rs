//! Trace qualification, client/server pairing, call-graph aggregation and the
//! wall-clock active-leaf critical path.
//!
//! This is a port of `trace_graph.go` and `critical_path.go` in the
//! evaluator's telemetry package (`wall_clock_active_leaf_v1`). Keep the two
//! in step: the parity test in `tests/parity.rs` holds numbers the Go code
//! produced for a shared fixture.

use crate::spans::{NamedWindow, SpanRef};
use crate::stats::{duration_ms, Distribution, Sample};
use crate::{
    CriticalContribution, CriticalPathSummary, CriticalSegment, GraphEdge, GraphNode, Hop,
    RepresentativeTrace, RootGraph, TraceQuality, WaterfallSpan, WindowQuality,
};
use std::collections::{BTreeMap, HashMap};
use stitch_core::SpanKind;

pub const CRITICAL_PATH_ALGORITHM: &str = "wall_clock_active_leaf_v1";
const CRITICAL_PATH_SCOPE: &str = "synchronous_request";

/// One trace that passed qualification. Indices refer to `spans`.
pub(crate) struct EligibleTrace<'a> {
    pub id: &'a str,
    pub spans: Vec<&'a SpanRef<'a>>,
    index: HashMap<&'a str, usize>,
    pub root: usize,
    window: usize,
    pub paths: Vec<String>,
    /// Client spans collapsed into their remote server span.
    paired: Vec<bool>,
    /// For a paired client, its server span.
    pair_of: Vec<Option<usize>>,
    /// Direct children by raw parent span ID, in input order.
    children: HashMap<&'a str, Vec<usize>>,
}

impl EligibleTrace<'_> {
    fn parent(&self, index: usize) -> Option<usize> {
        self.index.get(self.spans[index].parent_id).copied()
    }

    /// The nearest ancestor that is not a collapsed client.
    fn visible_parent(&self, index: usize) -> Option<usize> {
        let mut parent = self.parent(index)?;
        for _ in 0..self.spans.len() {
            if !self.paired[parent] {
                return Some(parent);
            }
            parent = self.parent(parent)?;
        }
        None
    }

    fn duration(&self, index: usize) -> f64 {
        duration_ms(self.spans[index].start, self.spans[index].end)
    }
}

/// Qualify one trace, or name why it is excluded and which window it was in.
pub(crate) fn qualify<'a>(
    id: &'a str,
    spans: Vec<&'a SpanRef<'a>>,
    windows: &[NamedWindow],
) -> Result<EligibleTrace<'a>, (&'static str, Option<usize>)> {
    let first_overlap = || {
        windows
            .iter()
            .position(|w| spans.iter().any(|s| w.overlaps(s.start, s.end)))
    };
    let mut index = HashMap::with_capacity(spans.len());
    let mut root = None;
    for (position, span) in spans.iter().enumerate() {
        if index.insert(span.span_id, position).is_some() {
            return Err(("duplicate_span_id", first_overlap()));
        }
        if span.parent_id.is_empty() {
            if root.is_some() {
                return Err(("multiple_roots", first_overlap()));
            }
            root = Some(position);
        }
    }
    let Some(root) = root else {
        return Err(("missing_parent", first_overlap()));
    };
    let Some(window) = windows
        .iter()
        .position(|w| w.contains(spans[root].start, spans[root].end))
    else {
        return Err(("outside_measurement_window", first_overlap()));
    };
    for span in &spans {
        if !windows[window].contains(span.start, span.end) {
            return Err(("outside_measurement_window", Some(window)));
        }
        if span.parent_id.is_empty() {
            continue;
        }
        let Some(&parent) = index.get(span.parent_id) else {
            return Err(("missing_parent", Some(window)));
        };
        if span.start < spans[parent].start || span.end > spans[parent].end {
            return Err(("clock_inconsistency", Some(window)));
        }
    }
    let mut children: HashMap<&str, Vec<usize>> = HashMap::new();
    for (position, span) in spans.iter().enumerate() {
        children.entry(span.parent_id).or_default().push(position);
    }
    let count = spans.len();
    let mut trace = EligibleTrace {
        id,
        spans,
        index,
        root,
        window,
        paths: Vec::new(),
        paired: vec![false; count],
        pair_of: vec![None; count],
        children,
    };
    for position in 0..count {
        let span = trace.spans[position];
        // A root client stays visible: collapsing it would leave its server
        // without a visible parent. (The Go code excludes such a trace as a
        // cycle; it does not occur when the root is a server span.)
        if span.kind != SpanKind::Client || span.parent_id.is_empty() {
            continue;
        }
        let mut matched = None;
        for &child in trace.children.get(span.span_id).into_iter().flatten() {
            let candidate = trace.spans[child];
            if candidate.kind == SpanKind::Server && candidate.service != span.service {
                if matched.is_some() {
                    matched = None;
                    break;
                }
                matched = Some(child);
            }
        }
        if let Some(server) = matched {
            trace.paired[position] = true;
            trace.pair_of[position] = Some(server);
        }
    }
    trace.paths = trace_paths(&trace).ok_or(("cycle", Some(window)))?;
    Ok(trace)
}

/// Each span's path: visible ancestors' `service:operation`, joined by ` > `.
fn trace_paths(trace: &EligibleTrace) -> Option<Vec<String>> {
    let count = trace.spans.len();
    let mut paths: Vec<Option<String>> = vec![None; count];
    for start in 0..count {
        let mut chain = Vec::new();
        let mut current = start;
        let base = loop {
            if let Some(path) = &paths[current] {
                break Some(path.clone());
            }
            if chain.len() > count {
                return None;
            }
            chain.push(current);
            if trace.spans[current].parent_id.is_empty() {
                break None;
            }
            current = trace.visible_parent(current)?;
        };
        let mut path = base;
        for &position in chain.iter().rev() {
            let span = trace.spans[position];
            let segment = format!("{}:{}", span.service, span.operation);
            let full = match path {
                Some(parent) => format!("{parent} > {segment}"),
                None => segment,
            };
            paths[position] = Some(full.clone());
            path = Some(full);
        }
    }
    paths.into_iter().collect()
}

struct CriticalSpan {
    start: u64,
    end: u64,
    depth: usize,
    children: Vec<usize>,
}

pub(crate) struct Segment {
    pub span: usize,
    pub start: u64,
    pub end: u64,
    pub failed: bool,
}

pub(crate) struct TraceCriticalPath {
    pub root_start: u64,
    pub root_end: u64,
    pub segments: Vec<Segment>,
    pub async_excluded: usize,
}

/// Attribute every instant of the root span to the deepest span active on the
/// synchronous path. A paired client's server span takes the client's
/// interval, so transport time counts toward the remote call.
pub(crate) fn critical_path(trace: &EligibleTrace) -> Result<TraceCriticalPath, String> {
    let count = trace.spans.len();
    let mut visible: Vec<Option<CriticalSpan>> = (0..count).map(|_| None).collect();
    let mut parents = vec![None; count];
    for position in 0..count {
        if trace.paired[position] {
            continue;
        }
        let span = trace.spans[position];
        let (mut start, mut end) = (span.start, span.end);
        if let Some(parent) = trace.parent(position).filter(|&p| trace.paired[p]) {
            if trace.pair_of[parent] == Some(position) {
                (start, end) = (trace.spans[parent].start, trace.spans[parent].end);
            }
        }
        parents[position] = trace.visible_parent(position);
        visible[position] = Some(CriticalSpan {
            start,
            end,
            depth: 0,
            children: Vec::new(),
        });
    }
    let mut root = None;
    let mut async_excluded = 0;
    for position in 0..count {
        if visible[position].is_none() {
            continue;
        }
        match parents[position] {
            None => {
                if root.is_some() {
                    return Err(format!(
                        "trace {} critical path has multiple visible roots",
                        trace.id
                    ));
                }
                root = Some(position);
            }
            Some(parent) => {
                if trace.spans[parent].kind == SpanKind::Producer
                    || trace.spans[position].kind == SpanKind::Consumer
                {
                    async_excluded += 1;
                } else if let Some(node) = visible[parent].as_mut() {
                    node.children.push(position);
                }
            }
        }
    }
    let root = root.ok_or_else(|| format!("trace {} has no visible root", trace.id))?;
    async_excluded += trace.spans.iter().map(|s| s.links.len()).sum::<usize>();

    let mut reachable = Vec::new();
    let mut stack = vec![(root, 0)];
    while let Some((position, depth)) = stack.pop() {
        reachable.push(position);
        let node = visible[position].as_mut().ok_or("invisible span")?;
        node.depth = depth;
        stack.extend(node.children.iter().map(|&child| (child, depth + 1)));
    }
    let (root_start, root_end) = visible[root]
        .as_ref()
        .map(|n| (n.start, n.end))
        .unwrap_or_default();
    let segments = active_leaf_segments(trace, &visible, &reachable, (root_start, root_end));
    let total: u64 = segments.iter().map(|s| s.end - s.start).sum();
    if total != root_end - root_start {
        return Err(format!(
            "trace {} critical path covers {total} ns, want {} ns",
            trace.id,
            root_end - root_start
        ));
    }
    Ok(TraceCriticalPath {
        root_start,
        root_end,
        segments,
        async_excluded,
    })
}

/// Split the root interval at every span boundary and give each piece to the
/// active span with no active child; ties go to the span ending last, then
/// the deepest, then the smallest span ID. Adjacent pieces of one path merge.
fn active_leaf_segments(
    trace: &EligibleTrace,
    visible: &[Option<CriticalSpan>],
    reachable: &[usize],
    (root_start, root_end): (u64, u64),
) -> Vec<Segment> {
    let node = |position: usize| visible[position].as_ref();
    let mut boundaries = vec![root_start, root_end];
    for &position in reachable {
        if let Some(n) = node(position) {
            boundaries.extend([n.start, n.end]);
        }
    }
    boundaries.sort_unstable();
    boundaries.dedup();
    let mut active = vec![false; visible.len()];
    let mut segments: Vec<Segment> = Vec::new();
    for pair in boundaries.windows(2) {
        let (start, end) = (pair[0], pair[1]);
        for &position in reachable {
            active[position] = node(position).is_some_and(|n| n.start <= start && n.end >= end);
        }
        let mut selected: Option<(usize, &CriticalSpan)> = None;
        for &position in reachable {
            let Some(candidate) = node(position) else {
                continue;
            };
            if !active[position] || candidate.children.iter().any(|&c| active[c]) {
                continue;
            }
            let better = selected.is_none_or(|(current, chosen)| {
                candidate.end > chosen.end
                    || (candidate.end == chosen.end && candidate.depth > chosen.depth)
                    || (candidate.end == chosen.end
                        && candidate.depth == chosen.depth
                        && trace.spans[position].span_id < trace.spans[current].span_id)
            });
            if better {
                selected = Some((position, candidate));
            }
        }
        if let Some((position, _)) = selected {
            let failed = trace.spans[position].failed;
            match segments.last_mut() {
                Some(last)
                    if trace.paths[last.span] == trace.paths[position]
                        && last.end == start
                        && last.failed == failed =>
                {
                    last.end = end;
                }
                _ => segments.push(Segment {
                    span: position,
                    start,
                    end,
                    failed,
                }),
            }
        }
    }
    segments
}

#[derive(Default)]
struct NodeAggregate {
    service: String,
    operation: String,
    kind: SpanKind,
    inclusive: Vec<Sample>,
    exclusive: Vec<Sample>,
    offsets: Vec<f64>,
    client: Vec<Sample>,
    network: Vec<Sample>,
    client_label: Option<(String, String)>,
}

pub(crate) struct RootAggregate<'a> {
    service: String,
    operation: String,
    traces: Vec<EligibleTrace<'a>>,
    critical: Vec<TraceCriticalPath>,
    nodes: BTreeMap<String, NodeAggregate>,
    edges: BTreeMap<(String, String, &'static str), usize>,
}

fn exclusive_ms(trace: &EligibleTrace, position: usize) -> f64 {
    let span = trace.spans[position];
    let mut intervals: Vec<(u64, u64)> = trace
        .children
        .get(span.span_id)
        .into_iter()
        .flatten()
        .map(|&child| (trace.spans[child].start, trace.spans[child].end))
        .collect();
    intervals.sort_by_key(|interval| interval.0);
    let mut covered = 0;
    let mut index = 0;
    while index < intervals.len() {
        let (start, mut end) = intervals[index];
        index += 1;
        while index < intervals.len() && intervals[index].0 <= end {
            end = end.max(intervals[index].1);
            index += 1;
        }
        covered += end - start;
    }
    duration_ms(covered, span.end - span.start)
}

/// Aggregates eligible traces by root endpoint and tracks trace quality.
#[derive(Default)]
pub(crate) struct TraceGraphBuilder<'a> {
    pub quality: TraceQuality,
    roots: BTreeMap<(String, String), RootAggregate<'a>>,
}

impl<'a> TraceGraphBuilder<'a> {
    pub fn new(windows: &[NamedWindow]) -> Self {
        Self {
            quality: TraceQuality {
                windows: windows
                    .iter()
                    .map(|w| WindowQuality {
                        label: w.label.clone(),
                        ..WindowQuality::default()
                    })
                    .collect(),
                ..TraceQuality::default()
            },
            roots: BTreeMap::new(),
        }
    }

    pub fn add(
        &mut self,
        id: &'a str,
        spans: Vec<&'a SpanRef<'a>>,
        windows: &[NamedWindow],
    ) -> Result<(), String> {
        self.quality.captured_traces += 1;
        let trace = match qualify(id, spans, windows) {
            Ok(trace) => trace,
            Err((reason, window)) => {
                self.quality.excluded_traces += 1;
                *self
                    .quality
                    .exclusion_reasons
                    .entry(reason.into())
                    .or_default() += 1;
                if let Some(window) = window {
                    self.quality.windows[window].captured_traces += 1;
                    self.quality.windows[window].excluded_traces += 1;
                }
                return Ok(());
            }
        };
        self.quality.eligible_traces += 1;
        self.quality.windows[trace.window].captured_traces += 1;
        self.quality.windows[trace.window].eligible_traces += 1;
        self.quality.matched_client_server_pairs += trace.paired.iter().filter(|p| **p).count();
        let root = trace.spans[trace.root];
        let aggregate = self
            .roots
            .entry((root.service.to_owned(), root.operation.to_owned()))
            .or_insert_with(|| RootAggregate {
                service: root.service.to_owned(),
                operation: root.operation.to_owned(),
                traces: Vec::new(),
                critical: Vec::new(),
                nodes: BTreeMap::new(),
                edges: BTreeMap::new(),
            });
        aggregate.critical.push(critical_path(&trace)?);
        add_trace_nodes(aggregate, &trace, &mut self.quality);
        aggregate.traces.push(trace);
        Ok(())
    }

    pub fn finish(self) -> (TraceQuality, Vec<RootGraph>) {
        let roots = self.roots.values().map(materialize).collect();
        (self.quality, roots)
    }
}

fn add_trace_nodes(
    aggregate: &mut RootAggregate,
    trace: &EligibleTrace,
    quality: &mut TraceQuality,
) {
    let root_start = trace.spans[trace.root].start;
    let mut visible_by_path: BTreeMap<&str, usize> = BTreeMap::new();
    for position in 0..trace.spans.len() {
        if trace.paired[position] {
            continue;
        }
        let span = trace.spans[position];
        let path = trace.paths[position].as_str();
        let node = aggregate
            .nodes
            .entry(path.to_owned())
            .or_insert_with(|| NodeAggregate {
                service: span.service.to_owned(),
                operation: span.operation.to_owned(),
                kind: span.kind,
                ..NodeAggregate::default()
            });
        node.inclusive.push(Sample {
            ms: trace.duration(position),
            failed: span.failed,
        });
        node.exclusive.push(Sample {
            ms: exclusive_ms(trace, position),
            failed: span.failed,
        });
        node.offsets.push(duration_ms(root_start, span.start));
        let parent = trace.parent(position);
        if let Some(client) = parent.filter(|&p| trace.pair_of[p] == Some(position)) {
            let client_ms = trace.duration(client);
            node.client.push(Sample {
                ms: client_ms,
                failed: trace.spans[client].failed,
            });
            node.network.push(Sample {
                ms: client_ms - trace.duration(position),
                failed: false,
            });
            node.client_label.get_or_insert_with(|| {
                let client = trace.spans[client];
                (client.service.to_owned(), client.operation.to_owned())
            });
        }
        visible_by_path.insert(path, position);
        if span.kind == SpanKind::Client {
            quality.unmatched_client_spans += 1;
        }
        if span.kind == SpanKind::Server
            && !span.parent_id.is_empty()
            && !parent.is_some_and(|p| trace.paired[p])
        {
            quality.unmatched_server_spans += 1;
        }
    }
    for (path, position) in visible_by_path {
        let span = trace.spans[position];
        let Some(parent) = trace.visible_parent(position) else {
            continue;
        };
        let relationship =
            if trace.spans[parent].kind == SpanKind::Producer || span.kind == SpanKind::Consumer {
                quality.async_relationships += 1;
                "async"
            } else {
                "sync"
            };
        *aggregate
            .edges
            .entry((trace.paths[parent].clone(), path.to_owned(), relationship))
            .or_default() += 1;
        for link in span.links {
            if link.trace_id != trace.id {
                continue;
            }
            let Some(&linked) = trace.index.get(link.span_id.as_str()) else {
                continue;
            };
            if trace.paired[linked] {
                continue;
            }
            *aggregate
                .edges
                .entry((trace.paths[linked].clone(), path.to_owned(), "link"))
                .or_default() += 1;
            quality.async_relationships += 1;
        }
    }
}

fn representative<'t>(traces: &'t [EligibleTrace<'t>], target: f64) -> &'t EligibleTrace<'t> {
    let root_ms = |t: &EligibleTrace| t.duration(t.root);
    let healthy: Vec<&EligibleTrace> = traces.iter().filter(|t| !t.spans[t.root].failed).collect();
    let candidates: Vec<&EligibleTrace> = if healthy.is_empty() {
        traces.iter().collect()
    } else {
        healthy
    };
    candidates
        .into_iter()
        .min_by(|a, b| {
            (root_ms(a) - target)
                .abs()
                .total_cmp(&(root_ms(b) - target).abs())
                .then_with(|| a.id.cmp(b.id))
        })
        .unwrap_or(&traces[0])
}

fn waterfall(
    trace: &EligibleTrace,
    label: &str,
    ids: &BTreeMap<&str, String>,
) -> RepresentativeTrace {
    let root_start = trace.spans[trace.root].start;
    let mut spans = Vec::with_capacity(trace.spans.len());
    let mut stack = vec![(trace.root, 0)];
    while let Some((position, depth)) = stack.pop() {
        let span = trace.spans[position];
        spans.push(WaterfallSpan {
            depth,
            node_id: (!trace.paired[position])
                .then(|| ids.get(trace.paths[position].as_str()).cloned())
                .flatten(),
            service: span.service.to_owned(),
            operation: span.operation.to_owned(),
            kind: span.kind,
            failed: span.failed,
            offset_ms: duration_ms(root_start, span.start),
            duration_ms: trace.duration(position),
        });
        let mut children: Vec<usize> = trace
            .children
            .get(span.span_id)
            .cloned()
            .unwrap_or_default();
        children.sort_by(|&a, &b| {
            (trace.spans[b].start, trace.spans[b].span_id)
                .cmp(&(trace.spans[a].start, trace.spans[a].span_id))
        });
        stack.extend(children.into_iter().map(|child| (child, depth + 1)));
    }
    RepresentativeTrace {
        label: label.to_owned(),
        trace_id: trace.id.to_owned(),
        duration_ms: trace.duration(trace.root),
        spans,
    }
}

fn parent_path(path: &str) -> Option<&str> {
    path.rfind(" > ").map(|index| &path[..index])
}

fn materialize(aggregate: &RootAggregate) -> RootGraph {
    let trace_count = aggregate.traces.len();
    let ids: BTreeMap<&str, String> = aggregate
        .nodes
        .keys()
        .enumerate()
        .map(|(index, path)| (path.as_str(), format!("node-{:03}", index + 1)))
        .collect();
    let nodes: Vec<GraphNode> = aggregate
        .nodes
        .iter()
        .map(|(path, node)| {
            let mut offsets = node.offsets.clone();
            offsets.sort_by(f64::total_cmp);
            GraphNode {
                id: ids[path.as_str()].clone(),
                parent_id: parent_path(path).and_then(|p| ids.get(p).cloned()),
                path: path.clone(),
                service: node.service.clone(),
                operation: node.operation.clone(),
                kind: node.kind,
                calls_per_request: crate::stats::count(node.inclusive.len())
                    / crate::stats::count(trace_count),
                median_start_offset_ms: crate::stats::percentile(&offsets, 50.0),
                inclusive: Distribution::of(&node.inclusive),
                exclusive: Distribution::of(&node.exclusive),
                hop: node.client_label.as_ref().map(|(service, operation)| Hop {
                    client_service: service.clone(),
                    client_operation: operation.clone(),
                    client: Distribution::of(&node.client),
                    network: Distribution::of(&node.network),
                }),
            }
        })
        .collect();
    let edges = aggregate
        .edges
        .iter()
        .map(|((from, to, relationship), count)| GraphEdge {
            from: ids[from.as_str()].clone(),
            to: ids[to.as_str()].clone(),
            relationship: (*relationship).to_owned(),
            count: *count,
        })
        .collect();
    let root_samples: Vec<Sample> = aggregate
        .traces
        .iter()
        .map(|t| Sample {
            ms: t.duration(t.root),
            failed: t.spans[t.root].failed,
        })
        .collect();
    let latency = Distribution::of(&root_samples);
    let p50 = representative(&aggregate.traces, latency.p50_ms);
    let p95 = representative(&aggregate.traces, latency.p95_ms);
    let critical_path = critical_summary(aggregate, &ids, &nodes, p95.id, latency);
    RootGraph {
        service: aggregate.service.clone(),
        operation: aggregate.operation.clone(),
        trace_count,
        error_count: latency.error_count,
        latency,
        representatives: vec![waterfall(p50, "p50", &ids), waterfall(p95, "p95", &ids)],
        nodes,
        edges,
        critical_path,
    }
}

fn critical_summary(
    aggregate: &RootAggregate,
    ids: &BTreeMap<&str, String>,
    nodes: &[GraphNode],
    representative_id: &str,
    latency: Distribution,
) -> CriticalPathSummary {
    let mut by_path: BTreeMap<&str, Vec<Sample>> = BTreeMap::new();
    let mut async_excluded = 0;
    let mut representative = None;
    for (trace, critical) in aggregate.traces.iter().zip(&aggregate.critical) {
        async_excluded += critical.async_excluded;
        let mut per_trace: BTreeMap<&str, Sample> = BTreeMap::new();
        for segment in &critical.segments {
            let sample = per_trace
                .entry(trace.paths[segment.span].as_str())
                .or_default();
            sample.ms += duration_ms(segment.start, segment.end);
            sample.failed |= segment.failed;
        }
        for (path, sample) in per_trace {
            by_path.entry(path).or_default().push(sample);
        }
        if trace.id == representative_id {
            representative = Some((trace, critical));
        }
    }
    let total_ms = latency.mean_ms * crate::stats::count(aggregate.traces.len());
    let node_by_id: HashMap<&str, &GraphNode> = nodes.iter().map(|n| (n.id.as_str(), n)).collect();
    let mut contributors: Vec<CriticalContribution> = by_path
        .into_iter()
        .map(|(path, samples)| {
            let node = node_by_id[ids[path].as_str()];
            let contribution = Distribution::of(&samples);
            let sum_ms = contribution.mean_ms * crate::stats::count(contribution.count);
            CriticalContribution {
                node_id: node.id.clone(),
                path: path.to_owned(),
                service: node.service.clone(),
                operation: node.operation.clone(),
                contribution,
                per_request_ms: sum_ms / crate::stats::count(aggregate.traces.len()),
                share: if total_ms > 0.0 {
                    sum_ms / total_ms
                } else {
                    0.0
                },
            }
        })
        .collect();
    contributors.sort_by(|a, b| {
        b.contribution
            .mean_ms
            .total_cmp(&a.contribution.mean_ms)
            .then_with(|| a.path.cmp(&b.path))
    });
    let (trace_id, duration, segments) = representative.map_or_else(
        || (String::new(), 0.0, Vec::new()),
        |(trace, critical)| {
            (
                trace.id.to_owned(),
                duration_ms(critical.root_start, critical.root_end),
                critical
                    .segments
                    .iter()
                    .map(|s| CriticalSegment {
                        node_id: ids[trace.paths[s.span].as_str()].clone(),
                        service: trace.spans[s.span].service.to_owned(),
                        operation: trace.spans[s.span].operation.to_owned(),
                        offset_ms: duration_ms(critical.root_start, s.start),
                        duration_ms: duration_ms(s.start, s.end),
                    })
                    .collect(),
            )
        },
    );
    CriticalPathSummary {
        algorithm: CRITICAL_PATH_ALGORITHM.into(),
        scope: CRITICAL_PATH_SCOPE.into(),
        trace_count: aggregate.traces.len(),
        async_relationships_excluded: async_excluded,
        duration: latency,
        nodes: contributors,
        representative_trace_id: trace_id,
        representative_duration_ms: duration,
        representative_segments: segments,
    }
}
