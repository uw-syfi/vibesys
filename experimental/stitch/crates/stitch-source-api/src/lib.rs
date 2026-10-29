//! Source attribution interfaces, separate from profiler formats.
#![forbid(unsafe_code)]

use std::path::PathBuf;
use stitch_core::{Activity, CpuSample, SourceAttribution};
use thiserror::Error;

#[derive(Clone, Debug)]
pub struct DeclaredLocation<'a> {
    pub target_id: &'a str,
    pub path: &'a str,
    pub line: u32,
}

#[derive(Debug, Error)]
pub enum MappingError {
    #[error("cannot open source root {path}: {source}")]
    Root {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("source path {0} is outside the configured source root")]
    OutsideRoot(PathBuf),
    #[error("duplicate source mapper registration {0}")]
    DuplicateMapper(String),
}

/// Source mappers may contribute zero or more claims for an evidence target. A claim
/// must name its basis and resolver so callers can distinguish declared
/// locations from debug-info or generated-source mappings.
pub trait SourceMapper {
    fn name(&self) -> &'static str;
    /// Map an observation to source claims.
    ///
    /// # Errors
    ///
    /// Returns a mapper error when source resolution fails.
    fn map(&self, target: SourceTarget<'_>) -> Result<Vec<SourceAttribution>, MappingError>;
}

#[derive(Clone, Copy)]
pub enum SourceTarget<'a> {
    Activity(&'a Activity),
    CpuSample(&'a CpuSample),
}

#[derive(Default)]
pub struct MappingRegistry {
    mappers: Vec<Box<dyn SourceMapper>>,
}

impl MappingRegistry {
    /// Register a source mapper.
    ///
    /// # Errors
    ///
    /// Returns an error for a duplicate mapper name.
    pub fn register(&mut self, mapper: impl SourceMapper + 'static) -> Result<(), MappingError> {
        if self.mappers.iter().any(|item| item.name() == mapper.name()) {
            return Err(MappingError::DuplicateMapper(mapper.name().into()));
        }
        self.mappers.push(Box::new(mapper));
        Ok(())
    }

    /// Gather claims from registered source mappers.
    ///
    /// # Errors
    ///
    /// Returns the first mapper error.
    pub fn map(&self, target: SourceTarget<'_>) -> Result<Vec<SourceAttribution>, MappingError> {
        let mut claims = Vec::new();
        for mapper in &self.mappers {
            claims.extend(mapper.map(target)?);
        }
        Ok(claims)
    }
}
