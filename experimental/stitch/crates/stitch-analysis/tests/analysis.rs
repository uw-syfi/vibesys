use stitch_analysis::{analyze, Analysis, RootGraph, WindowSelection};
use stitch_core::{
    Activity, ActivityKind, Graph, Interval, Observation, Origin, SpanDetail, SpanKind, SpanLink,
    TimeWindow,
};

const MS: u64 = 1_000_000;
const T0: u64 = 1_700_000_000_000_000_000;

struct S {
    trace: &'static str,
    id: &'static str,
    parent: &'static str,
    service: &'static str,
    operation: &'static str,
    kind: SpanKind,
    start_ms: u64,
    duration_ms: u64,
}

fn s(
    service: &'static str,
    id: &'static str,
    parent: &'static str,
    operation: &'static str,
    kind: SpanKind,
    start_ms: u64,
    duration_ms: u64,
) -> S {
    S {
        trace: "trace-a",
        id,
        parent,
        service,
        operation,
        kind,
        start_ms,
        duration_ms,
    }
}

fn graph(spans: &[S], windows: Vec<TimeWindow>, links: &[(&str, SpanLink)]) -> Graph {
    let observations = spans.iter().map(|span| {
        Observation::Activity(Activity {
            id: format!("{}:{}", span.trace, span.id),
            kind: ActivityKind::Span,
            name: span.operation.into(),
            interval: Interval {
                start_ns: T0 + span.start_ms * MS,
                end_ns: T0 + (span.start_ms + span.duration_ms) * MS,
                clock: "wall".into(),
            },
            service: Some(span.service.into()),
            trace_id: Some(span.trace.into()),
            span_id: Some(span.id.into()),
            parent_span_id: (!span.parent.is_empty()).then(|| span.parent.into()),
            pid: None,
            tid: None,
            source_file: None,
            source_line: None,
            origin: Origin {
                source: "test".into(),
                artifact: "test".into(),
                record: span.id.into(),
            },
            span: Some(Box::new(SpanDetail {
                kind: span.kind,
                operation: span.operation.into(),
                links: links
                    .iter()
                    .filter(|(id, _)| *id == span.id)
                    .map(|(_, link)| link.clone())
                    .collect(),
                ..SpanDetail::default()
            })),
        })
    });
    let mut graph = Graph::build(observations).unwrap();
    graph.windows = windows;
    graph
}

fn run(spans: &[S]) -> Analysis {
    analyze(&graph(spans, Vec::new(), &[]), &WindowSelection::All).unwrap()
}

fn contribution(root: &RootGraph, service: &str, operation: &str) -> f64 {
    root.critical_path
        .nodes
        .iter()
        .find(|n| n.service == service && n.operation == operation)
        .map_or(0.0, |n| n.contribution.mean_ms)
}

fn assert_contributions(root: &RootGraph, expected: &[(&str, &str, f64)]) {
    for (service, operation, ms) in expected {
        let got = contribution(root, service, operation);
        assert!(
            (got - ms).abs() < 1e-9,
            "{service}: {operation} = {got}, want {ms}"
        );
    }
    let segments = &root.critical_path.representative_segments;
    let mut cursor = 0.0;
    for segment in segments {
        assert!((segment.offset_ms - cursor).abs() < 1e-9);
        cursor += segment.duration_ms;
    }
    assert!(
        (cursor - root.latency.max_ms).abs() < 1e-9,
        "segments cover the root"
    );
}

use SpanKind::{Client, Consumer, Internal, Server};

#[test]
fn parallel_children_attribute_overlap_to_the_child_ending_last() {
    let analysis = run(&[
        s("frontend", "root", "", "root", Server, 100, 100),
        s("frontend", "early", "root", "early", Internal, 110, 60),
        s("frontend", "late", "root", "late", Internal, 130, 60),
    ]);
    assert_contributions(
        &analysis.roots[0],
        &[
            ("frontend", "root", 20.0),
            ("frontend", "early", 20.0),
            ("frontend", "late", 60.0),
        ],
    );
}

#[test]
fn chained_staggered_overlap_follows_the_active_leaf() {
    let analysis = run(&[
        s("frontend", "root", "", "root", Server, 100, 100),
        s("frontend", "a", "root", "a", Internal, 110, 70),
        s("frontend", "b", "root", "b", Internal, 120, 70),
        s("frontend", "c", "root", "c", Internal, 170, 30),
    ]);
    assert_contributions(
        &analysis.roots[0],
        &[
            ("frontend", "root", 10.0),
            ("frontend", "a", 10.0),
            ("frontend", "b", 50.0),
            ("frontend", "c", 30.0),
        ],
    );
}

#[test]
fn nested_spans_and_gaps_give_parents_their_self_time() {
    let analysis = run(&[
        s("frontend", "root", "", "root", Server, 0, 100),
        s("frontend", "outer", "root", "outer", Internal, 10, 50),
        s("frontend", "inner", "outer", "inner", Internal, 20, 10),
        s("frontend", "tail", "root", "tail", Internal, 80, 10),
    ]);
    let root = &analysis.roots[0];
    assert_contributions(
        root,
        &[
            ("frontend", "root", 40.0),
            ("frontend", "outer", 40.0),
            ("frontend", "inner", 10.0),
            ("frontend", "tail", 10.0),
        ],
    );
    let outer = root.nodes.iter().find(|n| n.operation == "outer").unwrap();
    assert!((outer.exclusive.mean_ms - 40.0).abs() < 1e-9);
    assert!((outer.inclusive.mean_ms - 50.0).abs() < 1e-9);
}

#[test]
fn a_remote_call_is_one_hop_that_owns_the_client_interval() {
    let analysis = run(&[
        s("frontend", "root", "", "root", Server, 100, 100),
        s(
            "frontend",
            "client",
            "root",
            "Search/Nearby",
            Client,
            110,
            60,
        ),
        s(
            "search",
            "server",
            "client",
            "Search/Nearby",
            Server,
            112,
            55,
        ),
        s("search", "db", "server", "mongo.find", Client, 120, 20),
    ]);
    let root = &analysis.roots[0];
    assert_contributions(
        root,
        &[
            ("frontend", "root", 40.0),
            ("search", "Search/Nearby", 40.0),
            ("search", "mongo.find", 20.0),
        ],
    );
    assert_eq!(analysis.quality.matched_client_server_pairs, 1);
    let paths: Vec<&str> = root.nodes.iter().map(|n| n.path.as_str()).collect();
    assert_eq!(
        paths,
        [
            "frontend:root",
            "frontend:root > search:Search/Nearby",
            "frontend:root > search:Search/Nearby > search:mongo.find",
        ]
    );
    let hop = root.nodes[1].hop.as_ref().unwrap();
    assert_eq!(hop.client_service, "frontend");
    assert!((hop.client.mean_ms - 60.0).abs() < 1e-9);
    assert!((hop.network.mean_ms - 5.0).abs() < 1e-9);
    assert!((root.nodes[1].inclusive.mean_ms - 55.0).abs() < 1e-9);
    assert_eq!(root.nodes[1].parent_id.as_deref(), Some("node-001"));
}

#[test]
fn ambiguous_or_same_service_clients_stay_unpaired() {
    let analysis = run(&[
        s("frontend", "root", "", "root", Server, 0, 100),
        s("frontend", "fanout", "root", "call", Client, 10, 50),
        s("a", "server-a", "fanout", "call", Server, 11, 40),
        s("b", "server-b", "fanout", "call", Server, 12, 40),
        s("frontend", "local", "root", "self", Client, 70, 20),
        s("frontend", "local-server", "local", "self", Server, 71, 10),
    ]);
    assert_eq!(analysis.quality.matched_client_server_pairs, 0);
    assert_eq!(analysis.quality.unmatched_client_spans, 2);
    assert_eq!(analysis.quality.unmatched_server_spans, 3);
    assert!(analysis.roots[0].nodes.iter().all(|n| n.hop.is_none()));
}

#[test]
fn async_consumers_and_links_are_excluded_from_the_critical_path() {
    let spans = [
        s("frontend", "root", "", "root", Server, 100, 100),
        s("worker", "consumer", "root", "consume", Consumer, 120, 70),
    ];
    let link = SpanLink {
        trace_id: "trace-b".into(),
        span_id: "producer".into(),
    };
    let analysis = analyze(
        &graph(&spans, Vec::new(), &[("root", link)]),
        &WindowSelection::All,
    )
    .unwrap();
    let root = &analysis.roots[0];
    assert_eq!(root.critical_path.async_relationships_excluded, 2);
    assert_contributions(root, &[("frontend", "root", 100.0)]);
    assert!((contribution(root, "worker", "consume")).abs() < 1e-9);
}

fn window(start_ms: u64, end_ms: u64, label: Option<&str>) -> TimeWindow {
    TimeWindow {
        start_unix_nanos: T0 + start_ms * MS,
        end_unix_nanos: T0 + end_ms * MS,
        label: label.map(str::to_owned),
    }
}

#[test]
fn windows_select_traces_and_name_exclusions() {
    let mut spans = vec![
        s("frontend", "root", "", "root", Server, 100, 100),
        s("frontend", "child", "root", "child", Internal, 110, 10),
    ];
    // Starts inside the first window, ends after it.
    let mut straddling = s("frontend", "late", "", "root", Server, 950, 100);
    straddling.trace = "trace-b";
    spans.push(straddling);
    let mut second = s("frontend", "other", "", "root", Server, 2100, 50);
    second.trace = "trace-c";
    spans.push(second);
    let mut orphan = s("search", "orphan", "gone", "x", Server, 2200, 10);
    orphan.trace = "trace-d";
    spans.push(orphan);
    let windows = vec![window(0, 1000, Some("warm")), window(2000, 3000, None)];
    let graph = graph(&spans, windows, &[]);

    let all = analyze(&graph, &WindowSelection::All).unwrap();
    assert_eq!(all.quality.captured_traces, 4);
    assert_eq!(all.quality.eligible_traces, 2);
    assert_eq!(
        all.quality
            .exclusion_reasons
            .get("outside_measurement_window"),
        Some(&1)
    );
    assert_eq!(
        all.quality.exclusion_reasons.get("missing_parent"),
        Some(&1)
    );
    assert_eq!(all.quality.windows[0].label, "warm");
    assert_eq!(all.quality.windows[1].label, "window-2");
    assert_eq!(
        (
            all.quality.windows[1].eligible_traces,
            all.quality.windows[1].excluded_traces
        ),
        (1, 1)
    );
    // The latency tables count only spans wholly inside a window.
    assert_eq!(all.report.span_count, 4);

    let second = analyze(&graph, &WindowSelection::Label("window-2".into())).unwrap();
    assert_eq!(second.quality.captured_traces, 2);
    assert_eq!(second.roots[0].trace_count, 1);
    assert_eq!(second.windows.len(), 1);

    let empty = window(5000, 6000, Some("idle"));
    let idle = graph_with_windows(&graph, vec![empty]);
    assert!(analyze(&idle, &WindowSelection::All).is_err());
}

fn graph_with_windows(graph: &Graph, windows: Vec<TimeWindow>) -> Graph {
    let mut copy = graph.clone();
    copy.windows = windows;
    copy
}

#[test]
fn clock_inconsistent_and_duplicate_traces_are_excluded() {
    let mut spans = vec![
        s("frontend", "root", "", "root", Server, 100, 100),
        s("frontend", "child", "root", "child", Internal, 150, 100),
    ];
    let mut second = s("frontend", "root", "", "root", Server, 100, 100);
    second.trace = "trace-b";
    spans.push(second);
    let mut graph = graph(&spans, Vec::new(), &[]);
    // The importer keeps a repeated span identity under a distinct ID.
    let mut duplicate = graph.activities.last().unwrap().clone();
    duplicate.id.push_str("#2");
    graph.activities.push(duplicate);
    let analysis = analyze(&graph, &WindowSelection::All).unwrap();
    assert_eq!(analysis.quality.eligible_traces, 0);
    assert_eq!(
        analysis
            .quality
            .exclusion_reasons
            .get("clock_inconsistency"),
        Some(&1)
    );
    assert_eq!(
        analysis.quality.exclusion_reasons.get("duplicate_span_id"),
        Some(&1)
    );
    assert!(analysis.roots.is_empty());
}

/// One trace per request whose datastore span name embeds the request's data.
fn family_spans(names: &[String], repeats: usize) -> Vec<S> {
    let names: Vec<&'static str> = names
        .iter()
        .map(|n| &*Box::leak(n.clone().into_boxed_str()))
        .collect();
    let mut spans = Vec::new();
    for repeat in 0..repeats {
        for (index, name) in names.iter().enumerate() {
            let trace: &'static str = Box::leak(format!("trace-{repeat}-{index}").into_boxed_str());
            let start = u64::try_from(index * 10).unwrap();
            for (id, parent, operation, kind, offset, duration) in [
                ("root", "", "GET /hotels", Server, 0, 10),
                (
                    "stable",
                    "root",
                    "mongodb_capacity_get_multi_number",
                    Client,
                    1,
                    2,
                ),
                ("data", "root", *name, Client, 4, 5),
            ] {
                let mut span = s(
                    if id == "root" {
                        "frontend"
                    } else {
                        "reservation"
                    },
                    id,
                    parent,
                    operation,
                    kind,
                    start + offset,
                    duration,
                );
                span.trace = trace;
                spans.push(span);
            }
        }
    }
    spans
}

#[test]
fn operation_names_with_request_data_are_aggregated_as_one_family() {
    let names: Vec<String> = (0..60)
        .map(|i| {
            if i % 2 == 0 {
                format!(
                    "mongodb_capacity_get_multi_number{}_2028-09-{:02}",
                    680_000 + i,
                    i % 28 + 1
                )
            } else {
                format!("mongodb_capacity_get_multi_numbera99266{i}-3_2028-02-10")
            }
        })
        .collect();
    let analysis = run(&family_spans(&names, 1));
    assert_eq!(analysis.operation_families.len(), 1);
    let family = &analysis.operation_families[0];
    assert_eq!(family.service, "reservation");
    assert_eq!(family.pattern, "mongodb_capacity_get_multi_number*");
    assert_eq!((family.distinct_names, family.spans), (60, 60));
    assert_eq!(family.examples.len(), 3);
    let root = &analysis.roots[0];
    let operations: Vec<&str> = root.nodes.iter().map(|n| n.operation.as_str()).collect();
    assert_eq!(
        operations,
        [
            "GET /hotels",
            "mongodb_capacity_get_multi_number",
            "mongodb_capacity_get_multi_number*",
        ]
    );
    assert_contributions(
        root,
        &[("reservation", "mongodb_capacity_get_multi_number*", 5.0)],
    );
    let rows: Vec<&str> = analysis
        .report
        .spans
        .iter()
        .map(|r| r.name.as_str())
        .collect();
    assert!(rows.contains(&"reservation: mongodb_capacity_get_multi_number*"));
    assert_eq!(rows.len(), 3);
    let waterfall = &root.representatives[0].spans;
    let data = waterfall
        .iter()
        .find(|s| s.operation.ends_with('*'))
        .unwrap();
    assert!(names.contains(data.raw_operation.as_ref().unwrap()));

    let files =
        stitch_analysis::render(&analysis, &stitch_analysis::RenderOptions::default()).unwrap();
    let warning = "WARNING: 60 span names of service reservation look like one operation with \
                   request data in the name (e.g. ";
    for name in ["README.txt", "report.txt"] {
        let text = &files.iter().find(|f| f.name == name).unwrap().contents;
        let top: String = text.lines().take(3).collect::<Vec<_>>().join("\n");
        assert!(top.contains(warning), "{name}: {top}");
        assert!(top.contains(
            "aggregated as mongodb_capacity_get_multi_number*. Fix the instrumentation: put the \
             data in an attribute."
        ));
    }
    let summary = &files
        .iter()
        .find(|f| f.name == "graph-summary.json")
        .unwrap()
        .contents;
    assert!(summary.contains("\"operation_families\""));
}

#[test]
fn repeated_short_or_few_names_are_not_aggregated() {
    // Names that repeat across many requests are real operations.
    let repeated: Vec<String> = (0..60).map(|i| format!("mongodb_find_shard{i}")).collect();
    assert!(run(&family_spans(&repeated, 11))
        .operation_families
        .is_empty());
    // Too few distinct names.
    let few: Vec<String> = (0..49).map(|i| format!("mongodb_find_shard{i}")).collect();
    assert!(run(&family_spans(&few, 1)).operation_families.is_empty());
    // The shared prefix is too short to name an operation.
    let short: Vec<String> = (0..60).map(|i| format!("get_u{i}")).collect();
    assert!(run(&family_spans(&short, 1)).operation_families.is_empty());
}
