use std::fs;
use stitch_core::{Graph, Observation, SpanKind};
use stitch_importer_api::{Importer, InputSpec};
use stitch_profiler_otel::OtlpJsonlImporter;

fn span(
    span_id: &str,
    parent: &str,
    kind: &serde_json::Value,
    attrs: &serde_json::Value,
) -> serde_json::Value {
    serde_json::json!({
        "traceId": "t1", "spanId": span_id, "parentSpanId": parent, "name": "GET /hotels?inDate=1",
        "kind": kind, "startTimeUnixNano": "100", "endTimeUnixNano": 100,
        "attributes": attrs, "status": {"code": "STATUS_CODE_OK"},
    })
}

fn import(lines: &[serde_json::Value]) -> Vec<stitch_core::Activity> {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("otel.jsonl");
    let text: Vec<String> = lines.iter().map(ToString::to_string).collect();
    fs::write(&path, text.join("\n") + "\n").unwrap();
    OtlpJsonlImporter
        .import(&InputSpec {
            format: "otlp_jsonl".into(),
            path,
            clock: "wall".into(),
        })
        .unwrap()
        .into_iter()
        .map(|o| match o {
            Observation::Activity(a) => a,
            Observation::CpuSample(_) => panic!("unexpected sample"),
        })
        .collect()
}

#[test]
fn spans_carry_kind_operation_status_and_datastore() {
    let attrs = serde_json::json!([
        {"key": "db.system", "value": {"stringValue": "memcached"}},
        {"key": "db.operation", "value": {"stringValue": "get"}},
        {"key": "error", "value": {"boolValue": true}},
    ]);
    let activities = import(&[serde_json::json!({"resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "rate"}}]},
        "instrumentationLibrarySpans": [{"spans": [span("s1", "", &serde_json::json!(3), &attrs)]}],
    }]})]);
    let detail = activities[0].span.as_deref().unwrap();
    assert_eq!(activities[0].service.as_deref(), Some("rate"));
    assert_eq!(detail.kind, SpanKind::Client);
    assert_eq!(detail.operation, "GET /hotels");
    assert!(detail.failed);
    assert_eq!(detail.db_system.as_deref(), Some("memcached"));
    assert_eq!(detail.db_operation.as_deref(), Some("get"));
    // A zero-length span is an instant and builds into a graph.
    assert!(Graph::build(activities.into_iter().map(Observation::Activity)).is_ok());
}

#[test]
fn repeated_span_identities_keep_distinct_observation_ids() {
    let document = serde_json::json!({"resourceSpans": [{"scopeSpans": [{"spans": [
        span("s1", "", &serde_json::json!("SPAN_KIND_SERVER"), &serde_json::json!([])),
    ]}]}]});
    let activities = import(&[document.clone(), document]);
    assert_eq!(activities.len(), 2);
    assert_ne!(activities[0].id, activities[1].id);
    assert_eq!(activities[0].span_id, activities[1].span_id);
    assert!(activities[0].service.is_none());
    assert!(Graph::build(activities.into_iter().map(Observation::Activity)).is_ok());
}

#[test]
fn reversed_timestamps_are_rejected_with_their_line() {
    let mut bad = span("s1", "", &serde_json::json!(2), &serde_json::json!([]));
    bad["startTimeUnixNano"] = "200".into();
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("otel.jsonl");
    fs::write(
        &path,
        serde_json::json!({"resourceSpans": [{"scopeSpans": [{"spans": [bad]}]}]}).to_string(),
    )
    .unwrap();
    let error = OtlpJsonlImporter
        .import(&InputSpec {
            format: "otlp_jsonl".into(),
            path,
            clock: "wall".into(),
        })
        .unwrap_err();
    assert!(error.to_string().contains("span 1:0/0/0"), "{error}");
}
