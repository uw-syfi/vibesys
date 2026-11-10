use std::fs;
use stitch_core::Graph;
use stitch_importer_api::ImporterRegistry;
use stitch_profiler_dial9::Dial9BridgeImporter;
use stitch_session::{ingest_with, load_manifest, RunManifest, SessionError};
use stitch_source_api::MappingRegistry;
use stitch_source_workspace::WorkspaceLocationResolver;

fn ingest(manifest: &RunManifest) -> Result<Graph, SessionError> {
    let mut importers = ImporterRegistry::default();
    importers.register(Dial9BridgeImporter)?;
    let mut mappers = MappingRegistry::default();
    if let Some(root) = &manifest.source_root {
        mappers.register(WorkspaceLocationResolver::new(
            root,
            manifest.source_revision.clone(),
        )?)?;
    }
    ingest_with(manifest, &importers, &mappers)
}

#[test]
fn source_root_is_resolved_relative_to_the_manifest() {
    let dir = tempfile::tempdir().unwrap();
    fs::create_dir(dir.path().join("src")).unwrap();
    fs::write(dir.path().join("src/main.rs"), "fn main() {}\n").unwrap();
    fs::write(
        dir.path().join("dial9.jsonl"),
        r#"{"task_id":"t","poll_id":"p","pid":1,"tid":2,"start_ns":10,"end_ns":20,"name":"work","source_file":"src/main.rs","source_line":1,"trace_id":"trace","parent_span_id":"span"}"#,
    )
    .unwrap();
    let manifest = serde_json::json!({
        "schema_version": 1,
        "label": "hotel",
        "source_root": ".",
        "source_revision": "abc123",
        "inputs": [{"format": "dial9_bridge_jsonl", "path": "dial9.jsonl", "clock": "mono"}]
    });
    let path = dir.path().join("manifest.json");
    fs::write(&path, serde_json::to_vec(&manifest).unwrap()).unwrap();
    let graph = ingest(&load_manifest(&path).unwrap()).unwrap();
    assert_eq!(graph.source_attributions.len(), 1);
    assert_eq!(graph.source_attributions[0].location.path, "src/main.rs");
}

fn write_manifest(dir: &std::path::Path, windows: &serde_json::Value) -> std::path::PathBuf {
    fs::write(
        dir.join("dial9.jsonl"),
        r#"{"task_id":"t","poll_id":"p","pid":1,"tid":2,"start_ns":10,"end_ns":20,"name":"work"}"#,
    )
    .unwrap();
    let manifest = serde_json::json!({
        "schema_version": 1,
        "label": "hotel",
        "inputs": [{"format": "dial9_bridge_jsonl", "path": "dial9.jsonl", "clock": "mono"}],
        "windows": windows,
    });
    let path = dir.join("manifest.json");
    fs::write(&path, serde_json::to_vec(&manifest).unwrap()).unwrap();
    path
}

#[test]
fn manifest_windows_are_carried_into_the_graph() {
    let dir = tempfile::tempdir().unwrap();
    let path = write_manifest(
        dir.path(),
        &serde_json::json!([
            {"start_unix_nanos": 100, "end_unix_nanos": 200, "label": "warm"},
            {"start_unix_nanos": 300, "end_unix_nanos": 400}
        ]),
    );
    let graph = ingest(&load_manifest(&path).unwrap()).unwrap();
    assert_eq!(graph.windows.len(), 2);
    assert_eq!(graph.windows[0].label.as_deref(), Some("warm"));
    assert_eq!(graph.windows[1].end_unix_nanos, 400);
}

#[test]
fn invalid_or_unknown_window_fields_are_rejected() {
    for windows in [
        serde_json::json!([{"start_unix_nanos": 200, "end_unix_nanos": 200}]),
        serde_json::json!([{"start_unix_nanos": 1, "end_unix_nanos": 2, "extra": 1}]),
        serde_json::json!([
            {"start_unix_nanos": 1, "end_unix_nanos": 2, "label": "a"},
            {"start_unix_nanos": 3, "end_unix_nanos": 4, "label": "a"}
        ]),
        serde_json::json!([{"start_unix_nanos": 1, "end_unix_nanos": 2, "label": "all"}]),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let path = write_manifest(dir.path(), &windows);
        assert!(load_manifest(&path).is_err(), "{windows}");
    }
}
