//! Capture and import orchestration. The process runner owns child processes.

use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use stitch_core::{Graph, GraphError, Observation};
use stitch_mapping::{MappingError, MappingRegistry, SourceTarget, WorkspaceLocationResolver};
use stitch_sources::{builtin_importers, ImportError, ImporterRegistry, InputSpec};
use thiserror::Error;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunManifest {
    pub schema_version: u32,
    pub label: String,
    pub inputs: Vec<InputSpec>,
    pub source_root: Option<PathBuf>,
    pub source_revision: Option<String>,
}

impl RunManifest {
    pub fn validate(&self) -> Result<(), SessionError> {
        if self.schema_version != 1 {
            return Err(SessionError::Invalid(format!(
                "unsupported schema_version {} (expected 1)",
                self.schema_version
            )));
        }
        if self.label.trim().is_empty() {
            return Err(SessionError::Invalid("label cannot be empty".into()));
        }
        if self.inputs.is_empty() {
            return Err(SessionError::Invalid("inputs cannot be empty".into()));
        }
        for input in &self.inputs {
            if input.clock.trim().is_empty() {
                return Err(SessionError::Invalid(format!(
                    "clock cannot be empty for {}",
                    input.path.display()
                )));
            }
        }
        Ok(())
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
pub enum SessionError {
    #[error("invalid session: {0}")]
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
    #[error(transparent)]
    Graph(#[from] GraphError),
    #[error(transparent)]
    Mapping(#[from] MappingError),
}

/// The process boundary needed by capture; callers can supply an implementation
/// that enforces their own execution policy.
pub trait CommandRunner {
    fn run(&self, program: &str, args: &[String]) -> Result<Output, SessionError>;
}

pub struct SystemCommandRunner;

impl CommandRunner for SystemCommandRunner {
    fn run(&self, program: &str, args: &[String]) -> Result<Output, SessionError> {
        let output =
            Command::new(program)
                .args(args)
                .output()
                .map_err(|e| SessionError::Command {
                    program: program.into(),
                    detail: e.to_string(),
                })?;
        if !output.status.success() {
            return Err(SessionError::Command {
                program: program.into(),
                detail: String::from_utf8_lossy(&output.stderr).trim().into(),
            });
        }
        Ok(output)
    }
}

pub fn load_manifest(path: &Path) -> Result<RunManifest, SessionError> {
    let contents = fs::read_to_string(path).map_err(|source| SessionError::Io {
        path: path.to_owned(),
        source,
    })?;
    let mut manifest: RunManifest = serde_json::from_str(&contents)
        .map_err(|e| SessionError::Invalid(format!("{}: {e}", path.display())))?;
    let base = path.parent().unwrap_or_else(|| Path::new("."));
    for input in &mut manifest.inputs {
        if input.path.is_relative() {
            input.path = base.join(&input.path);
        }
    }
    if let Some(root) = &mut manifest.source_root {
        if root.is_relative() {
            *root = base.join(&root);
        }
    }
    manifest.validate()?;
    Ok(manifest)
}

pub fn ingest(manifest: &RunManifest) -> Result<Graph, SessionError> {
    let mut mappers = MappingRegistry::default();
    if let Some(root) = &manifest.source_root {
        mappers.register(WorkspaceLocationResolver::new(
            root,
            manifest.source_revision.clone(),
        )?)?;
    }
    ingest_with(manifest, &builtin_importers(), &mappers)
}

pub fn ingest_with(
    manifest: &RunManifest,
    importers: &ImporterRegistry,
    mappers: &MappingRegistry,
) -> Result<Graph, SessionError> {
    manifest.validate()?;
    let mut observations: Vec<Observation> = Vec::new();
    for input in &manifest.inputs {
        observations.extend(importers.import(input)?);
    }
    if observations.is_empty() {
        return Err(SessionError::Invalid(
            "all profiling inputs were empty; no observations to correlate".into(),
        ));
    }
    let mut graph = Graph::build(observations)?;
    let mut attributions = Vec::new();
    for activity in &graph.activities {
        attributions.extend(mappers.map(SourceTarget::Activity(activity))?);
    }
    for sample in &graph.cpu_samples {
        attributions.extend(mappers.map(SourceTarget::CpuSample(sample))?);
    }
    for attribution in attributions {
        graph.add_source_attribution(attribution)?;
    }
    Ok(graph)
}

/// Capture one process with perf and convert its samples to an importable
/// script. Applications may emit OTel or dial9 artifacts in parallel.
pub fn capture_perf(
    request: &CaptureRequest,
    runner: &dyn CommandRunner,
) -> Result<RunManifest, SessionError> {
    if request.pid == 0 || request.seconds == 0 || request.seconds > 3600 {
        return Err(SessionError::Invalid(
            "pid must be positive and seconds must be between 1 and 3600".into(),
        ));
    }
    if request.output_dir.exists() {
        return Err(SessionError::Invalid(format!(
            "output directory already exists: {}",
            request.output_dir.display()
        )));
    }
    fs::create_dir_all(&request.output_dir).map_err(|source| SessionError::Io {
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
    fs::write(&script, output.stdout).map_err(|source| SessionError::Io {
        path: script.clone(),
        source,
    })?;
    let captured = builtin_importers().import(&InputSpec {
        format: "perf_script".into(),
        path: script.clone(),
        clock: "monotonic".into(),
    })?;
    if captured.is_empty() {
        return Err(SessionError::Invalid(format!(
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
        serde_json::to_vec_pretty(&manifest).map_err(|e| SessionError::Invalid(e.to_string()))?;
    fs::write(&manifest_path, serialized).map_err(|source| SessionError::Io {
        path: manifest_path,
        source,
    })?;
    Ok(manifest)
}
