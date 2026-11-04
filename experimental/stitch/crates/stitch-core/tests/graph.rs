use stitch_core::{
    Activity, ActivityKind, CpuSample, Graph, Interval, Observation, Origin, RelationshipKind,
};

fn origin(record: &str) -> Origin {
    Origin {
        source: "test".into(),
        artifact: "capture".into(),
        record: record.into(),
    }
}

fn activity(id: &str, kind: ActivityKind, parent_span_id: Option<&str>) -> Activity {
    Activity {
        id: id.into(),
        kind,
        name: id.into(),
        interval: Interval {
            start_ns: 10,
            end_ns: 20,
            clock: "mono".into(),
        },
        service: Some("hotel_frontend".into()),
        trace_id: Some("trace".into()),
        span_id: (id == "span").then(|| "s1".into()),
        parent_span_id: parent_span_id.map(str::to_owned),
        pid: (id == "poll").then_some(7),
        tid: (id == "poll").then_some(8),
        source_file: None,
        source_line: None,
        origin: origin(id),
    }
}

fn sample(id: &str, clock: &str) -> CpuSample {
    CpuSample {
        id: id.into(),
        pid: 7,
        tid: 8,
        at_ns: 15,
        clock: clock.into(),
        frames: vec!["handler".into()],
        origin: origin(id),
    }
}

#[test]
fn explicit_context_and_unique_same_clock_poll_support_attribution() {
    let graph = Graph::build([
        Observation::Activity(activity("span", ActivityKind::Span, None)),
        Observation::Activity(activity("poll", ActivityKind::TaskPoll, Some("s1"))),
        Observation::CpuSample(sample("cpu", "mono")),
    ])
    .unwrap();
    let summary = graph.trace("trace");
    assert_eq!(summary.assigned_samples, 1);
    assert_eq!(summary.hot_frames["handler"], 1);
    assert!(graph
        .relationships
        .iter()
        .any(|edge| edge.kind == RelationshipKind::SpanContext));
}

#[test]
fn source_annotated_cpu_region_links_unix_span_to_monotonic_sample() {
    let mut span = activity("span", ActivityKind::Span, None);
    span.interval.clock = "unix".into();
    span.interval.start_ns = 100;
    span.interval.end_ns = 200;
    let mut region = activity("region", ActivityKind::CpuRegion, Some("s1"));
    region.pid = Some(7);
    region.tid = Some(8);
    let graph = Graph::build([
        Observation::Activity(span),
        Observation::Activity(region),
        Observation::CpuSample(sample("cpu", "mono")),
    ])
    .unwrap();
    assert_eq!(graph.trace("trace").assigned_samples, 1);
    assert!(graph
        .relationships
        .iter()
        .any(|edge| edge.kind == RelationshipKind::SpanContext));
    assert!(graph
        .relationships
        .iter()
        .any(|edge| edge.kind == RelationshipKind::SampledDuring));
}

#[test]
fn unaligned_or_ambiguous_samples_remain_unassigned() {
    for ambiguous in [false, true] {
        let mut observations = vec![Observation::Activity(activity(
            "poll",
            ActivityKind::TaskPoll,
            None,
        ))];
        if ambiguous {
            observations.push(Observation::Activity(activity(
                "second_poll",
                ActivityKind::TaskPoll,
                None,
            )));
            if let Observation::Activity(second) = observations.last_mut().unwrap() {
                second.pid = Some(7);
                second.tid = Some(8);
            }
        }
        observations.push(Observation::CpuSample(sample(
            "cpu",
            if ambiguous { "mono" } else { "other" },
        )));
        let graph = Graph::build(observations).unwrap();
        assert_eq!(graph.unassigned_samples, 1);
        assert!(!graph
            .relationships
            .iter()
            .any(|edge| edge.kind == RelationshipKind::SampledDuring));
    }
}

#[test]
fn all_observation_ids_and_intervals_are_checked() {
    let duplicate = Graph::build([
        Observation::Activity(activity("poll", ActivityKind::TaskPoll, None)),
        Observation::CpuSample(sample("poll", "mono")),
    ]);
    assert!(duplicate.is_err());
    let mut invalid = activity("invalid", ActivityKind::Span, None);
    invalid.interval.end_ns = invalid.interval.start_ns;
    assert!(Graph::build([Observation::Activity(invalid)]).is_err());
}

#[test]
fn cpu_stack_tree_counts_self_and_inclusive_samples() {
    let mut first = sample("first", "mono");
    first.frames = vec!["handler".into(), "runtime".into()];
    let mut second = sample("second", "mono");
    second.frames = vec!["scheduler".into(), "runtime".into()];
    let mut third = sample("third", "mono");
    third.frames = vec!["handler".into(), "runtime".into()];
    let graph = Graph::build([
        Observation::CpuSample(first),
        Observation::CpuSample(second),
        Observation::CpuSample(third),
    ])
    .unwrap();
    let tree = graph.sample_cost_tree();
    assert_eq!(tree.samples, 3);
    assert_eq!(tree.roots.len(), 1);
    assert_eq!(tree.roots[0].frame, "runtime");
    assert_eq!(tree.roots[0].inclusive_samples, 3);
    assert_eq!(tree.roots[0].self_samples, 0);
    assert_eq!(tree.roots[0].children[0].frame, "handler");
    assert_eq!(tree.roots[0].children[0].inclusive_samples, 2);
    assert_eq!(tree.roots[0].children[0].self_samples, 2);
}
