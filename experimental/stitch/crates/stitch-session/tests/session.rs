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
