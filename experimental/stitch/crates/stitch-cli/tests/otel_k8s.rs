//! The `capture otel-k8s` → `ingest` → `render` command contract, driven
//! through the built binary with a fake kubectl script; no cluster is used.
#![cfg(unix)]

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::{Command, Output};

const FAKE_KUBECTL: &str = r#"#!/bin/sh
printf '%s\n' "$*" >> "$STITCH_FAKE_LOG"
case "$1" in
  apply) cat > "$STITCH_FAKE_LOG.apply" ;;
  exec) cat "$STITCH_FAKE_EXPORT" ;;
  get) [ "$3" = "missing" ] && { echo 'Error from server (NotFound): deployments.apps "missing" not found' >&2; exit 1; } ;;
esac
exit 0
"#;

fn stitch(dir: &Path, args: &[&str]) -> Output {
    let fixtures = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/hotel-otel");
    Command::new(env!("CARGO_BIN_EXE_stitch"))
        .args(args)
        .env("STITCH_FAKE_LOG", dir.join("kubectl.log"))
        .env("STITCH_FAKE_EXPORT", fixtures.join("otel.jsonl"))
        .output()
        .unwrap()
}

fn fake_kubectl(dir: &Path) -> String {
    let path = dir.join("kubectl");
    fs::write(&path, FAKE_KUBECTL).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
    path.display().to_string()
}

#[test]
fn start_prints_one_json_object_and_logs_the_kubectl_calls() {
    let dir = tempfile::tempdir().unwrap();
    let kubectl = fake_kubectl(dir.path());
    let output = stitch(
        dir.path(),
        &[
            "capture",
            "otel-k8s",
            "start",
            "--namespace",
            "hotel",
            "--deployment",
            "frontend",
            "--deployment",
            "search",
            "--sampler-ratio",
            "0.25",
            "--kubectl",
            &kubectl,
        ],
    );
    assert!(output.status.success(), "{output:?}");
    let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["namespace"], "hotel");
    assert_eq!(
        report["collector_service"],
        "stitch-otel-collector.hotel.svc.cluster.local"
    );
    assert_eq!(
        report["deployments"],
        serde_json::json!(["frontend", "search"])
    );
    assert_eq!(report["sampler_ratio"], 0.25);
    assert!(report["started_unix_nanos"].as_u64().unwrap() > 0);
    let log = fs::read_to_string(dir.path().join("kubectl.log")).unwrap();
    let verbs: Vec<&str> = log
        .lines()
        .map(|l| l.split(' ').take(2).collect::<Vec<_>>().join(" "))
        .map(|v| match v.as_str() {
            "get deployment" => "get",
            "apply --namespace" => "apply",
            "rollout status" => "rollout",
            "set env" => "set-env",
            _ => "other",
        })
        .collect();
    assert_eq!(
        verbs,
        ["get", "get", "apply", "rollout", "set-env", "set-env", "rollout", "rollout"]
    );
    assert!(log.contains("OTEL_TRACES_SAMPLER_ARG=0.25 "));
    let applied: serde_json::Value =
        serde_json::from_slice(&fs::read(dir.path().join("kubectl.log.apply")).unwrap()).unwrap();
    assert_eq!(applied["kind"], "List");
}

#[test]
fn start_fails_with_kubectl_stderr_and_a_non_zero_exit() {
    let dir = tempfile::tempdir().unwrap();
    let kubectl = fake_kubectl(dir.path());
    let output = stitch(
        dir.path(),
        &[
            "capture",
            "otel-k8s",
            "start",
            "--namespace",
            "hotel",
            "--deployment",
            "missing",
            "--kubectl",
            &kubectl,
        ],
    );
    assert!(!output.status.success());
    assert!(output.stdout.is_empty());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("NotFound"), "{stderr}");
}

#[test]
fn collect_ingest_and_render_produce_the_analysis_files() {
    let dir = tempfile::tempdir().unwrap();
    let kubectl = fake_kubectl(dir.path());
    let out = dir.path().join("otel");
    let out_arg = out.display().to_string();
    let output = stitch(
        dir.path(),
        &[
            "capture",
            "otel-k8s",
            "collect",
            "--namespace",
            "hotel",
            "--out",
            &out_arg,
            "--settle-seconds",
            "0",
            "--window",
            "1700000000500000000:1700000003000000000:measure",
            "--label",
            "hotel trace",
            "--kubectl",
            &kubectl,
        ],
    );
    assert!(output.status.success(), "{output:?}");
    let summary = String::from_utf8_lossy(&output.stdout);
    assert!(
        summary.starts_with("Collected 272 spans in 21 traces from 6 services"),
        "{summary}"
    );
    let graph = dir.path().join("graph.json");
    let manifest = out.join("manifest.json").display().to_string();
    let graph_arg = graph.display().to_string();
    let output = stitch(dir.path(), &["ingest", &manifest, "--out", &graph_arg]);
    assert!(output.status.success(), "{output:?}");
    let rendered = dir.path().join("render").display().to_string();
    let output = stitch(
        dir.path(),
        &[
            "render", &graph_arg, "--out", &rendered, "--window", "measure",
        ],
    );
    assert!(output.status.success(), "{output:?}");
    for name in [
        "README.txt",
        "report.txt",
        "report.json",
        "tree.txt",
        "critical-path.txt",
        "waterfall.txt",
        "graph-summary.json",
    ] {
        assert!(Path::new(&rendered).join(name).is_file(), "{name}");
    }
    let output = stitch(
        dir.path(),
        &["render", &graph_arg, "--out", &rendered, "--window", "nope"],
    );
    assert!(!output.status.success());
}
