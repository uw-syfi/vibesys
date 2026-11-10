//! Bounded, deterministic text and JSON renderings of an [`Analysis`].

use crate::stats::count;
use crate::{
    Analysis, AnalysisError, Distribution, GraphNode, LatencyRow, RepresentativeTrace, RootGraph,
    CRITICAL_PATH_ALGORITHM,
};
use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt::Write as _;
use stitch_core::SpanKind;

/// Waterfalls list at most this many spans per representative trace.
pub const WATERFALL_SPAN_LIMIT: usize = 100;
const NAME_LIMIT: usize = 72;
const RULE_WIDTH: usize = 100;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RenderOptions {
    /// Rows per latency table.
    pub top: usize,
    /// Root endpoints per text file.
    pub max_roots: usize,
    /// Call-graph nodes, critical-path rows and path segments per root.
    pub max_nodes: usize,
    /// Characters in a waterfall bar.
    pub width: usize,
}

impl Default for RenderOptions {
    fn default() -> Self {
        Self {
            top: 20,
            max_roots: 10,
            max_nodes: 30,
            width: 48,
        }
    }
}

/// One output file: a name relative to the output directory, and its text.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RenderedFile {
    pub name: &'static str,
    pub contents: String,
}

// `write!` into a `String` cannot fail; this keeps call sites free of
// `unwrap` and of ignored results.
macro_rules! out {
    ($buffer:expr, $($arg:tt)*) => {{
        let _ = write!($buffer, $($arg)*);
    }};
}

fn plural(n: usize, one: &str, many: &str) -> String {
    format!("{n} {}", if n == 1 { one } else { many })
}

fn truncate(name: &str, limit: usize) -> String {
    if name.chars().count() <= limit {
        return name.to_owned();
    }
    let mut short: String = name.chars().take(limit.saturating_sub(1)).collect();
    short.push('…');
    short
}

fn root_label(root: &RootGraph) -> String {
    format!("{}: {}", root.service, root.operation)
}

fn latency_line(latency: &Distribution) -> String {
    format!(
        "mean {:.2} ms  p50 {:.2} ms  p95 {:.2} ms  p99 {:.2} ms  max {:.2} ms",
        latency.mean_ms, latency.p50_ms, latency.p95_ms, latency.p99_ms, latency.max_ms
    )
}

fn kind_name(kind: SpanKind) -> &'static str {
    match kind {
        SpanKind::Internal => "internal",
        SpanKind::Server => "server",
        SpanKind::Client => "client",
        SpanKind::Producer => "producer",
        SpanKind::Consumer => "consumer",
    }
}

/// One loud paragraph per operation family, or nothing.
fn family_warnings(analysis: &Analysis) -> String {
    let mut output = String::new();
    for family in &analysis.operation_families {
        out!(
            output,
            "WARNING: {} span names of service {} look like one operation with request data in \
             the name (e.g. {}); aggregated as {}. Fix the instrumentation: put the data in an \
             attribute.\n",
            family.distinct_names,
            family.service,
            family.examples.join(", "),
            family.pattern
        );
    }
    if !output.is_empty() {
        output.push('\n');
    }
    output
}

/// A one-line pointer to the warnings, for files other than README and report.
fn family_note(analysis: &Analysis) -> String {
    match analysis.operation_families.len() {
        0 => String::new(),
        n => format!(
            "NOTE: {} aggregated as `prefix*`; operation names embed request data (see README.txt).\n",
            plural(n, "span-name family is", "span-name families are")
        ),
    }
}

fn windows_text(analysis: &Analysis) -> String {
    if analysis.windows.is_empty() {
        return "none (every span in the graph)".into();
    }
    analysis
        .windows
        .iter()
        .map(|w| {
            format!(
                "{} [{}, {}] ({:.1} s)",
                w.label,
                w.start_unix_nanos,
                w.end_unix_nanos,
                crate::stats::duration_ms(w.start_unix_nanos, w.end_unix_nanos) / 1000.0
            )
        })
        .collect::<Vec<_>>()
        .join("; ")
}

fn quality_line(analysis: &Analysis) -> String {
    let quality = &analysis.quality;
    let mut line = format!(
        "eligible traces {}/{} (excluded {}",
        quality.eligible_traces, quality.captured_traces, quality.excluded_traces
    );
    if !quality.exclusion_reasons.is_empty() {
        let reasons: Vec<String> = quality
            .exclusion_reasons
            .iter()
            .map(|(reason, n)| format!("{reason} {n}"))
            .collect();
        out!(line, ": {}", reasons.join(", "));
    }
    line.push(')');
    line
}

fn table(output: &mut String, title: &str, column: &str, rows: &[LatencyRow], top: usize) {
    let shown = &rows[..rows.len().min(top)];
    out!(
        output,
        "\n{title} by p95 (top {} of {})\n",
        shown.len(),
        rows.len()
    );
    if shown.is_empty() {
        output.push_str("  (none)\n");
        return;
    }
    let names: Vec<String> = shown
        .iter()
        .map(|r| truncate(&r.name, NAME_LIMIT))
        .collect();
    let width = names
        .iter()
        .map(|n| n.chars().count())
        .chain([column.len()])
        .max()
        .unwrap_or(0);
    out!(
        output,
        "  {column:<width$}  {:>8}  {:>6}  {:>9}  {:>9}  {:>9}  {:>9}  {:>9}\n",
        "count",
        "errors",
        "mean ms",
        "p50 ms",
        "p95 ms",
        "p99 ms",
        "max ms"
    );
    for (row, name) in shown.iter().zip(names) {
        let l = &row.latency;
        let pad = width - name.chars().count();
        out!(
            output,
            "  {name}{}  {:>8}  {:>6}  {:>9.2}  {:>9.2}  {:>9.2}  {:>9.2}  {:>9.2}\n",
            " ".repeat(pad),
            l.count,
            l.error_count,
            l.mean_ms,
            l.p50_ms,
            l.p95_ms,
            l.p99_ms,
            l.max_ms
        );
    }
}

fn report_text(analysis: &Analysis, options: &RenderOptions) -> String {
    let report = &analysis.report;
    let mut output = String::from("STITCH TRACE REPORT\n");
    output.push_str(&family_warnings(analysis));
    out!(output, "windows: {}\n", windows_text(analysis));
    let error_rate = if report.span_count == 0 {
        0.0
    } else {
        100.0 * count(report.error_count) / count(report.span_count)
    };
    out!(
        output,
        "spans {}  traces {}  errors {} ({error_rate:.2}% of spans)\n",
        report.span_count,
        report.trace_count,
        report.error_count
    );
    out!(output, "{}\n", quality_line(analysis));
    table(
        &mut output,
        "SERVICES",
        "service",
        &report.services,
        options.top,
    );
    table(
        &mut output,
        "SPANS",
        "service: operation",
        &report.spans,
        options.top,
    );
    table(
        &mut output,
        "DATASTORES (client spans with db.system)",
        "caller → system: operation",
        &report.datastores,
        options.top,
    );
    output
}

/// The nodes to draw: largest total inclusive time first, with every
/// ancestor of a chosen node also chosen, within `limit` nodes.
fn chosen_nodes(root: &RootGraph, limit: usize) -> BTreeSet<&str> {
    let by_id: HashMap<&str, &GraphNode> = root.nodes.iter().map(|n| (n.id.as_str(), n)).collect();
    let mut ranked: Vec<&GraphNode> = root.nodes.iter().collect();
    ranked.sort_by(|a, b| {
        let total = |n: &GraphNode| n.inclusive.mean_ms * count(n.inclusive.count);
        total(b).total_cmp(&total(a)).then_with(|| a.id.cmp(&b.id))
    });
    let mut chosen = BTreeSet::new();
    for node in ranked {
        let mut missing = Vec::new();
        let mut current = Some(node);
        while let Some(n) = current {
            if chosen.contains(n.id.as_str()) {
                break;
            }
            missing.push(n.id.as_str());
            current = n.parent_id.as_deref().and_then(|p| by_id.get(p).copied());
        }
        if chosen.len() + missing.len() <= limit {
            chosen.extend(missing);
        }
    }
    chosen
}

fn tree_node(
    output: &mut String,
    node: &GraphNode,
    children: &HashMap<&str, Vec<&GraphNode>>,
    chosen: &BTreeSet<&str>,
    prefix: &str,
    branch: Option<bool>,
) {
    let (connector, child_prefix) = match branch {
        None => (String::new(), prefix.to_owned()),
        Some(last) => {
            let arrow = if node.hop.is_some() {
                "─▶ "
            } else {
                "── "
            };
            (
                format!("{}{arrow}", if last { "└" } else { "├" }),
                format!("{prefix}{}", if last { "    " } else { "│   " }),
            )
        }
    };
    let kids: Vec<&GraphNode> = children
        .get(node.id.as_str())
        .map(|c| {
            c.iter()
                .copied()
                .filter(|c| chosen.contains(c.id.as_str()))
                .collect()
        })
        .unwrap_or_default();
    let hidden = children.get(node.id.as_str()).map_or(0, Vec::len) - kids.len();
    let detail = format!(
        "{child_prefix}{}",
        if kids.is_empty() { "     " } else { "│    " }
    );
    let kind = match (&node.hop, node.kind) {
        (Some(_), _) => "rpc".to_owned(),
        (None, kind) => kind_name(kind).to_owned(),
    };
    out!(
        output,
        "{prefix}{connector}{}: {}  [{kind}]  ×{:.2}/req\n",
        node.service,
        node.operation,
        node.calls_per_request
    );
    if let Some(hop) = &node.hop {
        out!(
            output,
            "{detail}client {:.2} / {:.2} ms   server {:.2} / {:.2} ms   network+queue {:.2} / {:.2} ms\n",
            hop.client.p50_ms,
            hop.client.p95_ms,
            node.inclusive.p50_ms,
            node.inclusive.p95_ms,
            hop.network.p50_ms,
            hop.network.p95_ms
        );
        out!(
            output,
            "{detail}self {:.2} / {:.2} ms\n",
            node.exclusive.p50_ms,
            node.exclusive.p95_ms
        );
    } else {
        out!(
            output,
            "{detail}incl {:.2} / {:.2} ms   self {:.2} / {:.2} ms\n",
            node.inclusive.p50_ms,
            node.inclusive.p95_ms,
            node.exclusive.p50_ms,
            node.exclusive.p95_ms
        );
    }
    if hidden > 0 {
        out!(
            output,
            "{detail}… {} not shown\n",
            plural(hidden, "child node", "child nodes")
        );
    }
    for (index, child) in kids.iter().enumerate() {
        tree_node(
            output,
            child,
            children,
            chosen,
            &child_prefix,
            Some(index + 1 == kids.len()),
        );
    }
}

fn root_header(output: &mut String, root: &RootGraph) {
    out!(output, "{}\n", "═".repeat(RULE_WIDTH));
    out!(
        output,
        "ROOT {}   requests {}  errors {}\n",
        root_label(root),
        root.trace_count,
        root.error_count
    );
    out!(output, "latency {}\n\n", latency_line(&root.latency));
}

fn roots_shown<'a>(analysis: &'a Analysis, options: &RenderOptions) -> &'a [RootGraph] {
    &analysis.roots[..analysis.roots.len().min(options.max_roots)]
}

fn omitted_roots(output: &mut String, analysis: &Analysis, options: &RenderOptions) {
    let omitted = analysis.roots.len().saturating_sub(options.max_roots);
    if omitted > 0 {
        out!(
            output,
            "{}\n… {} omitted (raise --max-roots)\n",
            "═".repeat(RULE_WIDTH),
            plural(omitted, "root endpoint", "root endpoints")
        );
    }
}

fn no_roots(output: &mut String, analysis: &Analysis) {
    if analysis.roots.is_empty() {
        out!(
            output,
            "No eligible traces: {}. See README.txt.\n",
            quality_line(analysis)
        );
    }
}

fn tree_text(analysis: &Analysis, options: &RenderOptions) -> String {
    let mut output = String::from("STITCH CALL GRAPH\n");
    output.push_str(&family_note(analysis));
    output.push_str(
        "Per root endpoint, every eligible trace merged into one tree. Each node is \
         `service: operation  [kind]  ×calls per root request`.\n\
         ─▶ marks a remote call: a client span paired with the server span it caused, shown as one \
         hop. Its client time is the caller's view, server time the callee's, and network+queue \
         their difference.\n\
         ── marks an in-process child. Times are p50 / p95 in ms; incl is the node's wall time, \
         self subtracts the union of its direct children.\n\
         Children are ordered by median start offset.\n",
    );
    out!(output, "{}\n\n", quality_line(analysis));
    no_roots(&mut output, analysis);
    for root in roots_shown(analysis, options) {
        root_header(&mut output, root);
        let chosen = chosen_nodes(root, options.max_nodes);
        let mut children: HashMap<&str, Vec<&GraphNode>> = HashMap::new();
        let mut tops = Vec::new();
        for node in &root.nodes {
            match &node.parent_id {
                Some(parent) => children.entry(parent.as_str()).or_default().push(node),
                None => tops.push(node),
            }
        }
        for list in children.values_mut() {
            list.sort_by(|a, b| {
                a.median_start_offset_ms
                    .total_cmp(&b.median_start_offset_ms)
                    .then_with(|| a.path.cmp(&b.path))
            });
        }
        for top in tops.into_iter().filter(|n| chosen.contains(n.id.as_str())) {
            tree_node(&mut output, top, &children, &chosen, "", None);
        }
        let hidden = root.nodes.len() - chosen.len();
        if hidden > 0 {
            out!(
                output,
                "\n{} of {} not shown (raise --max-nodes)\n",
                plural(hidden, "node", "nodes"),
                root.nodes.len()
            );
        }
        output.push('\n');
    }
    omitted_roots(&mut output, analysis, options);
    output
}

fn critical_text(analysis: &Analysis, options: &RenderOptions) -> String {
    let mut output = String::from("STITCH CRITICAL PATH\n");
    output.push_str(&family_note(analysis));
    out!(
        output,
        "Algorithm {CRITICAL_PATH_ALGORITHM}: at each instant of the root span, the deepest span \
         active on the synchronous path is critical; among parallel children the one ending last \
         wins. A remote call's server node also owns its client's interval, so network time counts \
         toward the callee. Segments cover the root's wall time exactly; a parent's own time \
         between children is its self time. Producer/consumer and link relationships are excluded.\n\
         share = this node's critical time / all root wall time. ms/req = critical time per root \
         request. mean and p95 are over the traces where the node was critical (on-path).\n\n"
    );
    no_roots(&mut output, analysis);
    for root in roots_shown(analysis, options) {
        root_header(&mut output, root);
        let critical = &root.critical_path;
        let labels: Vec<String> = critical
            .nodes
            .iter()
            .map(|n| format!("{}: {}", n.service, n.operation))
            .collect();
        let mut repeated: BTreeMap<&str, usize> = BTreeMap::new();
        for label in &labels {
            *repeated.entry(label.as_str()).or_default() += 1;
        }
        out!(
            output,
            "{:>4}  {:>6}  {:>8}  {:>8}  {:>8}  {:>13}  node\n",
            "rank",
            "share",
            "ms/req",
            "mean ms",
            "p95 ms",
            "on-path"
        );
        let shown = critical.nodes.len().min(options.max_nodes);
        for (index, (node, label)) in critical.nodes.iter().zip(&labels).take(shown).enumerate() {
            let label = if repeated[label.as_str()] > 1 {
                format!("{label}   (path {})", node.path)
            } else {
                label.clone()
            };
            out!(
                output,
                "{:>4}  {:>5.1}%  {:>8.2}  {:>8.2}  {:>8.2}  {:>13}  {label}\n",
                index + 1,
                node.share * 100.0,
                node.per_request_ms,
                node.contribution.mean_ms,
                node.contribution.p95_ms,
                format!("{}/{}", node.contribution.count, critical.trace_count)
            );
        }
        if critical.nodes.len() > shown {
            out!(
                output,
                "… {} not shown (raise --max-nodes)\n",
                plural(critical.nodes.len() - shown, "contributor", "contributors")
            );
        }
        out!(
            output,
            "\nCritical path of the p95-representative trace {} ({:.2} ms):\n",
            critical.representative_trace_id,
            critical.representative_duration_ms
        );
        out!(output, "{:>10}  {:>9}  node\n", "offset ms", "dur ms");
        let segments = &critical.representative_segments;
        let shown = segments.len().min(options.max_nodes);
        for segment in &segments[..shown] {
            out!(
                output,
                "{:>10.2}  {:>9.2}  {}: {}\n",
                segment.offset_ms,
                segment.duration_ms,
                segment.service,
                segment.operation
            );
        }
        if segments.len() > shown {
            out!(
                output,
                "… {} not shown (raise --max-nodes)\n",
                plural(segments.len() - shown, "segment", "segments")
            );
        }
        out!(
            output,
            "async relationships excluded: {}\n\n",
            critical.async_relationships_excluded
        );
    }
    omitted_roots(&mut output, analysis, options);
    output
}

fn bar(offset: f64, duration: f64, total: f64, width: usize) -> String {
    if total <= 0.0 {
        return " ".repeat(width);
    }
    let cells = count(width);
    // Both positions are clamped to [0, width] before the conversion.
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    let position = |ms: f64| (ms / total * cells).clamp(0.0, cells) as usize;
    let mut start = position(offset).min(width - 1);
    let mut end = position(offset + duration).min(width);
    if end <= start {
        end = (start + 1).min(width);
        start = end - 1;
    }
    format!(
        "{}{}{}",
        " ".repeat(start),
        "█".repeat(end - start),
        " ".repeat(width - end)
    )
}

fn waterfall(output: &mut String, trace: &RepresentativeTrace, target: f64, width: usize) {
    out!(
        output,
        "{} representative  trace {}  {:.2} ms (root {} {:.2} ms)\n",
        trace.label,
        trace.trace_id,
        trace.duration_ms,
        trace.label,
        target
    );
    out!(
        output,
        "{:>9}  {:>9}  |{:<width$}|  span\n",
        "start ms",
        "dur ms",
        "timeline"
    );
    let shown = trace.spans.len().min(WATERFALL_SPAN_LIMIT);
    for span in &trace.spans[..shown] {
        let mut label = format!(
            "{}{}: {}",
            "  ".repeat(span.depth),
            span.service,
            span.operation
        );
        if span.kind != SpanKind::Internal {
            out!(label, " ({})", kind_name(span.kind));
        }
        if let Some(raw) = &span.raw_operation {
            out!(label, " [{}]", truncate(raw, NAME_LIMIT));
        }
        if span.failed {
            label.push_str(" ERROR");
        }
        out!(
            output,
            "{:>9.2}  {:>9.2}  |{}|  {label}\n",
            span.offset_ms,
            span.duration_ms,
            bar(span.offset_ms, span.duration_ms, trace.duration_ms, width)
        );
    }
    if trace.spans.len() > shown {
        out!(
            output,
            "… {} not shown\n",
            plural(trace.spans.len() - shown, "span", "spans")
        );
    }
    output.push('\n');
}

fn waterfall_text(analysis: &Analysis, options: &RenderOptions) -> String {
    let mut output = String::from("STITCH WATERFALLS\n");
    output.push_str(&family_note(analysis));
    output.push_str(
        "Per root endpoint, the eligible trace closest to the root's p50 latency and the one closest \
         to its p95 (preferring traces without a root error). One line per span, indented by depth, \
         in call order; client spans are listed with their server span beneath them.\n\n",
    );
    no_roots(&mut output, analysis);
    for root in roots_shown(analysis, options) {
        root_header(&mut output, root);
        for trace in &root.representatives {
            let target = if trace.label == "p50" {
                root.latency.p50_ms
            } else {
                root.latency.p95_ms
            };
            waterfall(&mut output, trace, target, options.width);
        }
    }
    omitted_roots(&mut output, analysis, options);
    output
}

fn readme_text(analysis: &Analysis, options: &RenderOptions) -> String {
    let mut output = String::from("STITCH TRACE ANALYSIS\n\n");
    output.push_str(&family_warnings(analysis));
    output.push_str(
        "Files (read the .txt files first; the .json files hold the same data unbounded):\n\
         \x20 report.txt          span, trace and error counts; latency tables by service, by\n\
         \x20                     `service: operation` and by datastore call, sorted by p95\n\
         \x20 report.json         the same tables, unbounded\n\
         \x20 tree.txt            per root endpoint, the aggregated call graph with client/server hops\n\
         \x20 critical-path.txt   per root endpoint, nodes ranked by critical-path contribution\n\
         \x20 waterfall.txt       per root endpoint, the p50 and p95 representative traces\n\
         \x20 graph-summary.json  roots, nodes, edges, critical paths and representatives\n\n",
    );
    out!(output, "Windows: {}\n", windows_text(analysis));
    let report = &analysis.report;
    out!(
        output,
        "Spans in windows: {}  traces: {}  span errors: {}\n",
        report.span_count,
        report.trace_count,
        report.error_count
    );
    let quality = &analysis.quality;
    out!(
        output,
        "Traces captured: {}  eligible for the call graph: {}  excluded: {}\n",
        quality.captured_traces,
        quality.eligible_traces,
        quality.excluded_traces
    );
    if quality.exclusion_reasons.is_empty() {
        output.push_str("Exclusions by reason: none\n");
    } else {
        output.push_str("Exclusions by reason:\n");
        for (reason, n) in &quality.exclusion_reasons {
            out!(output, "  {reason:<28} {n}\n");
        }
    }
    if quality.windows.len() > 1 {
        output.push_str("Per window (captured / eligible / excluded):\n");
        for window in &quality.windows {
            out!(
                output,
                "  {:<28} {} / {} / {}\n",
                window.label,
                window.captured_traces,
                window.eligible_traces,
                window.excluded_traces
            );
        }
    }
    out!(
        output,
        "Client/server pairs: {}  unmatched client spans: {}  unmatched server spans: {}  async relationships: {}\n",
        quality.matched_client_server_pairs,
        quality.unmatched_client_spans,
        quality.unmatched_server_spans,
        quality.async_relationships
    );
    out!(
        output,
        "Root endpoints: {} (text files show up to --max-roots {})\n\n",
        analysis.roots.len(),
        options.max_roots
    );
    output.push_str(
        "Counts are of sampled spans: with a sampler ratio below 1 they are a sample of requests.\n\
         Exclusion reasons: duplicate_span_id (a span ID repeats in a trace), multiple_roots,\n\
         missing_parent (a parent span was not captured, often a trace cut by the window or by the\n\
         sampler), outside_measurement_window (the root or a span is not inside one window),\n\
         clock_inconsistency (a child starts before or ends after its parent, typically clock skew\n\
         between hosts or work that outlives its request), cycle.\n\
         The latency tables cover every span inside the windows; the call graph, critical path\n\
         and waterfalls cover eligible traces only.\n\
         Span names of one service that share a prefix of 8+ characters and differ in a later\n\
         token containing digits are aggregated as `prefix*` when there are 50+ such names\n\
         averaging at most 10 spans each; waterfall.txt and the JSON keep the raw names.\n",
    );
    output
}

#[derive(Serialize)]
struct GraphSummary<'a> {
    schema_version: u32,
    critical_path_algorithm: &'static str,
    windows: &'a [crate::NamedWindow],
    quality: &'a crate::TraceQuality,
    operation_families: &'a [crate::OperationFamily],
    roots: &'a [RootGraph],
}

#[derive(Serialize)]
struct ReportJson<'a> {
    schema_version: u32,
    windows: &'a [crate::NamedWindow],
    quality: &'a crate::TraceQuality,
    operation_families: &'a [crate::OperationFamily],
    #[serde(flatten)]
    report: &'a crate::LatencyReport,
}

fn json(value: &impl Serialize) -> Result<String, AnalysisError> {
    let mut text =
        serde_json::to_string_pretty(value).map_err(|e| AnalysisError::Internal(e.to_string()))?;
    text.push('\n');
    Ok(text)
}

/// Render every output file.
///
/// # Errors
///
/// Returns an error for zero limits or a waterfall narrower than 10 characters.
pub fn render(
    analysis: &Analysis,
    options: &RenderOptions,
) -> Result<Vec<RenderedFile>, AnalysisError> {
    if options.top == 0 || options.max_roots == 0 || options.max_nodes == 0 {
        return Err(AnalysisError::Options(
            "--top, --max-roots and --max-nodes must be positive".into(),
        ));
    }
    if !(10..=400).contains(&options.width) {
        return Err(AnalysisError::Options(
            "--width must be between 10 and 400".into(),
        ));
    }
    Ok(vec![
        RenderedFile {
            name: "README.txt",
            contents: readme_text(analysis, options),
        },
        RenderedFile {
            name: "report.txt",
            contents: report_text(analysis, options),
        },
        RenderedFile {
            name: "report.json",
            contents: json(&ReportJson {
                schema_version: 1,
                windows: &analysis.windows,
                quality: &analysis.quality,
                operation_families: &analysis.operation_families,
                report: &analysis.report,
            })?,
        },
        RenderedFile {
            name: "tree.txt",
            contents: tree_text(analysis, options),
        },
        RenderedFile {
            name: "critical-path.txt",
            contents: critical_text(analysis, options),
        },
        RenderedFile {
            name: "waterfall.txt",
            contents: waterfall_text(analysis, options),
        },
        RenderedFile {
            name: "graph-summary.json",
            contents: json(&GraphSummary {
                schema_version: 1,
                critical_path_algorithm: CRITICAL_PATH_ALGORITHM,
                windows: &analysis.windows,
                quality: &analysis.quality,
                operation_families: &analysis.operation_families,
                roots: &analysis.roots,
            })?,
        },
    ])
}
