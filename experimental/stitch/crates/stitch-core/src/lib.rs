//! Pure, source-independent profiling evidence and correlation.
#![forbid(unsafe_code)]

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use thiserror::Error;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ActivityKind {
    Span,
    TaskPoll,
    CpuRegion,
    GpuKernel,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Interval {
    pub start_ns: u64,
    pub end_ns: u64,
    /// Unaligned clocks remain distinct. Timestamp overlap is meaningful only
    /// when both observations name the same clock domain.
    pub clock: String,
}

impl Interval {
    #[must_use]
    pub fn contains(&self, instant_ns: u64, clock: &str) -> bool {
        self.clock == clock && self.start_ns <= instant_ns && instant_ns < self.end_ns
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Origin {
    pub source: String,
    pub artifact: String,
    pub record: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Activity {
    pub id: String,
    pub kind: ActivityKind,
    pub name: String,
    pub interval: Interval,
    pub service: Option<String>,
    pub trace_id: Option<String>,
    pub span_id: Option<String>,
    pub parent_span_id: Option<String>,
    pub pid: Option<u32>,
    pub tid: Option<u32>,
    pub source_file: Option<String>,
    pub source_line: Option<u32>,
    pub origin: Origin,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct CpuSample {
    pub id: String,
    pub pid: u32,
    pub tid: u32,
    pub at_ns: u64,
    pub clock: String,
    pub frames: Vec<String>,
    pub origin: Origin,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Observation {
    Activity(Activity),
    CpuSample(CpuSample),
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RelationshipKind {
    ParentSpan,
    SpanContext,
    SampledDuring,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Relationship {
    pub from: String,
    pub to: String,
    pub kind: RelationshipKind,
    /// How the relationship was established; never inferred from mere overlap
    /// across distinct clock domains.
    pub reason: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct SourceLocation {
    pub path: String,
    pub line: u32,
    pub column: Option<u32>,
    pub function: Option<String>,
    pub build_id: Option<String>,
    pub revision: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SourceBasis {
    DeclaredLocation,
    DebugInfo,
    GeneratedSourceMap,
    RuntimeFrame,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct SourceAttribution {
    pub target_id: String,
    pub location: SourceLocation,
    pub resolver: String,
    pub basis: SourceBasis,
    pub reason: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Graph {
    pub activities: Vec<Activity>,
    pub cpu_samples: Vec<CpuSample>,
    pub relationships: Vec<Relationship>,
    pub source_attributions: Vec<SourceAttribution>,
    pub unassigned_samples: usize,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct SampleCostTree {
    /// The number of observed CPU samples, not elapsed CPU time.
    pub samples: usize,
    pub roots: Vec<SampleCostNode>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct SampleCostNode {
    pub frame: String,
    pub inclusive_samples: usize,
    pub self_samples: usize,
    pub children: Vec<SampleCostNode>,
}

#[derive(Default)]
struct CostNodeAccumulator {
    inclusive_samples: usize,
    self_samples: usize,
    children: BTreeMap<String, CostNodeAccumulator>,
}

fn finish_cost_nodes(nodes: BTreeMap<String, CostNodeAccumulator>) -> Vec<SampleCostNode> {
    let mut finished: Vec<_> = nodes
        .into_iter()
        .map(|(frame, node)| SampleCostNode {
            frame,
            inclusive_samples: node.inclusive_samples,
            self_samples: node.self_samples,
            children: finish_cost_nodes(node.children),
        })
        .collect();
    finished.sort_by(|a, b| {
        b.inclusive_samples
            .cmp(&a.inclusive_samples)
            .then_with(|| a.frame.cmp(&b.frame))
    });
    finished
}

fn add_sample_stack(nodes: &mut BTreeMap<String, CostNodeAccumulator>, frames: &[String]) {
    let Some((frame, rest)) = frames.split_last() else {
        let node = nodes.entry("[unknown]".into()).or_default();
        node.inclusive_samples += 1;
        node.self_samples += 1;
        return;
    };
    let node = nodes.entry(frame.clone()).or_default();
    node.inclusive_samples += 1;
    if rest.is_empty() {
        node.self_samples += 1;
    } else {
        add_sample_stack(&mut node.children, rest);
    }
}

#[derive(Debug, Error)]
pub enum GraphError {
    #[error("duplicate observation id: {0}")]
    DuplicateId(String),
    #[error("invalid interval for activity {0}")]
    InvalidInterval(String),
    #[error("source attribution names unknown evidence target {0}")]
    UnknownTarget(String),
}

impl Graph {
    /// Build a stack tree from all CPU samples. Every sample contributes one
    /// count along its observed stack, with roots nearest the caller.
    #[must_use]
    pub fn sample_cost_tree(&self) -> SampleCostTree {
        let mut roots = BTreeMap::<String, CostNodeAccumulator>::new();
        for sample in &self.cpu_samples {
            add_sample_stack(&mut roots, &sample.frames);
        }
        SampleCostTree {
            samples: self.cpu_samples.len(),
            roots: finish_cost_nodes(roots),
        }
    }

    /// Build a graph from observations.
    ///
    /// # Errors
    ///
    /// Returns an error for duplicate IDs or invalid activity intervals.
    pub fn build(observations: impl IntoIterator<Item = Observation>) -> Result<Self, GraphError> {
        let mut ids = BTreeSet::new();
        let mut activities = Vec::new();
        let mut cpu_samples = Vec::new();
        for observation in observations {
            match observation {
                Observation::Activity(activity) => {
                    if !ids.insert(activity.id.clone()) {
                        return Err(GraphError::DuplicateId(activity.id));
                    }
                    if activity.interval.end_ns <= activity.interval.start_ns {
                        return Err(GraphError::InvalidInterval(activity.id));
                    }
                    activities.push(activity);
                }
                Observation::CpuSample(sample) => {
                    if !ids.insert(sample.id.clone()) {
                        return Err(GraphError::DuplicateId(sample.id));
                    }
                    cpu_samples.push(sample);
                }
            }
        }

        let spans: BTreeMap<_, _> = activities
            .iter()
            .filter(|a| a.kind == ActivityKind::Span)
            .filter_map(|a| Some(((a.trace_id.as_ref()?, a.span_id.as_ref()?), a.id.as_str())))
            .collect();
        let mut relationships = Vec::new();
        for activity in &activities {
            let Some(trace_id) = activity.trace_id.as_ref() else {
                continue;
            };
            if let Some(parent_id) = activity.parent_span_id.as_ref() {
                if let Some(parent) = spans.get(&(trace_id, parent_id)) {
                    relationships.push(Relationship {
                        from: (*parent).to_owned(),
                        to: activity.id.clone(),
                        kind: if activity.kind == ActivityKind::Span {
                            RelationshipKind::ParentSpan
                        } else {
                            RelationshipKind::SpanContext
                        },
                        reason: "matching trace and parent span identifiers".into(),
                    });
                }
            }
        }

        let mut unassigned_samples = 0;
        for sample in &cpu_samples {
            let matching_regions: Vec<_> = activities
                .iter()
                .filter(|a| matches!(a.kind, ActivityKind::TaskPoll | ActivityKind::CpuRegion))
                .filter(|a| a.pid == Some(sample.pid) && a.tid == Some(sample.tid))
                .filter(|a| a.interval.contains(sample.at_ns, &sample.clock))
                .collect();
            if matching_regions.len() == 1 {
                relationships.push(Relationship {
                    from: matching_regions[0].id.clone(),
                    to: sample.id.clone(),
                    kind: RelationshipKind::SampledDuring,
                    reason: "same process, thread, clock, and unique CPU interval".into(),
                });
            } else {
                unassigned_samples += 1;
            }
        }
        Ok(Self {
            activities,
            cpu_samples,
            relationships,
            source_attributions: Vec::new(),
            unassigned_samples,
        })
    }

    /// Add a source claim for an existing observation.
    ///
    /// # Errors
    ///
    /// Returns an error when the target ID is absent from the graph.
    pub fn add_source_attribution(
        &mut self,
        attribution: SourceAttribution,
    ) -> Result<(), GraphError> {
        if !self
            .activities
            .iter()
            .any(|a| a.id == attribution.target_id)
            && !self
                .cpu_samples
                .iter()
                .any(|s| s.id == attribution.target_id)
        {
            return Err(GraphError::UnknownTarget(attribution.target_id));
        }
        self.source_attributions.push(attribution);
        Ok(())
    }

    #[must_use]
    pub fn trace(&self, trace_id: &str) -> TraceSummary {
        let mut ids: BTreeSet<&str> = self
            .activities
            .iter()
            .filter(|a| a.trace_id.as_deref() == Some(trace_id))
            .map(|a| a.id.as_str())
            .collect();
        let mut changed = true;
        while changed {
            changed = false;
            for edge in &self.relationships {
                if ids.contains(edge.from.as_str()) && ids.insert(&edge.to) {
                    changed = true;
                }
            }
        }
        let activities = self
            .activities
            .iter()
            .filter(|a| ids.contains(a.id.as_str()))
            .cloned()
            .collect();
        let mut hot_frames = BTreeMap::<String, usize>::new();
        let mut assigned_samples = 0;
        for sample in &self.cpu_samples {
            if ids.contains(sample.id.as_str()) {
                assigned_samples += 1;
                let frame = sample
                    .frames
                    .first()
                    .cloned()
                    .unwrap_or_else(|| "[unknown]".into());
                *hot_frames.entry(frame).or_default() += 1;
            }
        }
        TraceSummary {
            trace_id: trace_id.to_owned(),
            activities,
            assigned_samples,
            hot_frames,
            source_attributions: self
                .source_attributions
                .iter()
                .filter(|attribution| ids.contains(attribution.target_id.as_str()))
                .cloned()
                .collect(),
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TraceSummary {
    pub trace_id: String,
    pub activities: Vec<Activity>,
    pub assigned_samples: usize,
    pub hot_frames: BTreeMap<String, usize>,
    pub source_attributions: Vec<SourceAttribution>,
}
