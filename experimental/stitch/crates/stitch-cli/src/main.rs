use clap::{Parser, Subcommand};
use std::path::PathBuf;
use stitch_core::Graph;
use stitch_session::{capture_perf, ingest, load_manifest, CaptureRequest, SystemCommandRunner};

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
    },
    /// Import profiler artifacts and build the evidence graph.
    Ingest {
        manifest: PathBuf,
        #[arg(long)]
        out: PathBuf,
    },
    /// Explain one trace using an ingested graph.
    Explain { graph: PathBuf, trace_id: String },
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
        Commands::Capture { pid, seconds, out } => {
            let request = CaptureRequest {
                pid,
                seconds,
                output_dir: out.clone(),
            };
            capture_perf(&request, &SystemCommandRunner)?;
            println!("Captured perf artifacts in {}", out.display());
        }
        Commands::Ingest { manifest, out } => {
            let manifest = load_manifest(&manifest)?;
            let graph = ingest(&manifest)?;
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
    }
    Ok(())
}

fn main() {
    if let Err(error) = run(Cli::parse()) {
        eprintln!("stitch: {error}");
        std::process::exit(1);
    }
}
