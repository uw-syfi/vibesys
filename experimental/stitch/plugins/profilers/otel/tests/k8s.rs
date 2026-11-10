use std::cell::RefCell;
use std::fs;
use std::time::Duration;
use stitch_core::TimeWindow;
use stitch_profiler_otel::k8s::{
    collect, collector_manifest, start, CollectRequest, K8sCaptureError, Kubectl, StartRequest,
    COLLECTOR_CONFIG, DEFAULT_COLLECTOR_IMAGE, DEFAULT_SIDECAR_IMAGE,
};
use stitch_session::load_manifest;

/// One line exactly as the 0.159.0 collector's file exporter wrote it.
const COLLECTOR_LINE: &str = r#"{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"frontend"}}]},"scopeSpans":[{"scope":{},"spans":[{"traceId":"5b8efff798038103d269b633813fc60c","spanId":"eee19b7ec3c1b174","name":"GET /hotels","kind":2,"startTimeUnixNano":"1700000000000000000","endTimeUnixNano":"1700000000100000000","attributes":[{"key":"db.system","value":{"stringValue":"mongodb"}}],"status":{"code":2}}]}]}]}"#;

#[derive(Default)]
struct FakeKubectl {
    calls: RefCell<Vec<Vec<String>>>,
    stdin: RefCell<Vec<String>>,
    pauses: RefCell<Vec<Duration>>,
    export: Vec<u8>,
    elapsed: Duration,
    fail_on: Option<&'static str>,
}

impl Kubectl for FakeKubectl {
    fn run(&self, args: &[String], stdin: Option<&[u8]>) -> Result<Vec<u8>, K8sCaptureError> {
        self.calls.borrow_mut().push(args.to_vec());
        if let Some(input) = stdin {
            self.stdin
                .borrow_mut()
                .push(String::from_utf8(input.to_vec()).unwrap());
        }
        if self
            .fail_on
            .is_some_and(|word| args.iter().any(|a| a == word))
        {
            return Err(K8sCaptureError::Kubectl {
                args: args.join(" "),
                detail: "NotFound".into(),
            });
        }
        Ok(if args[0] == "exec" {
            self.export.clone()
        } else {
            Vec::new()
        })
    }

    fn elapsed(&self) -> Duration {
        self.elapsed
    }

    fn unix_nanos(&self) -> u64 {
        1_700_000_000_123_456_789
    }

    fn pause(&self, duration: Duration) {
        self.pauses.borrow_mut().push(duration);
    }
}

fn request() -> StartRequest {
    StartRequest {
        namespace: "hotel".into(),
        deployments: vec!["frontend".into(), "search".into()],
        sampler_ratio: 0.5,
        collector_image: DEFAULT_COLLECTOR_IMAGE.into(),
        sidecar_image: DEFAULT_SIDECAR_IMAGE.into(),
        timeout: Duration::from_secs(600),
    }
}

fn strs(values: &[&str]) -> Vec<String> {
    values.iter().map(|v| (*v).to_owned()).collect()
}

#[test]
fn start_applies_the_collector_then_instruments_only_named_deployments() {
    let kubectl = FakeKubectl {
        elapsed: Duration::from_millis(30_500),
        ..FakeKubectl::default()
    };
    let report = start(&request(), &kubectl).unwrap();
    let env = [
        "OTEL_EXPORTER_OTLP_ENDPOINT=http://stitch-otel-collector.hotel.svc.cluster.local:4317",
        "OTEL_EXPORTER_OTLP_PROTOCOL=grpc",
        "OTEL_TRACES_SAMPLER=parentbased_traceidratio",
        "OTEL_TRACES_SAMPLER_ARG=0.5",
        "JAEGER_AGENT_HOST=stitch-otel-collector.hotel.svc.cluster.local",
        "JAEGER_AGENT_PORT=6831",
        "JAEGER_SAMPLER_TYPE=probabilistic",
        "JAEGER_SAMPLER_PARAM=0.5",
    ];
    let set_env = |name: &str| {
        let mut command = strs(&[
            "set",
            "env",
            &format!("deployment/{name}"),
            "--namespace",
            "hotel",
            "--containers=*",
        ]);
        command.extend(strs(&env));
        command
    };
    let rollout = |name: &str| {
        strs(&[
            "rollout",
            "status",
            &format!("deployment/{name}"),
            "--namespace",
            "hotel",
            "--watch=true",
            "--timeout=570s",
        ])
    };
    let expected = vec![
        strs(&[
            "get",
            "deployment",
            "frontend",
            "--namespace",
            "hotel",
            "--output=name",
        ]),
        strs(&[
            "get",
            "deployment",
            "search",
            "--namespace",
            "hotel",
            "--output=name",
        ]),
        strs(&["apply", "--namespace", "hotel", "--filename=-"]),
        rollout("stitch-otel-collector"),
        set_env("frontend"),
        set_env("search"),
        rollout("frontend"),
        rollout("search"),
    ];
    assert_eq!(*kubectl.calls.borrow(), expected);
    assert_eq!(
        kubectl.stdin.borrow().as_slice(),
        [collector_manifest(
            "hotel",
            DEFAULT_COLLECTOR_IMAGE,
            DEFAULT_SIDECAR_IMAGE
        )]
    );
    assert_eq!(
        serde_json::to_value(&report).unwrap(),
        serde_json::json!({
            "namespace": "hotel",
            "collector_service": "stitch-otel-collector.hotel.svc.cluster.local",
            "deployments": ["frontend", "search"],
            "sampler_ratio": 0.5,
            "started_unix_nanos": 1_700_000_000_123_456_789_u64,
        })
    );
}

#[test]
fn start_checks_every_deployment_before_changing_the_cluster() {
    let kubectl = FakeKubectl {
        fail_on: Some("search"),
        ..FakeKubectl::default()
    };
    let error = start(&request(), &kubectl).unwrap_err();
    assert!(error.to_string().contains("NotFound"), "{error}");
    assert!(kubectl.stdin.borrow().is_empty());
    assert!(kubectl.calls.borrow().iter().all(|call| call[0] == "get"));
}

#[test]
fn start_fails_once_the_timeout_is_spent() {
    let kubectl = FakeKubectl {
        elapsed: Duration::from_secs(600),
        ..FakeKubectl::default()
    };
    let error = start(&request(), &kubectl).unwrap_err();
    assert!(
        matches!(&error, K8sCaptureError::Timeout { stage, .. } if stage.contains("stitch-otel-collector")),
        "{error}"
    );
}

#[test]
fn start_rejects_invalid_arguments_without_running_kubectl() {
    let cases: [fn(&mut StartRequest); 7] = [
        |r| r.namespace = "Hotel".into(),
        |r| r.deployments.clear(),
        |r| r.deployments.push("frontend".into()),
        |r| r.deployments.push("stitch-otel-collector".into()),
        |r| r.sampler_ratio = 1.5,
        |r| r.collector_image = "bad image".into(),
        |r| r.timeout = Duration::ZERO,
    ];
    for mutate in cases {
        let mut invalid = request();
        mutate(&mut invalid);
        let kubectl = FakeKubectl::default();
        assert!(matches!(
            start(&invalid, &kubectl),
            Err(K8sCaptureError::Invalid(_))
        ));
        assert!(kubectl.calls.borrow().is_empty());
    }
}

#[test]
fn collector_manifest_declares_receivers_exporter_and_reader() {
    let manifest: serde_json::Value = serde_json::from_str(&collector_manifest(
        "hotel",
        "collector@sha256:a",
        "reader@sha256:b",
    ))
    .unwrap();
    let items = manifest["items"].as_array().unwrap();
    let kinds: Vec<_> = items.iter().map(|i| i["kind"].as_str().unwrap()).collect();
    assert_eq!(kinds, ["ConfigMap", "Deployment", "Service"]);
    for item in items {
        assert_eq!(item["metadata"]["name"], "stitch-otel-collector");
        assert_eq!(item["metadata"]["namespace"], "hotel");
    }
    assert_eq!(items[0]["data"]["config.yaml"], COLLECTOR_CONFIG);
    let pod = &items[1]["spec"]["template"]["spec"];
    assert_eq!(pod["containers"][0]["image"], "collector@sha256:a");
    assert_eq!(pod["containers"][1]["name"], "reader");
    assert_eq!(pod["containers"][1]["image"], "reader@sha256:b");
    assert_eq!(
        pod["containers"][0]["resources"]["limits"]["memory"],
        "512Mi"
    );
    assert!(pod["volumes"][1]["emptyDir"].is_object());
    let ports: Vec<_> = items[2]["spec"]["ports"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| (p["port"].as_u64().unwrap(), p["protocol"].as_str().unwrap()))
        .collect();
    assert_eq!(ports, [(4317, "TCP"), (4318, "TCP"), (6831, "UDP")]);
    for needle in [
        "endpoint: 0.0.0.0:4317",
        "endpoint: 0.0.0.0:4318",
        "thrift_compact",
        "endpoint: 0.0.0.0:6831",
        "memory_limiter",
        "path: /data/otel.jsonl",
        "processors: [memory_limiter, batch]",
    ] {
        assert!(COLLECTOR_CONFIG.contains(needle), "{needle}");
    }
}

fn collect_request(dir: &std::path::Path) -> CollectRequest {
    CollectRequest {
        namespace: "hotel".into(),
        output_dir: dir.join("otel"),
        settle: Duration::from_secs(5),
        windows: vec![TimeWindow {
            start_unix_nanos: 1_699_999_999_000_000_000,
            end_unix_nanos: 1_700_000_001_000_000_000,
            label: Some("measure".into()),
        }],
        label: None,
        source_root: Some("src".into()),
    }
}

#[test]
fn collect_writes_a_manifest_that_ingest_accepts() {
    let dir = tempfile::tempdir().unwrap();
    let kubectl = FakeKubectl {
        export: format!("{COLLECTOR_LINE}\n{COLLECTOR_LINE}\n{{\"resourceSp").into_bytes(),
        ..FakeKubectl::default()
    };
    let summary = collect(&collect_request(dir.path()), &kubectl).unwrap();
    assert_eq!(*kubectl.pauses.borrow(), [Duration::from_secs(5)]);
    assert_eq!(
        kubectl.calls.borrow()[0],
        strs(&[
            "exec",
            "--namespace",
            "hotel",
            "deployment/stitch-otel-collector",
            "--container",
            "reader",
            "--",
            "cat",
            "/data/otel.jsonl"
        ])
    );
    assert_eq!(summary.spans, 2);
    assert_eq!(summary.traces, 1);
    assert_eq!(summary.services, ["frontend"]);
    assert!(summary.dropped_partial_line);
    let manifest_path = dir.path().join("otel/manifest.json");
    let raw: serde_json::Value =
        serde_json::from_slice(&fs::read(&manifest_path).unwrap()).unwrap();
    assert_eq!(
        raw,
        serde_json::json!({
            "schema_version": 1,
            "label": "otel-k8s hotel",
            "inputs": [{"format": "otlp_jsonl", "path": "otel.jsonl", "clock": "wall"}],
            "source_root": "src",
            "source_revision": null,
            "windows": [{
                "start_unix_nanos": 1_699_999_999_000_000_000_u64,
                "end_unix_nanos": 1_700_000_001_000_000_000_u64,
                "label": "measure"
            }],
        })
    );
    let manifest = load_manifest(&manifest_path).unwrap();
    assert_eq!(manifest.windows.len(), 1);
}

#[test]
fn collect_fails_without_spans_but_keeps_the_export() {
    let dir = tempfile::tempdir().unwrap();
    let kubectl = FakeKubectl::default();
    let error = collect(&collect_request(dir.path()), &kubectl).unwrap_err();
    assert!(error.to_string().contains("no spans"), "{error}");
    assert!(dir.path().join("otel/otel.jsonl").is_file());
    assert!(!dir.path().join("otel/manifest.json").exists());
}

#[test]
fn collect_rejects_invalid_windows_before_reading() {
    let dir = tempfile::tempdir().unwrap();
    let mut request = collect_request(dir.path());
    request.windows[0].end_unix_nanos = request.windows[0].start_unix_nanos;
    let kubectl = FakeKubectl::default();
    assert!(matches!(
        collect(&request, &kubectl),
        Err(K8sCaptureError::Invalid(_))
    ));
    assert!(kubectl.calls.borrow().is_empty());
}
