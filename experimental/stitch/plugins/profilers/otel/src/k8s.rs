//! `OTel` trace capture for applications on Kubernetes.
//!
//! `start` deploys an `OTel` Collector into the application's namespace and
//! points the named Deployments' `OTel` and Jaeger SDKs at it; `collect` reads
//! the collector's OTLP JSON lines back and writes a Stitch run manifest.
//! Every cluster operation goes through [`Kubectl`], so callers can supply
//! their own execution policy and tests never reach a cluster.

use crate::OtlpJsonlImporter;
use serde::Serialize;
use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use stitch_core::{Observation, TimeWindow};
use stitch_importer_api::{ImportError, Importer, InputSpec};
use stitch_session::RunManifest;
use thiserror::Error;

/// The name of the collector's `ConfigMap`, Deployment and Service.
pub const COLLECTOR_NAME: &str = "stitch-otel-collector";
/// `otel/opentelemetry-collector-contrib` 0.159.0, pinned by its multi-arch index digest.
pub const DEFAULT_COLLECTOR_IMAGE: &str = "otel/opentelemetry-collector-contrib:0.159.0@sha256:1f2c54a30e713fac6b3ae77a1ec84010c2007e29ced8ec666214fc2f6739c1cc";
/// `busybox` 1.37.0, pinned by its multi-arch index digest.
pub const DEFAULT_SIDECAR_IMAGE: &str =
    "busybox:1.37.0@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec";
/// Where the collector's file exporter writes, inside the shared volume.
pub const COLLECTOR_OUTPUT: &str = "/data/otel.jsonl";
const READER_CONTAINER: &str = "reader";
const OTLP_GRPC_PORT: u16 = 4317;
const OTLP_HTTP_PORT: u16 = 4318;
const JAEGER_COMPACT_PORT: u16 = 6831;
const HEALTH_PORT: u16 = 13133;
const COLLECTOR_UID: u32 = 10001;

/// The collector pipeline: OTLP and Jaeger compact thrift in, OTLP JSON lines
/// out. The memory limiter keeps the collector below its container limit so
/// it refuses data under overload instead of being OOM-killed.
pub const COLLECTOR_CONFIG: &str = r"extensions:
  health_check:
    endpoint: 0.0.0.0:13133
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
  jaeger:
    protocols:
      thrift_compact:
        endpoint: 0.0.0.0:6831
processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 400
    spike_limit_mib: 100
  batch:
    send_batch_size: 2048
    timeout: 1s
exporters:
  file:
    path: /data/otel.jsonl
    format: json
    flush_interval: 1s
service:
  extensions: [health_check]
  telemetry:
    metrics:
      level: none
  pipelines:
    traces:
      receivers: [otlp, jaeger]
      processors: [memory_limiter, batch]
      exporters: [file]
";

#[derive(Debug, Error)]
pub enum K8sCaptureError {
    #[error("invalid otel-k8s capture: {0}")]
    Invalid(String),
    #[error("kubectl {args} failed: {detail}")]
    Kubectl { args: String, detail: String },
    #[error("timed out after {seconds}s {stage}")]
    Timeout { seconds: u64, stage: String },
    #[error("I/O error at {path}: {source}")]
    Io {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error(transparent)]
    Import(#[from] ImportError),
}

/// The process and clock boundary of a capture.
pub trait Kubectl {
    /// Run kubectl with `args`, feeding `stdin` if given, and return stdout.
    ///
    /// # Errors
    ///
    /// Returns an error if kubectl cannot start or exits unsuccessfully.
    fn run(&self, args: &[String], stdin: Option<&[u8]>) -> Result<Vec<u8>, K8sCaptureError>;
    /// Monotonic time elapsed since the capture command began.
    fn elapsed(&self) -> Duration;
    /// The current wall-clock time in Unix nanoseconds.
    fn unix_nanos(&self) -> u64;
    /// Wait for `duration`, for example to let the collector flush.
    fn pause(&self, duration: Duration);
}

/// Runs a kubectl executable; it honors `KUBECONFIG` and the current context.
pub struct SystemKubectl {
    program: PathBuf,
    started: Instant,
}

impl SystemKubectl {
    #[must_use]
    pub fn new(program: impl Into<PathBuf>) -> Self {
        Self {
            program: program.into(),
            started: Instant::now(),
        }
    }
}

impl Kubectl for SystemKubectl {
    fn run(&self, args: &[String], stdin: Option<&[u8]>) -> Result<Vec<u8>, K8sCaptureError> {
        let failed = |detail: String| K8sCaptureError::Kubectl {
            args: args.join(" "),
            detail,
        };
        let mut child = Command::new(&self.program)
            .args(args)
            .stdin(if stdin.is_some() {
                Stdio::piped()
            } else {
                Stdio::null()
            })
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| failed(format!("cannot run {}: {e}", self.program.display())))?;
        if let (Some(input), Some(mut pipe)) = (stdin, child.stdin.take()) {
            std::io::Write::write_all(&mut pipe, input)
                .map_err(|e| failed(format!("cannot write stdin: {e}")))?;
        }
        let output = child
            .wait_with_output()
            .map_err(|e| failed(e.to_string()))?;
        if !output.status.success() {
            return Err(failed(format!(
                "{}: {}",
                output.status,
                String::from_utf8_lossy(&output.stderr).trim()
            )));
        }
        Ok(output.stdout)
    }

    fn elapsed(&self) -> Duration {
        self.started.elapsed()
    }

    fn unix_nanos(&self) -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |d| u64::try_from(d.as_nanos()).unwrap_or(u64::MAX))
    }

    fn pause(&self, duration: Duration) {
        std::thread::sleep(duration);
    }
}

#[derive(Clone, Debug)]
pub struct StartRequest {
    pub namespace: String,
    pub deployments: Vec<String>,
    pub sampler_ratio: f64,
    pub collector_image: String,
    pub sidecar_image: String,
    pub timeout: Duration,
}

/// What `start` prints: the evaluator and later commands rely on these keys.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct StartReport {
    pub namespace: String,
    /// The collector Service's in-cluster DNS name.
    pub collector_service: String,
    pub deployments: Vec<String>,
    pub sampler_ratio: f64,
    /// Wall-clock time after every instrumented rollout completed.
    pub started_unix_nanos: u64,
}

#[derive(Clone, Debug)]
pub struct CollectRequest {
    pub namespace: String,
    pub output_dir: PathBuf,
    pub settle: Duration,
    pub windows: Vec<TimeWindow>,
    pub label: Option<String>,
    pub source_root: Option<PathBuf>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CollectSummary {
    pub spans: usize,
    pub traces: usize,
    pub services: Vec<String>,
    pub bytes: usize,
    /// Whether an incomplete final line, still being written, was dropped.
    pub dropped_partial_line: bool,
}

/// The collector Service's in-cluster DNS name.
#[must_use]
pub fn collector_host(namespace: &str) -> String {
    format!("{COLLECTOR_NAME}.{namespace}.svc.cluster.local")
}

fn is_dns_label(value: &str, max: usize) -> bool {
    !value.is_empty()
        && value.len() <= max
        && value
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-' || b == b'.')
        && !value.starts_with(['-', '.'])
        && !value.ends_with(['-', '.'])
}

fn validate_namespace(namespace: &str) -> Result<(), K8sCaptureError> {
    if is_dns_label(namespace, 63) && !namespace.contains('.') {
        Ok(())
    } else {
        Err(K8sCaptureError::Invalid(format!(
            "namespace {namespace:?} is not a DNS-1123 label"
        )))
    }
}

fn validate_image(flag: &str, image: &str) -> Result<(), K8sCaptureError> {
    if image.is_empty() || image.chars().any(char::is_whitespace) {
        return Err(K8sCaptureError::Invalid(format!(
            "{flag} {image:?} is not an image reference"
        )));
    }
    Ok(())
}

impl StartRequest {
    /// Check names, images, the sampler ratio and the timeout.
    ///
    /// # Errors
    ///
    /// Returns an error naming the first invalid value.
    pub fn validate(&self) -> Result<(), K8sCaptureError> {
        validate_namespace(&self.namespace)?;
        if self.deployments.is_empty() {
            return Err(K8sCaptureError::Invalid(
                "at least one --deployment is required".into(),
            ));
        }
        let mut seen = BTreeSet::new();
        for deployment in &self.deployments {
            if !is_dns_label(deployment, 253) {
                return Err(K8sCaptureError::Invalid(format!(
                    "deployment {deployment:?} is not a DNS-1123 name"
                )));
            }
            if deployment == COLLECTOR_NAME {
                return Err(K8sCaptureError::Invalid(format!(
                    "deployment {COLLECTOR_NAME} is the collector itself"
                )));
            }
            if !seen.insert(deployment) {
                return Err(K8sCaptureError::Invalid(format!(
                    "deployment {deployment:?} is repeated"
                )));
            }
        }
        if !(self.sampler_ratio.is_finite() && (0.0..=1.0).contains(&self.sampler_ratio)) {
            return Err(K8sCaptureError::Invalid(format!(
                "sampler ratio {} must be between 0 and 1",
                self.sampler_ratio
            )));
        }
        validate_image("--collector-image", &self.collector_image)?;
        validate_image("--sidecar-image", &self.sidecar_image)?;
        if self.timeout.is_zero() {
            return Err(K8sCaptureError::Invalid("timeout must be positive".into()));
        }
        Ok(())
    }
}

/// The collector's `ConfigMap`, Deployment and Service as one `v1/List`, which
/// `kubectl apply` accepts as JSON.
#[must_use]
pub fn collector_manifest(namespace: &str, collector_image: &str, sidecar_image: &str) -> String {
    let labels = serde_json::json!({
        "app.kubernetes.io/name": COLLECTOR_NAME,
        "app.kubernetes.io/managed-by": "stitch",
    });
    let metadata = serde_json::json!({
        "name": COLLECTOR_NAME,
        "namespace": namespace,
        "labels": labels,
    });
    let config_map = serde_json::json!({
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": metadata,
        "data": {"config.yaml": COLLECTOR_CONFIG},
    });
    let deployment = serde_json::json!({
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": metadata,
        "spec": {
            "replicas": 1,
            // One collector at a time: a second pod would split the file.
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {"app.kubernetes.io/name": COLLECTOR_NAME}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    // Both containers share the collector's user so the reader
                    // can open the exporter's file.
                    "securityContext": {
                        "runAsNonRoot": true,
                        "runAsUser": COLLECTOR_UID,
                        "runAsGroup": COLLECTOR_UID,
                        "fsGroup": COLLECTOR_UID,
                    },
                    "containers": [
                        {
                            "name": "collector",
                            "image": collector_image,
                            "args": ["--config=/conf/config.yaml"],
                            "env": [{"name": "GOMEMLIMIT", "value": "420MiB"}],
                            "ports": [
                                {"name": "otlp-grpc", "containerPort": OTLP_GRPC_PORT, "protocol": "TCP"},
                                {"name": "otlp-http", "containerPort": OTLP_HTTP_PORT, "protocol": "TCP"},
                                {"name": "jaeger-compact", "containerPort": JAEGER_COMPACT_PORT, "protocol": "UDP"},
                            ],
                            "readinessProbe": {
                                "httpGet": {"path": "/", "port": HEALTH_PORT},
                                "periodSeconds": 2,
                            },
                            "resources": {
                                "requests": {"cpu": "200m", "memory": "256Mi"},
                                "limits": {"cpu": "2", "memory": "512Mi"},
                            },
                            "volumeMounts": [
                                {"name": "config", "mountPath": "/conf", "readOnly": true},
                                {"name": "data", "mountPath": "/data"},
                            ],
                        },
                        {
                            "name": READER_CONTAINER,
                            "image": sidecar_image,
                            "command": ["sh", "-c", "trap 'exit 0' TERM; while true; do sleep 3600 & wait $!; done"],
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "16Mi"},
                                "limits": {"cpu": "500m", "memory": "128Mi"},
                            },
                            "volumeMounts": [{"name": "data", "mountPath": "/data", "readOnly": true}],
                        },
                    ],
                    "volumes": [
                        {"name": "config", "configMap": {"name": COLLECTOR_NAME}},
                        {"name": "data", "emptyDir": {"sizeLimit": "4Gi"}},
                    ],
                },
            },
        },
    });
    let service = serde_json::json!({
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": metadata,
        "spec": {
            "selector": {"app.kubernetes.io/name": COLLECTOR_NAME},
            "ports": [
                {"name": "otlp-grpc", "port": OTLP_GRPC_PORT, "targetPort": OTLP_GRPC_PORT, "protocol": "TCP"},
                {"name": "otlp-http", "port": OTLP_HTTP_PORT, "targetPort": OTLP_HTTP_PORT, "protocol": "TCP"},
                {"name": "jaeger-compact", "port": JAEGER_COMPACT_PORT, "targetPort": JAEGER_COMPACT_PORT, "protocol": "UDP"},
            ],
        },
    });
    let list = serde_json::json!({
        "apiVersion": "v1",
        "kind": "List",
        "items": [config_map, deployment, service],
    });
    let mut text = serde_json::to_string_pretty(&list).unwrap_or_default();
    text.push('\n');
    text
}

/// The environment `start` sets on every container of each named Deployment.
#[must_use]
pub fn instrumentation_env(namespace: &str, sampler_ratio: f64) -> Vec<(String, String)> {
    let host = collector_host(namespace);
    let ratio = sampler_ratio.to_string();
    [
        (
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            format!("http://{host}:{OTLP_GRPC_PORT}"),
        ),
        ("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc".into()),
        ("OTEL_TRACES_SAMPLER", "parentbased_traceidratio".into()),
        ("OTEL_TRACES_SAMPLER_ARG", ratio.clone()),
        ("JAEGER_AGENT_HOST", host),
        ("JAEGER_AGENT_PORT", JAEGER_COMPACT_PORT.to_string()),
        ("JAEGER_SAMPLER_TYPE", "probabilistic".into()),
        ("JAEGER_SAMPLER_PARAM", ratio),
    ]
    .into_iter()
    .map(|(key, value)| (key.to_owned(), value))
    .collect()
}

fn args(values: &[&str]) -> Vec<String> {
    values.iter().map(|value| (*value).to_owned()).collect()
}

fn remaining(
    kubectl: &dyn Kubectl,
    timeout: Duration,
    stage: &str,
) -> Result<String, K8sCaptureError> {
    let left = timeout.saturating_sub(kubectl.elapsed());
    if left.is_zero() {
        return Err(K8sCaptureError::Timeout {
            seconds: timeout.as_secs(),
            stage: stage.into(),
        });
    }
    let seconds = left.as_secs() + u64::from(left.subsec_nanos() > 0);
    Ok(format!("--timeout={seconds}s"))
}

fn rollout_status(
    kubectl: &dyn Kubectl,
    namespace: &str,
    deployment: &str,
    timeout: Duration,
) -> Result<(), K8sCaptureError> {
    let stage = format!("waiting for deployment/{deployment} to roll out");
    let limit = remaining(kubectl, timeout, &stage)?;
    kubectl
        .run(
            &args(&[
                "rollout",
                "status",
                &format!("deployment/{deployment}"),
                "--namespace",
                namespace,
                "--watch=true",
                &limit,
            ]),
            None,
        )
        .map(drop)
        .map_err(|error| match error {
            K8sCaptureError::Kubectl { detail, .. } => K8sCaptureError::Kubectl {
                args: format!("rollout status deployment/{deployment}"),
                detail: format!("{detail} ({stage} within {}s)", timeout.as_secs()),
            },
            other => other,
        })
}

/// Deploy the collector and instrument the named Deployments.
///
/// Only the named Deployments are changed; other workloads in the namespace,
/// such as the data tier, keep running untouched.
///
/// # Errors
///
/// Returns an error for invalid arguments, a missing Deployment, a kubectl
/// failure, or a rollout that does not finish within the timeout.
pub fn start(
    request: &StartRequest,
    kubectl: &dyn Kubectl,
) -> Result<StartReport, K8sCaptureError> {
    request.validate()?;
    let namespace = request.namespace.as_str();
    for deployment in &request.deployments {
        kubectl.run(
            &args(&[
                "get",
                "deployment",
                deployment,
                "--namespace",
                namespace,
                "--output=name",
            ]),
            None,
        )?;
    }
    let manifest = collector_manifest(namespace, &request.collector_image, &request.sidecar_image);
    kubectl.run(
        &args(&["apply", "--namespace", namespace, "--filename=-"]),
        Some(manifest.as_bytes()),
    )?;
    rollout_status(kubectl, namespace, COLLECTOR_NAME, request.timeout)?;
    let env = instrumentation_env(namespace, request.sampler_ratio);
    for deployment in &request.deployments {
        let mut command = args(&[
            "set",
            "env",
            &format!("deployment/{deployment}"),
            "--namespace",
            namespace,
            "--containers=*",
        ]);
        command.extend(env.iter().map(|(key, value)| format!("{key}={value}")));
        kubectl.run(&command, None)?;
    }
    for deployment in &request.deployments {
        rollout_status(kubectl, namespace, deployment, request.timeout)?;
    }
    Ok(StartReport {
        namespace: request.namespace.clone(),
        collector_service: collector_host(namespace),
        deployments: request.deployments.clone(),
        sampler_ratio: request.sampler_ratio,
        started_unix_nanos: kubectl.unix_nanos(),
    })
}

/// Drop a final line that is still being written: one with no newline that
/// does not parse as JSON.
fn complete_lines(mut data: Vec<u8>) -> (Vec<u8>, bool) {
    if data.is_empty() || data.ends_with(b"\n") {
        return (data, false);
    }
    let start = data
        .iter()
        .rposition(|byte| *byte == b'\n')
        .map_or(0, |index| index + 1);
    if serde_json::from_slice::<serde_json::Value>(&data[start..]).is_ok() {
        data.push(b'\n');
        return (data, false);
    }
    data.truncate(start);
    (data, true)
}

fn write(path: &Path, data: &[u8]) -> Result<(), K8sCaptureError> {
    fs::write(path, data).map_err(|source| K8sCaptureError::Io {
        path: path.to_owned(),
        source,
    })
}

/// Read the collector's spans into `output_dir/otel.jsonl` and write
/// `output_dir/manifest.json`. The collector keeps running.
///
/// # Errors
///
/// Returns an error for invalid windows, a kubectl failure, a malformed
/// export, or an export without spans. The export file is kept for diagnosis.
pub fn collect(
    request: &CollectRequest,
    kubectl: &dyn Kubectl,
) -> Result<CollectSummary, K8sCaptureError> {
    validate_namespace(&request.namespace)?;
    TimeWindow::validate_all(&request.windows).map_err(K8sCaptureError::Invalid)?;
    let label = request
        .label
        .clone()
        .unwrap_or_else(|| format!("otel-k8s {}", request.namespace));
    if label.trim().is_empty() {
        return Err(K8sCaptureError::Invalid("label cannot be empty".into()));
    }
    kubectl.pause(request.settle);
    let raw = kubectl.run(
        &args(&[
            "exec",
            "--namespace",
            &request.namespace,
            &format!("deployment/{COLLECTOR_NAME}"),
            "--container",
            READER_CONTAINER,
            "--",
            "cat",
            COLLECTOR_OUTPUT,
        ]),
        None,
    )?;
    let (data, dropped_partial_line) = complete_lines(raw);
    fs::create_dir_all(&request.output_dir).map_err(|source| K8sCaptureError::Io {
        path: request.output_dir.clone(),
        source,
    })?;
    let export = request.output_dir.join("otel.jsonl");
    write(&export, &data)?;
    let input = InputSpec {
        format: "otlp_jsonl".into(),
        path: PathBuf::from("otel.jsonl"),
        clock: "wall".into(),
    };
    let observations = OtlpJsonlImporter.import(&InputSpec {
        path: export.clone(),
        ..input.clone()
    })?;
    let mut traces = BTreeSet::new();
    let mut services = BTreeSet::new();
    for observation in &observations {
        if let Observation::Activity(span) = observation {
            traces.extend(span.trace_id.as_deref());
            services.insert(span.service.as_deref().unwrap_or("unknown"));
        }
    }
    if observations.is_empty() {
        return Err(K8sCaptureError::Invalid(format!(
            "the collector in namespace {} recorded no spans; check that the instrumented \
             Deployments received traffic and export to {}; the export is at {}",
            request.namespace,
            collector_host(&request.namespace),
            export.display()
        )));
    }
    let manifest = RunManifest {
        schema_version: 1,
        label,
        inputs: vec![input],
        source_root: request.source_root.clone(),
        source_revision: None,
        windows: request.windows.clone(),
    };
    manifest
        .validate()
        .map_err(|e| K8sCaptureError::Invalid(e.to_string()))?;
    let mut serialized = serde_json::to_vec_pretty(&manifest)
        .map_err(|e| K8sCaptureError::Invalid(e.to_string()))?;
    serialized.push(b'\n');
    write(&request.output_dir.join("manifest.json"), &serialized)?;
    Ok(CollectSummary {
        spans: observations.len(),
        traces: traces.len(),
        services: services.into_iter().map(str::to_owned).collect(),
        bytes: data.len(),
        dropped_partial_line,
    })
}

#[cfg(test)]
mod tests {
    use super::complete_lines;

    #[test]
    fn complete_lines_keeps_whole_documents_and_drops_a_torn_tail() {
        assert_eq!(complete_lines(b"{}\n".to_vec()), (b"{}\n".to_vec(), false));
        assert_eq!(
            complete_lines(b"{}\n{}".to_vec()),
            (b"{}\n{}\n".to_vec(), false)
        );
        assert_eq!(
            complete_lines(b"{}\n{\"a\"".to_vec()),
            (b"{}\n".to_vec(), true)
        );
        assert_eq!(complete_lines(b"{\"a".to_vec()), (Vec::new(), true));
        assert_eq!(complete_lines(Vec::new()), (Vec::new(), false));
    }
}
