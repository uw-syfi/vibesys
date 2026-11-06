package microservices

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/evaluator"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/phase"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/workload"
)

// Task is everything a microservices task script passes to Main: the Go form of Python's
// microservices.main. D is the dataset's type and M the workload's model (any type when the
// workload is another program).
type Task[D, M any] struct {
	// Metrics the benchmark reports, by name.
	Metrics map[string]evaluator.Metric
	// Validity is when a phase is a measurement; evaluator.DefaultValidity() when nil.
	Validity *evaluator.Validity
	// Dataset builds the initial data, a pure function of the task's constants: every process of
	// a sample builds it (the orchestrator, the seed, each phase), and the seed refuses one that
	// differs from the orchestrator's. It is written as JSON (encoding/json) into dataset.json in
	// the artifacts and for the generator (--dataset). Nil: D's zero value.
	Dataset func() (D, error)
	// System is where the application runs: Local, or a Kubernetes namespace (package k8s).
	System System
	// Seed writes the dataset into the deployed application before the phases, from a process
	// next to it (a seed pod); nil when there is nothing to write.
	Seed func(ctx context.Context, data D) error
	// Workload is the request generator, in this binary (`generate`); or Generator is its
	// command, another program built on package workload, in the task image. Exactly one is set.
	// A relative Generator program with a slash is taken from this binary's directory.
	Workload  *workload.Workload[D, M]
	Generator []string
	// Benchmark is the scoring definition: it runs phases on run and calls run.Report. An error
	// ends the sample with an error record.
	Benchmark func(ctx context.Context, run *Run) error
	// Capture records every phase's whole history for an accuracy check, which benchmarks leave
	// off: every response body, the loadgen client of every operation and the generator's info
	// of every operation are kept in the phase directories, and history.binpb lists the phases
	// and the faults (Run.Fault). See docs/spec.md, "Accuracy history".
	Capture bool
}

const usageText = `usage:
  TASK benchmark [--project-root DIR] --vs-output FILE [--seed N]   one sample (vibesys)
  TASK phase --work DIR --target URL                              one phase (a phase pod)
  TASK generate --phase-input F --dataset F --state DIR          the generator (under loadgen)
  TASK seed --dataset-digest HEX                                  the seed (a seed pod)
`

// Main runs the command in os.Args and exits: `benchmark` is one sample (the core's benchmark,
// see evaluator.Main), whose body deploys the application on the Task's System, runs Benchmark
// on a Run and tears the application down on every path; `phase` is the phase pod's command;
// `generate` the generator's (with Task.Workload); `seed` the seed pod's.
func Main[D, M any](task Task[D, M]) {
	if err := task.validate(); err != nil {
		fmt.Fprintln(os.Stderr, "bench: error:", err)
		os.Exit(2)
	}
	args := os.Args[1:]
	if len(args) == 0 {
		usage("a command is required")
	}
	switch args[0] {
	case "benchmark":
		root, rest, err := cutProjectRoot(args[1:])
		if err != nil {
			usage("%v", err)
		}
		os.Args = append([]string{os.Args[0], "benchmark"}, rest...)
		evaluator.Main(evaluator.Task{
			Metrics:   task.Metrics,
			Benchmark: func(ctx context.Context, run *evaluator.Run) error { return sample(ctx, task, run, root) },
		})
	case "phase":
		os.Exit(withSignals(func(ctx context.Context) int { return podCommand(ctx, task, args[1:]) }))
	case "generate":
		if task.Workload == nil {
			usage("the workload is the generator %q: run it instead", task.Generator)
		}
		w := *task.Workload
		if w.Dataset == nil {
			w.Dataset = func(string) (D, error) { return dataset(task) }
		}
		workload.Main(w)
	case "seed":
		os.Exit(withSignals(func(ctx context.Context) int { return seedCommand(ctx, task, args[1:]) }))
	default:
		usage("unknown command %q", args[0])
	}
}

func usage(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "bench: error: "+format+"\n"+usageText, args...)
	os.Exit(2)
}

func (t Task[D, M]) validate() error {
	if t.Benchmark == nil {
		return errors.New("microservices.Task needs a Benchmark")
	}
	if t.System == nil {
		return errors.New("microservices.Task needs a System (microservices.Local, or k8s)")
	}
	if (t.Workload == nil) == (len(t.Generator) == 0) {
		return errors.New("microservices.Task needs exactly one of Workload and Generator")
	}
	if t.Validity != nil {
		if err := t.Validity.Validate(); err != nil {
			return err
		}
	}
	return nil
}

func (t Task[D, M]) validity() evaluator.Validity {
	if t.Validity != nil {
		return *t.Validity
	}
	return evaluator.DefaultValidity()
}

// cutProjectRoot takes --project-root out of a benchmark's flags.
func cutProjectRoot(args []string) (string, []string, error) {
	var root string
	var rest []string
	for i := 0; i < len(args); i++ {
		switch {
		case args[i] == "--project-root":
			if i+1 >= len(args) {
				return "", nil, errors.New("argument --project-root: expected one argument")
			}
			root = args[i+1]
			i++
		case strings.HasPrefix(args[i], "--project-root="):
			root = strings.TrimPrefix(args[i], "--project-root=")
		default:
			rest = append(rest, args[i])
		}
	}
	return root, rest, nil
}

// withSignals runs f with a context the first SIGTERM, SIGINT or SIGHUP cancels.
func withSignals(f func(ctx context.Context) int) int {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT, syscall.SIGHUP)
	defer stop()
	return f(ctx)
}

func dataset[D, M any](task Task[D, M]) (D, error) {
	var data D
	if task.Dataset == nil {
		return data, nil
	}
	data, err := task.Dataset()
	if err != nil {
		return data, fmt.Errorf("Task Dataset: %w", err)
	}
	return data, nil
}

// encodeDataset is the dataset file's bytes and their digest (SHA-256, hex).
func encodeDataset[D, M any](task Task[D, M]) ([]byte, string, error) {
	data, err := dataset(task)
	if err != nil {
		return nil, "", err
	}
	encoded, err := json.Marshal(data)
	if err != nil {
		return nil, "", fmt.Errorf("the dataset is not JSON: %w", err)
	}
	encoded = append(encoded, '\n')
	sum := sha256.Sum256(encoded)
	return encoded, hex.EncodeToString(sum[:]), nil
}

func executable() (string, error) {
	exe, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("this binary's path: %w", err)
	}
	return exe, nil
}

// sample is a benchmark sample's body: dataset, deploy, Benchmark, teardown.
func sample[D, M any](ctx context.Context, task Task[D, M], run *evaluator.Run, projectRoot string) error {
	exe, err := executable()
	if err != nil {
		return err
	}
	encoded, digest, err := encodeDataset(task)
	if err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(run.Artifacts(), phase.DatasetFile), encoded, 0o644); err != nil {
		return err
	}
	evaluator.Log("dataset %s (%d bytes)", digest[:16], len(encoded))
	logf := func(line string) { evaluator.Log("%s", line) }
	var seed []string
	if task.Seed != nil {
		seed = []string{exe, "seed", "--dataset-digest", digest}
	}
	deployed, err := task.System.deploy(ctx, deployRequest{projectRoot: projectRoot, artifacts: run.Artifacts(), seed: seed, log: logf})
	if err != nil {
		var sampleErr *evaluator.SampleError
		if errors.As(err, &sampleErr) || ctx.Err() != nil {
			return err
		}
		return &evaluator.SampleError{Msg: "deploy: " + err.Error()}
	}
	defer func() {
		if err := deployed.close(context.WithoutCancel(ctx)); err != nil {
			evaluator.Log("teardown: %v", err)
		}
	}()
	r := &Run{Run: run, phases: &phases{
		pods:      deployed,
		command:   func(work, target string) []string { return []string{exe, "phase", "--work", work, "--target", target} },
		seed:      run.Seed(),
		artifacts: run.Artifacts(),
		validity:  task.validity(),
		capture:   task.Capture,
		log:       logf,
		now:       time.Now,
	}}
	return task.Benchmark(ctx, r)
}

// generatorCommand is the generator as the phase pod runs it.
func generatorCommand[D, M any](task Task[D, M], exe string) []string {
	if task.Workload != nil {
		return []string{exe}
	}
	program := task.Generator[0]
	if strings.Contains(program, "/") && !filepath.IsAbs(program) {
		program = filepath.Join(filepath.Dir(exe), program)
	}
	return append([]string{program}, task.Generator[1:]...)
}

// podCommand is `phase --work DIR --target URL`: one phase, here.
func podCommand[D, M any](ctx context.Context, task Task[D, M], args []string) int {
	set := flag.NewFlagSet("phase", flag.ContinueOnError)
	set.SetOutput(io.Discard)
	work := set.String("work", "", "the phase's work directory")
	target := set.String("target", "", "the application's base URL")
	if err := set.Parse(args); err != nil || *work == "" || *target == "" || set.NArg() > 0 {
		usage("phase needs --work DIR --target URL")
	}
	exe, err := executable()
	if err != nil {
		evaluator.Log("phase failed: %v", err)
		return 1
	}
	encoded, _, err := encodeDataset(task)
	if err != nil {
		evaluator.Log("phase failed: %v", err)
		return 1
	}
	scratch, err := os.MkdirTemp("", "vse-dataset-")
	if err != nil {
		evaluator.Log("phase failed: %v", err)
		return 1
	}
	defer os.RemoveAll(scratch)
	datasetFile := filepath.Join(scratch, phase.DatasetFile)
	if err := os.WriteFile(datasetFile, encoded, 0o644); err != nil {
		evaluator.Log("phase failed: %v", err)
		return 1
	}
	pod := phase.Pod{
		WorkDir: *work, Target: *target, Generator: generatorCommand(task, exe), DatasetFile: datasetFile,
		MinSustainedRatio: task.validity().MinSustainedRatio, Stderr: os.Stderr,
		Event: func(_, message string) { evaluator.Log("%s", message) },
	}
	if _, err := pod.Run(ctx); err != nil {
		evaluator.Log("phase failed: %v", err)
		return 1
	}
	return 0
}

// seedCommand is `seed --dataset-digest HEX`: the dataset, checked against the orchestrator's,
// written into the application.
func seedCommand[D, M any](ctx context.Context, task Task[D, M], args []string) int {
	set := flag.NewFlagSet("seed", flag.ContinueOnError)
	set.SetOutput(io.Discard)
	expected := set.String("dataset-digest", "", "the orchestrator's dataset digest")
	if err := set.Parse(args); err != nil || *expected == "" || set.NArg() > 0 {
		usage("seed needs --dataset-digest HEX")
	}
	_, digest, err := encodeDataset(task)
	if err != nil {
		evaluator.Log("seed failed: %v", err)
		return 1
	}
	if digest != *expected {
		evaluator.Log("the dataset here is %s, the orchestrator's %s: Dataset must be a pure function of the task's constants",
			digest[:16], short(*expected))
		return 1
	}
	if task.Seed == nil {
		evaluator.Log("no Seed: nothing to write")
		return 0
	}
	data, err := dataset(task)
	if err != nil {
		evaluator.Log("seed failed: %v", err)
		return 1
	}
	if err := task.Seed(ctx, data); err != nil {
		evaluator.Log("seed failed: %v", err)
		return 1
	}
	evaluator.Log("seeded dataset %s", digest[:16])
	return 0
}

func short(digest string) string {
	if len(digest) > 16 {
		return digest[:16]
	}
	return digest
}
