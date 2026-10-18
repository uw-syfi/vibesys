//! Linux perf script importer.
#![forbid(unsafe_code)]
use serde::{Deserialize, Serialize};
use std::fs;
use std::fs::File;
use std::path::PathBuf;
use std::process::{Command, Output};
use stitch_session::RunManifest;
use thiserror::Error;

use std::io::{BufRead, BufReader};
use std::path::Path;
use stitch_core::{CpuSample, Observation, Origin};
use stitch_importer_api::{ImportError, Importer, InputSpec};

pub struct PerfScriptImporter;

fn perf_time_ns(raw: &str) -> Option<u64> {
    let raw = raw.trim_end_matches(':');
    let (seconds, fraction) = raw.split_once('.')?;
    if fraction.is_empty() || fraction.len() > 9 || !fraction.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let seconds: u64 = seconds.parse().ok()?;
    let nanos: u64 = fraction.parse().ok()?;
    seconds
        .checked_mul(1_000_000_000)?
        .checked_add(nanos * 10_u64.pow(u32::try_from(9 - fraction.len()).ok()?))
}

fn finish_perf_sample(
    out: &mut Vec<Observation>,
    current: &mut Option<CpuSample>,
    path: &Path,
) -> Result<(), ImportError> {
    if let Some(sample) = current.take() {
        if sample.frames.is_empty() {
            return Err(invalid(
                "perf_script",
                path,
                format!(
                    "sample at line {} has no frames; export with the ip field and call graphs",
                    sample.origin.record
                ),
            ));
        }
        out.push(Observation::CpuSample(sample));
    }
    Ok(())
}

impl Importer for PerfScriptImporter {
    fn format(&self) -> &'static str {
        "perf_script"
    }

    fn import(&self, input: &InputSpec) -> Result<Vec<Observation>, ImportError> {
        let file = File::open(&input.path).map_err(|source| ImportError::Io {
            path: input.path.clone(),
            source,
        })?;
        let mut out = Vec::new();
        let mut current: Option<CpuSample> = None;
        for (index, line) in BufReader::new(file).lines().enumerate() {
            let line = line.map_err(|source| ImportError::Io {
                path: input.path.clone(),
                source,
            })?;
            if line.starts_with('#') {
                continue;
            }
            if line.trim().is_empty() {
                finish_perf_sample(&mut out, &mut current, &input.path)?;
                continue;
            }
            if line.starts_with(char::is_whitespace) {
                let sample = current.as_mut().ok_or_else(|| {
                    invalid(
                        "perf_script",
                        &input.path,
                        format!("line {} has a frame without a sample", index + 1),
                    )
                })?;
                let frame = line.trim();
                let frame = frame
                    .split_once(char::is_whitespace)
                    .map(|(_, symbol)| symbol.trim())
                    .filter(|symbol| !symbol.is_empty())
                    .unwrap_or(frame);
                sample.frames.push(frame.to_owned());
                continue;
            }
            finish_perf_sample(&mut out, &mut current, &input.path)?;
            // perf script prints pid/tid as one field, followed by a header
            // and indented call-chain frames. Older one-line exports are also
            // accepted so existing manifests remain readable.
            let mut fields = line.split_whitespace();
            let first = fields.next().unwrap_or_default();
            let (pid, tid, time_token) = if let Some((pid, tid)) = first.split_once('/') {
                (pid.parse().ok(), tid.parse().ok(), fields.next())
            } else {
                (
                    first.parse().ok(),
                    fields.next().and_then(|s| s.parse().ok()),
                    fields.next(),
                )
            };
            let time = time_token.and_then(perf_time_ns);
            let (Some(pid), Some(tid), Some(time)) = (pid, tid, time) else {
                return Err(invalid(
                    "perf_script",
                    &input.path,
                    format!("line {} has unexpected perf script fields", index + 1),
                ));
            };
            let symbol = fields.collect::<Vec<_>>().join(" ");
            current = Some(CpuSample {
                id: format!("perf:{}:{}", input.path.display(), index + 1),
                pid,
                tid,
                at_ns: time,
                clock: input.clock.clone(),
                frames: if symbol.is_empty() {
                    Vec::new()
                } else {
                    vec![symbol]
                },
                origin: Origin {
                    source: "perf".into(),
                    artifact: input.path.display().to_string(),
                    record: (index + 1).to_string(),
                },
            });
        }
        finish_perf_sample(&mut out, &mut current, &input.path)?;
        Ok(out)
    }
}

fn invalid(format: &'static str, path: &Path, detail: impl Into<String>) -> ImportError {
    ImportError::Invalid {
        format,
        path: path.to_owned(),
        detail: detail.into(),
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CaptureRequest {
    pub pid: u32,
    pub seconds: u32,
    pub output_dir: PathBuf,
    pub stack_mode: CaptureStackMode,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CaptureStackMode {
    FramePointer,
    #[default]
    Dwarf,
}

#[derive(Debug, Error)]
pub enum CaptureError {
    #[error("invalid perf capture: {0}")]
    Invalid(String),
    #[error("I/O error at {path}: {source}")]
    Io {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("command {program} failed: {detail}")]
    Command { program: String, detail: String },
    #[error(transparent)]
    Import(#[from] ImportError),
}

/// The process boundary needed by capture; callers can supply an implementation
/// that enforces their own execution policy.
pub trait CommandRunner {
    /// Run one profiler command and return its output.
    ///
    /// # Errors
    ///
    /// Returns an error if the command fails or exits unsuccessfully.
    fn run(&self, program: &str, args: &[String]) -> Result<Output, CaptureError>;
}

pub struct SystemCommandRunner;

impl CommandRunner for SystemCommandRunner {
    fn run(&self, program: &str, args: &[String]) -> Result<Output, CaptureError> {
        let output =
            Command::new(program)
                .args(args)
                .output()
                .map_err(|e| CaptureError::Command {
                    program: program.into(),
                    detail: e.to_string(),
                })?;
        if !output.status.success() {
            return Err(CaptureError::Command {
                program: program.into(),
                detail: String::from_utf8_lossy(&output.stderr).trim().into(),
            });
        }
        Ok(output)
    }
}

/// Capture one process with perf and convert its samples to an importable
/// script. Applications may emit `OTel` or dial9 artifacts in parallel.
///
/// # Errors
///
/// Returns an error for invalid capture parameters, I/O failures, or profiler failures.
pub fn capture_perf(
    request: &CaptureRequest,
    runner: &dyn CommandRunner,
) -> Result<RunManifest, CaptureError> {
    if request.pid == 0 || request.seconds == 0 || request.seconds > 3600 {
        return Err(CaptureError::Invalid(
            "pid must be positive and seconds must be between 1 and 3600".into(),
        ));
    }
    if request.output_dir.exists() {
        return Err(CaptureError::Invalid(format!(
            "output directory already exists: {}",
            request.output_dir.display()
        )));
    }
    fs::create_dir_all(&request.output_dir).map_err(|source| CaptureError::Io {
        path: request.output_dir.clone(),
        source,
    })?;
    let data = request.output_dir.join("perf.data");
    let script = request.output_dir.join("perf.script");
    let record_args = vec![
        "record".into(),
        "--clockid".into(),
        "mono".into(),
        "-F".into(),
        "99".into(),
        "--call-graph".into(),
        match request.stack_mode {
            CaptureStackMode::FramePointer => "fp",
            CaptureStackMode::Dwarf => "dwarf,8192",
        }
        .into(),
        "-o".into(),
        data.display().to_string(),
        "-p".into(),
        request.pid.to_string(),
        "--".into(),
        "sleep".into(),
        request.seconds.to_string(),
    ];
    runner.run("perf", &record_args)?;
    let script_args = vec![
        "script".into(),
        "--no-inline".into(),
        "--ns".into(),
        "-F".into(),
        "pid,tid,time,ip,sym".into(),
        "-i".into(),
        data.display().to_string(),
    ];
    let output = runner.run("perf", &script_args)?;
    fs::write(&script, output.stdout).map_err(|source| CaptureError::Io {
        path: script.clone(),
        source,
    })?;
    let captured = PerfScriptImporter.import(&InputSpec {
        format: "perf_script".into(),
        path: script,
        clock: "monotonic".into(),
    })?;
    if captured.is_empty() {
        return Err(CaptureError::Invalid(format!(
            "perf recorded no CPU samples for pid {}; artifacts remain in {}",
            request.pid,
            request.output_dir.display()
        )));
    }
    let manifest = RunManifest {
        schema_version: 1,
        label: format!("perf pid {}", request.pid),
        inputs: vec![InputSpec {
            format: "perf_script".into(),
            path: PathBuf::from("perf.script"),
            clock: "monotonic".into(),
        }],
        source_root: None,
        source_revision: None,
    };
    let manifest_path = request.output_dir.join("manifest.json");
    let serialized =
        serde_json::to_vec_pretty(&manifest).map_err(|e| CaptureError::Invalid(e.to_string()))?;
    fs::write(&manifest_path, serialized).map_err(|source| CaptureError::Io {
        path: manifest_path,
        source,
    })?;
    Ok(manifest)
}
