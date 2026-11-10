#![forbid(unsafe_code)]

use clap::{Args, Parser, Subcommand, ValueEnum};
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::time::Duration;
use stitch_analysis::{analyze, render, RenderOptions, WindowSelection};
use stitch_core::{Graph, TimeWindow};
use stitch_importer_api::ImporterRegistry;
use stitch_profiler_dial9::{Dial9BridgeImporter, Dial9NativeImporter};
use stitch_profiler_gpu_bridge::GpuBridgeImporter;
use stitch_profiler_otel::k8s::{self, SystemKubectl};
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
    /// Profile a running Linux process with perf, or capture with a named
    /// capture plugin (`stitch capture otel-k8s ...`).
    #[command(args_conflicts_with_subcommands = true, subcommand_negates_reqs = true)]
    Capture {
        #[command(flatten)]
        perf: PerfCapture,
        #[command(subcommand)]
        plugin: Option<CapturePlugin>,
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
    /// Render trace analysis of an ingested graph as text and JSON files.
    Render {
        graph: PathBuf,
        #[arg(long)]
        out: PathBuf,
        /// A window label (or window-N for an unlabeled window), or all.
        #[arg(long, default_value = "all")]
        window: String,
        /// Rows per latency table.
        #[arg(long, default_value_t = 20)]
        top: usize,
        /// Root endpoints per text file.
        #[arg(long, default_value_t = 10)]
        max_roots: usize,
        /// Call-graph nodes, critical-path rows and segments per root.
        #[arg(long, default_value_t = 30)]
        max_nodes: usize,
        /// Characters in a waterfall bar.
        #[arg(long, default_value_t = 48)]
        width: usize,
    },
}

#[derive(Args)]
struct PerfCapture {
    #[arg(long, required = true)]
    pid: Option<u32>,
    #[arg(long, required = true)]
    seconds: Option<u32>,
    #[arg(long, required = true)]
    out: Option<PathBuf>,
    #[arg(long, value_enum, default_value_t = StackMode::Dwarf)]
    stack_mode: StackMode,
}

#[derive(Subcommand)]
enum CapturePlugin {
    /// OpenTelemetry traces from Deployments on Kubernetes, through an
    /// in-namespace OpenTelemetry Collector.
    #[command(name = "otel-k8s", subcommand)]
    OtelK8s(OtelK8s),
}

#[derive(Subcommand)]
enum OtelK8s {
    /// Deploy the collector and point the named Deployments at it; prints JSON.
    Start {
        #[arg(long)]
        namespace: String,
        /// A Deployment to instrument; repeat for each one.
        #[arg(long = "deployment", required = true)]
        deployments: Vec<String>,
        #[arg(long, default_value_t = 1.0)]
        sampler_ratio: f64,
        #[arg(long, default_value = k8s::DEFAULT_COLLECTOR_IMAGE)]
        collector_image: String,
        #[arg(long, default_value = k8s::DEFAULT_SIDECAR_IMAGE)]
        sidecar_image: String,
        #[arg(long, default_value_t = 600)]
        timeout_seconds: u64,
        #[arg(long, default_value = "kubectl")]
        kubectl: PathBuf,
    },
    /// Read the collected spans into DIR/otel.jsonl and DIR/manifest.json.
    Collect {
        #[arg(long)]
        namespace: String,
        #[arg(long)]
        out: PathBuf,
        #[arg(long, default_value_t = 5)]
        settle_seconds: u64,
        /// A measurement window as START:END[:LABEL] in Unix nanoseconds; repeat
        /// for each window.
        #[arg(long = "window", value_parser = parse_window)]
        windows: Vec<TimeWindow>,
        #[arg(long)]
        label: Option<String>,
        #[arg(long)]
        source_root: Option<PathBuf>,
        #[arg(long, default_value = "kubectl")]
        kubectl: PathBuf,
    },
}

fn parse_window(raw: &str) -> Result<TimeWindow, String> {
    let mut parts = raw.splitn(3, ':');
    let mut bound = |name: &str| {
        parts
            .next()
            .and_then(|value| value.parse::<u64>().ok())
            .ok_or_else(|| format!("{name} must be Unix nanoseconds in START:END[:LABEL]"))
    };
    let start_unix_nanos = bound("START")?;
    let end_unix_nanos = bound("END")?;
    let label = parts.next().map(str::to_owned);
    Ok(TimeWindow {
        start_unix_nanos,
        end_unix_nanos,
        label,
    })
}

fn run_otel_k8s(command: OtelK8s) -> Result<(), Box<dyn std::error::Error>> {
    match command {
        OtelK8s::Start {
            namespace,
            deployments,
            sampler_ratio,
            collector_image,
            sidecar_image,
            timeout_seconds,
            kubectl,
        } => {
            let report = k8s::start(
                &k8s::StartRequest {
                    namespace,
                    deployments,
                    sampler_ratio,
                    collector_image,
                    sidecar_image,
                    timeout: Duration::from_secs(timeout_seconds),
                },
                &SystemKubectl::new(kubectl),
            )?;
            println!("{}", serde_json::to_string(&report)?);
        }
        OtelK8s::Collect {
            namespace,
            out,
            settle_seconds,
            windows,
            label,
            source_root,
            kubectl,
        } => {
            let summary = k8s::collect(
                &k8s::CollectRequest {
                    namespace,
                    output_dir: out.clone(),
                    settle: Duration::from_secs(settle_seconds),
                    windows,
                    label,
                    source_root,
                },
                &SystemKubectl::new(kubectl),
            )?;
            if summary.dropped_partial_line {
                eprintln!("stitch: dropped an incomplete final line still being written");
            }
            println!(
                "Collected {} spans in {} traces from {} services ({}) into {}",
                summary.spans,
                summary.traces,
                summary.services.len(),
                summary.services.join(", "),
                out.display()
            );
        }
    }
    Ok(())
}

fn run_render(
    graph: &Path,
    out: &Path,
    window: String,
    options: &RenderOptions,
) -> Result<(), Box<dyn std::error::Error>> {
    let graph: Graph = serde_json::from_slice(&std::fs::read(graph)?)?;
    let selection = if window == "all" {
        WindowSelection::All
    } else {
        WindowSelection::Label(window)
    };
    let analysis = analyze(&graph, &selection)?;
    let files = render(&analysis, options)?;
    std::fs::create_dir_all(out)?;
    for file in files {
        std::fs::write(out.join(file.name), file.contents)?;
    }
    println!(
        "Rendered {} root endpoints from {} of {} traces ({} spans) to {}",
        analysis.roots.len(),
        analysis.quality.eligible_traces,
        analysis.quality.captured_traces,
        analysis.report.span_count,
        out.display()
    );
    Ok(())
}

fn write_graph(graph: &Graph, out: &Path) -> Result<(), Box<dyn std::error::Error>> {
    let mut writer = std::io::BufWriter::new(std::fs::File::create(out)?);
    serde_json::to_writer(&mut writer, graph)?;
    writer.flush()?;
    Ok(())
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
            plugin: Some(CapturePlugin::OtelK8s(command)),
            ..
        } => run_otel_k8s(command)?,
        Commands::Capture {
            perf:
                PerfCapture {
                    pid: Some(pid),
                    seconds: Some(seconds),
                    out: Some(out),
                    stack_mode,
                },
            plugin: None,
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
        Commands::Capture { .. } => {
            return Err("capture needs --pid, --seconds and --out, or a capture plugin".into());
        }
        Commands::Ingest { manifest, out } => {
            let manifest = load_manifest(&manifest)?;
            let (importers, mappers) = plugins(&manifest)?;
            let graph = ingest_with(&manifest, &importers, &mappers)?;
            write_graph(&graph, &out)?;
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
        Commands::Render {
            graph,
            out,
            window,
            top,
            max_roots,
            max_nodes,
            width,
        } => run_render(
            &graph,
            &out,
            window,
            &RenderOptions {
                top,
                max_roots,
                max_nodes,
                width,
            },
        )?,
    }
    Ok(())
}

fn main() {
    if let Err(error) = run(Cli::parse()) {
        eprintln!("stitch: {error}");
        std::process::exit(1);
    }
}
