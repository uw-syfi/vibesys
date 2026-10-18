#![forbid(unsafe_code)]

use clap::{Parser, Subcommand, ValueEnum};
use std::path::PathBuf;
use stitch_core::Graph;
use stitch_importer_api::ImporterRegistry;
use stitch_profiler_dial9::{Dial9BridgeImporter, Dial9NativeImporter};
use stitch_profiler_gpu_bridge::GpuBridgeImporter;
use stitch_profiler_otel::{OtlpJsonImporter, OtlpJsonlImporter};
use stitch_profiler_perf::{
    capture_perf, CaptureRequest, CaptureStackMode, PerfScriptImporter, SystemCommandRunner,
};
use stitch_session::{ingest_with, load_manifest, RunManifest};
use stitch_source_api::MappingRegistry;
use stitch_source_workspace::WorkspaceLocationResolver;

#[derive(Clone, Copy, ValueEnum)]
enum StackMode {
    Dwarf,
    FramePointer,
}

#[derive(Parser)]
#[command(name = "stitch", about = "Experimental heterogeneous profiling CLI")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Check the selected artifacts and report capture requirements.
    Plan { manifest: PathBuf },
    /// Profile a running Linux process with perf.
    Capture {
        #[arg(long)]
        pid: u32,
        #[arg(long)]
        seconds: u32,
        #[arg(long)]
        out: PathBuf,
        #[arg(long, value_enum, default_value_t = StackMode::Dwarf)]
        stack_mode: StackMode,
    },
    /// Import profiler artifacts and build the evidence graph.
    Ingest {
        manifest: PathBuf,
        #[arg(long)]
        out: PathBuf,
    },
    /// Explain one trace using an ingested graph.
    Explain { graph: PathBuf, trace_id: String },
    /// Show a CPU sample cost tree for an ingested graph.
    Costs { graph: PathBuf },
}

/// The CLI selects its built-in plugins; the session and core do not depend on them.
fn plugins(
    manifest: &RunManifest,
) -> Result<(ImporterRegistry, MappingRegistry), Box<dyn std::error::Error>> {
    let mut importers = ImporterRegistry::default();
    importers.register(OtlpJsonImporter)?;
    importers.register(OtlpJsonlImporter)?;
    importers.register(PerfScriptImporter)?;
    importers.register(Dial9BridgeImporter)?;
    importers.register(Dial9NativeImporter)?;
    importers.register(GpuBridgeImporter)?;
    let mut mappers = MappingRegistry::default();
    if let Some(root) = &manifest.source_root {
        mappers.register(WorkspaceLocationResolver::new(
            root,
            manifest.source_revision.clone(),
        )?)?;
    }
    Ok((importers, mappers))
}

fn run(cli: Cli) -> Result<(), Box<dyn std::error::Error>> {
    match cli.command {
        Commands::Plan { manifest } => {
            let manifest = load_manifest(&manifest)?;
            println!("Session: {}", manifest.label);
            for input in &manifest.inputs {
                println!(
                    "{:?}: {} ({}) {}",
                    input.format,
                    input.path.display(),
                    input.clock,
                    if input.path.is_file() {
                        "ready"
                    } else {
                        "missing"
                    }
                );
            }
            println!("OTel requires exported spans and propagated trace context.");
            println!(
                "Tokio task linkage requires a dial9 bridge export with trace and parent span IDs."
            );
            println!(
                "CPU sample to poll linkage requires the same process, thread, and clock domain."
            );
        }
        Commands::Capture {
            pid,
            seconds,
            out,
            stack_mode,
        } => {
            let request = CaptureRequest {
                pid,
                seconds,
                output_dir: out.clone(),
                stack_mode: match stack_mode {
                    StackMode::Dwarf => CaptureStackMode::Dwarf,
                    StackMode::FramePointer => CaptureStackMode::FramePointer,
                },
            };
            capture_perf(&request, &SystemCommandRunner)?;
            println!("Captured perf artifacts in {}", out.display());
        }
        Commands::Ingest { manifest, out } => {
            let manifest = load_manifest(&manifest)?;
            let (importers, mappers) = plugins(&manifest)?;
            let graph = ingest_with(&manifest, &importers, &mappers)?;
            std::fs::write(&out, serde_json::to_vec_pretty(&graph)?)?;
            println!(
                "Wrote {} activities, {} CPU samples, {} relationships to {}",
                graph.activities.len(),
                graph.cpu_samples.len(),
                graph.relationships.len(),
                out.display()
            );
        }
        Commands::Explain { graph, trace_id } => {
            let graph: Graph = serde_json::from_slice(&std::fs::read(graph)?)?;
            let summary = graph.trace(&trace_id);
            println!("{}", serde_json::to_string_pretty(&summary)?);
        }
        Commands::Costs { graph } => {
            let graph: Graph = serde_json::from_slice(&std::fs::read(graph)?)?;
            println!(
                "{}",
                serde_json::to_string_pretty(&graph.sample_cost_tree())?
            );
        }
    }
    Ok(())
}

fn main() {
    if let Err(error) = run(Cli::parse()) {
        eprintln!("stitch: {error}");
        std::process::exit(1);
    }
}
