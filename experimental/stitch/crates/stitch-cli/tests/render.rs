//! Trace analysis of a hotelReservation-shaped OTLP fixture: parity with the
//! evaluator's Go implementation, and golden text renderings.
//!
//! `fixtures/hotel-otel/go-trace-graph.json` holds `BuildTraceGraph` output
//! from `sdk/vibesys-evaluator/go/internal/telemetry` for the same fixture and
//! window. Regenerate it with a throwaway Go test in that package after
//! changing either implementation, and the golden files with
//! `STITCH_UPDATE_GOLDEN=1 cargo test -p stitch-cli --test render`.

use serde_json::Value;
use std::path::{Path, PathBuf};
use stitch_analysis::{analyze, render, Analysis, Distribution, RenderOptions, WindowSelection};
use stitch_core::Graph;
use stitch_importer_api::ImporterRegistry;
use stitch_profiler_otel::OtlpJsonlImporter;
use stitch_session::{ingest_with, load_manifest};
use stitch_source_api::MappingRegistry;

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/hotel-otel")
}

fn graph() -> Graph {
    let manifest = load_manifest(&fixtures().join("manifest.json")).unwrap();
    let mut importers = ImporterRegistry::default();
    importers.register(OtlpJsonlImporter).unwrap();
    ingest_with(&manifest, &importers, &MappingRegistry::default()).unwrap()
}

fn analysis() -> Analysis {
    analyze(&graph(), &WindowSelection::All).unwrap()
}

fn assert_close(context: &str, got: f64, want: &Value) {
    let want = want.as_f64().unwrap_or_else(|| panic!("{context}: {want}"));
    assert!(
        (got - want).abs() <= 1e-9,
        "{context}: got {got}, want {want}"
    );
}

fn assert_distribution(context: &str, got: &Distribution, want: &Value) {
    assert_eq!(
        got.count as u64,
        want["count"].as_u64().unwrap(),
        "{context} count"
    );
    assert_eq!(
        got.error_count as u64,
        want["error_count"].as_u64().unwrap(),
        "{context} error_count"
    );
    for (name, value) in [
        ("mean_ms", got.mean_ms),
        ("p50_ms", got.p50_ms),
        ("p95_ms", got.p95_ms),
        ("p99_ms", got.p99_ms),
        ("max_ms", got.max_ms),
    ] {
        assert_close(&format!("{context} {name}"), value, &want[name]);
    }
}

#[test]
fn trace_graph_matches_the_go_implementation() {
    let go: Value =
        serde_json::from_slice(&std::fs::read(fixtures().join("go-trace-graph.json")).unwrap())
            .unwrap();
    let analysis = analysis();
    let quality = serde_json::to_value(&analysis.quality).unwrap();
    for key in [
        "captured_traces",
        "eligible_traces",
        "excluded_traces",
        "exclusion_reasons",
        "matched_client_server_pairs",
        "unmatched_client_spans",
        "unmatched_server_spans",
        "async_relationships",
    ] {
        assert_eq!(quality[key], go["quality"][key], "quality {key}");
    }
    let go_roots = go["roots"].as_array().unwrap();
    assert_eq!(analysis.roots.len(), go_roots.len());
    for want in go_roots {
        let root = analysis
            .roots
            .iter()
            .find(|r| r.service == want["service"] && r.operation == want["operation"])
            .unwrap_or_else(|| panic!("missing root {want}"));
        let context = format!("{}: {}", root.service, root.operation);
        assert_eq!(
            root.trace_count as u64,
            want["trace_count"].as_u64().unwrap()
        );
        assert_distribution(&context, &root.latency, &want["latency_ms"]);
        let want_nodes = want["nodes"].as_array().unwrap();
        assert_eq!(root.nodes.len(), want_nodes.len(), "{context} nodes");
        for (node, want) in root.nodes.iter().zip(want_nodes) {
            assert_eq!(node.id, want["id"].as_str().unwrap());
            assert_eq!(node.path, want["path"].as_str().unwrap());
            assert_distribution(&node.path, &node.inclusive, &want["inclusive_latency_ms"]);
            assert_distribution(&node.path, &node.exclusive, &want["exclusive_latency_ms"]);
        }
        let critical = &root.critical_path;
        let want = &want["critical_path"];
        assert_eq!(critical.algorithm, want["algorithm"].as_str().unwrap());
        assert_eq!(critical.scope, want["scope"].as_str().unwrap());
        assert_eq!(
            critical.async_relationships_excluded as u64,
            want["async_relationships_excluded"].as_u64().unwrap()
        );
        let want_nodes = want["nodes_by_contribution"].as_array().unwrap();
        assert_eq!(critical.nodes.len(), want_nodes.len(), "{context} critical");
        for (node, want) in critical.nodes.iter().zip(want_nodes) {
            assert_eq!(node.path, want["path"].as_str().unwrap(), "{context} order");
            assert_eq!(node.node_id, want["node_id"].as_str().unwrap());
            assert_distribution(&node.path, &node.contribution, &want["contribution_ms"]);
        }
        let representative = &want["representative"];
        assert_eq!(
            critical.representative_trace_id,
            representative["trace_id"].as_str().unwrap()
        );
        assert_close(
            "representative duration",
            critical.representative_duration_ms,
            &representative["duration_ms"],
        );
        let segments = representative["segments"].as_array().unwrap();
        assert_eq!(critical.representative_segments.len(), segments.len());
        for (segment, want) in critical.representative_segments.iter().zip(segments) {
            assert_eq!(segment.node_id, want["node_id"].as_str().unwrap());
            assert_close("segment offset", segment.offset_ms, &want["offset_ms"]);
            assert_close(
                "segment duration",
                segment.duration_ms,
                &want["duration_ms"],
            );
        }
        assert_eq!(
            root.representatives[1].trace_id,
            critical.representative_trace_id
        );
    }
}

#[test]
fn rendered_files_match_golden_copies() {
    let files = render(
        &analysis(),
        &RenderOptions {
            max_nodes: 12,
            ..RenderOptions::default()
        },
    )
    .unwrap();
    let golden = fixtures().join("render");
    let update = std::env::var_os("STITCH_UPDATE_GOLDEN").is_some();
    if update {
        std::fs::create_dir_all(&golden).unwrap();
    }
    for file in files {
        let path = golden.join(file.name);
        if update {
            std::fs::write(&path, &file.contents).unwrap();
            continue;
        }
        let want = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("{}: {e}; set STITCH_UPDATE_GOLDEN=1", path.display()));
        assert!(
            file.contents == want,
            "{} differs from its golden copy; set STITCH_UPDATE_GOLDEN=1 to update\n{}",
            file.name,
            file.contents
        );
    }
}

#[test]
fn a_named_window_selects_its_spans_and_unknown_names_fail() {
    let graph = graph();
    let measured = analyze(&graph, &WindowSelection::Label("measure".into())).unwrap();
    assert_eq!(measured.quality.eligible_traces, 17);
    let error = analyze(&graph, &WindowSelection::Label("warmup".into())).unwrap_err();
    assert!(error.to_string().contains("available: measure"), "{error}");
}

#[test]
fn render_bounds_are_validated() {
    let analysis = analysis();
    for options in [
        RenderOptions {
            top: 0,
            ..RenderOptions::default()
        },
        RenderOptions {
            width: 5,
            ..RenderOptions::default()
        },
    ] {
        assert!(render(&analysis, &options).is_err());
    }
}
