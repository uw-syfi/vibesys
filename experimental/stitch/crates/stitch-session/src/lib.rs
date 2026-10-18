//! Manifest validation and import orchestration through supplied plugins.
#![forbid(unsafe_code)]

use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};
use stitch_core::{Graph, GraphError, Observation};
use stitch_importer_api::{ImportError, ImporterRegistry, InputSpec};
use stitch_source_api::{MappingError, MappingRegistry, SourceTarget};
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
    /// Validate the manifest's schema and required fields.
    ///
    /// # Errors
    ///
    /// Returns an error for unsupported schemas or empty required fields.
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

#[derive(Debug, Error)]
pub enum SessionError {
    #[error("invalid session: {0}")]
    Invalid(String),
    #[error("I/O error at {path}: {source}")]
    Io {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error(transparent)]
    Import(#[from] ImportError),
    #[error(transparent)]
    Graph(#[from] GraphError),
    #[error(transparent)]
    Mapping(#[from] MappingError),
}

/// Read, resolve paths in, and validate a session manifest.
///
/// # Errors
///
/// Returns an error if the file cannot be read, parsed, or validated.
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

/// Correlate imported observations and resolve available source locations.
///
/// # Errors
///
/// Returns an error for invalid input, importer or mapper failures, or invalid graph evidence.
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
