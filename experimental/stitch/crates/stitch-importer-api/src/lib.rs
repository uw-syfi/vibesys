//! Profiler artifact importer contract and registry. Implementations live in plugin crates.
#![forbid(unsafe_code)]

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::PathBuf;
use stitch_core::Observation;
use thiserror::Error;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InputSpec {
    pub format: String,
    pub path: PathBuf,
    pub clock: String,
}

#[derive(Debug, Error)]
pub enum ImportError {
    #[error("cannot read {path}: {source}")]
    Io {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("invalid {format} artifact {path}: {detail}")]
    Invalid {
        format: &'static str,
        path: PathBuf,
        detail: String,
    },
    #[error("unknown importer {0}")]
    UnknownFormat(String),
    #[error("duplicate importer registration {0}")]
    DuplicateFormat(String),
}

/// Narrow role interface: an importer translates one artifact format and does
/// not control the capture lifecycle.
pub trait Importer {
    fn format(&self) -> &'static str;
    /// Import observations from one profiler artifact.
    ///
    /// # Errors
    ///
    /// Returns an error if the artifact cannot be read or parsed.
    fn import(&self, input: &InputSpec) -> Result<Vec<Observation>, ImportError>;
}

#[derive(Default)]
pub struct ImporterRegistry {
    importers: BTreeMap<&'static str, Box<dyn Importer>>,
}

impl ImporterRegistry {
    /// Register one importer for its advertised format.
    ///
    /// # Errors
    ///
    /// Returns an error if that format is already registered.
    pub fn register(&mut self, importer: impl Importer + 'static) -> Result<(), ImportError> {
        let format = importer.format();
        if self.importers.contains_key(format) {
            return Err(ImportError::DuplicateFormat(format.into()));
        }
        self.importers.insert(format, Box::new(importer));
        Ok(())
    }

    /// Dispatch an artifact to its registered importer.
    ///
    /// # Errors
    ///
    /// Returns an error for an unknown format or an importer failure.
    pub fn import(&self, input: &InputSpec) -> Result<Vec<Observation>, ImportError> {
        let importer = self
            .importers
            .get(input.format.as_str())
            .ok_or_else(|| ImportError::UnknownFormat(input.format.clone()))?;
        importer.import(input)
    }
}
