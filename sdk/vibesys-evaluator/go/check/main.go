package check

import (
	"flag"
	"fmt"
	"io"
	"os"
	"time"
)

// DefaultReportDir is where Main writes the report and timelines unless --out says otherwise.
const DefaultReportDir = "/vibesys/artifacts/accuracy"

// Main is the command line of a task's oracle: it checks a captured history directory (see
// OpenHistory) against the specs oracle builds for it, prints the report, writes it and the
// failures' timelines to --out, and exits 0 on PASS, 1 on FAIL and 2 on UNKNOWN, with the
// verdict's one-paragraph reason on standard error when it is not PASS.
//
//	ORACLE --history DIR [--out DIR] [--partition-timeout D] [--total-timeout D]
//	       [--max-search-steps N] [--memory-mb N] [--workers N] [--max-reads-per-key N]
//	       [--max-ops-per-partition N] [--max-lost-share F] [--seed N]
//
// The flags' defaults are DefaultOptions. oracle gets the opened history, so the specs can read
// its dataset (History.Dataset); an error from it is an UNKNOWN verdict.
func Main(oracle func(h *History) ([]AnySpec, error)) {
	os.Exit(run(os.Args[1:], oracle, os.Stdout, os.Stderr))
}

func run(args []string, oracle func(h *History) ([]AnySpec, error), stdout, stderr io.Writer) int {
	d := DefaultOptions
	fs := flag.NewFlagSet("oracle", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dir := fs.String("history", "", "the captured history directory (required)")
	out := fs.String("out", DefaultReportDir, "where to write report.txt and the failures' timelines")
	var o Options
	fs.DurationVar(&o.PartitionTimeout, "partition-timeout", d.PartitionTimeout, "one partition's search budget")
	fs.DurationVar(&o.TotalTimeout, "total-timeout", d.TotalTimeout, "the whole check's search budget")
	fs.Int64Var(&o.MaxSearchSteps, "max-search-steps", d.MaxSearchSteps, "one partition's search budget in model steps (negative: none)")
	memMB := fs.Int64("memory-mb", d.MemoryBytes>>20, "the checker's heap budget, MiB")
	fs.IntVar(&o.Workers, "workers", 0, "partitions searched at once (0: GOMAXPROCS)")
	fs.IntVar(&o.MaxReadsPerKey, "max-reads-per-key", d.MaxReadsPerKey, "reads kept per key (negative: all)")
	fs.IntVar(&o.MaxOpsPerPartition, "max-ops-per-partition", d.MaxOpsPerPartition, "larger partitions are not searched")
	fs.Float64Var(&o.MaxLostShare, "max-lost-share", d.MaxLostShare, "the largest share of unknown outcomes that passes (negative: no limit)")
	fs.Int64Var(&o.Seed, "seed", d.Seed, "the read sample's seed")
	if err := fs.Parse(args); err != nil {
		fmt.Fprintf(stderr, "UNKNOWN: %v\n", err)
		return Unknown.ExitCode()
	}
	if *dir == "" || fs.NArg() > 0 {
		fmt.Fprintln(stderr, "UNKNOWN: usage: --history DIR is required and nothing else is positional")
		return Unknown.ExitCode()
	}
	o.MemoryBytes = *memMB << 20
	began := time.Now()
	h, err := OpenHistory(*dir)
	if err != nil {
		fmt.Fprintf(stderr, "UNKNOWN: the history cannot be read (a capture problem, not the candidate's): %v\n", err)
		return Unknown.ExitCode()
	}
	specs, err := oracle(h)
	if err != nil {
		fmt.Fprintf(stderr, "UNKNOWN: the oracle cannot be set up (a task problem, not the candidate's): %v\n", err)
		return Unknown.ExitCode()
	}
	report := Check(h, specs, o)
	fmt.Fprint(stdout, report.Text(40))
	paths, err := report.WriteFiles(*out)
	if err != nil {
		fmt.Fprintf(stderr, "writing the report to %s: %v\n", *out, err)
	}
	fmt.Fprintf(stdout, "report: %v (%v)\n", paths, time.Since(began).Round(time.Millisecond))
	if report.Verdict != Pass {
		fmt.Fprintln(stderr, report.Reason)
	}
	return report.Verdict.ExitCode()
}
