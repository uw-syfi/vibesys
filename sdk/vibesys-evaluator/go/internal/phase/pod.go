package phase

import (
	"bytes"
	"compress/gzip"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/files"
)

// heartbeat is how often a running pod says that loadgen still runs.
const heartbeat = time.Minute

// stderrTail is how much of loadgen's standard error a failure quotes.
const stderrTail = 2000

// Pod is one phase where its pod runs: the work directory holds InputFile.
type Pod struct {
	// WorkDir holds InputFile; the phase's files are left there.
	WorkDir string
	// Target is the base URL of every request, as this pod reaches the system.
	Target string
	// Generator is the generator command; `generate` and its flags follow it.
	Generator []string
	// DatasetFile is the dataset file the generator reads.
	DatasetFile string
	// MinSustainedRatio is the open loop's --min-sustained.
	MinSustainedRatio float64
	// Loadgen is the loadgen binary; empty: the loadgen next to this process's executable (as a
	// wheel bundles them), else loadgen on PATH.
	Loadgen string
	// Stderr receives loadgen's and the generator's standard error; nil discards it.
	Stderr io.Writer
	// Event, when set, is told what the pod is doing (stage and message, see PhaseEvent).
	Event func(stage, message string)
}

// PodError is a phase that did not run: the input, loadgen or the files failed.
type PodError struct{ Msg string }

func (e *PodError) Error() string { return e.Msg }

func podErr(format string, args ...any) error { return &PodError{fmt.Sprintf(format, args...)} }

// Run runs the phase of p.WorkDir: loadgen starts the generator and sends its stream to the
// target, then the pod's files are written into the work directory (OutputFile last). It returns
// the PhaseOutput it wrote.
func (p Pod) Run(ctx context.Context) (*pb.PhaseOutput, error) {
	event := p.Event
	if event == nil {
		event = func(string, string) {}
	}
	in := &pb.PhaseInput{}
	raw, err := os.ReadFile(filepath.Join(p.WorkDir, InputFile))
	if err != nil {
		return nil, podErr("the phase input: %v", err)
	}
	if err := proto.Unmarshal(raw, in); err != nil {
		return nil, podErr("%s: not a PhaseInput: %v", filepath.Join(p.WorkDir, InputFile), err)
	}
	if err := Validate(in); err != nil {
		return nil, podErr("%s: %v", filepath.Join(p.WorkDir, InputFile), err)
	}
	if len(p.Generator) == 0 || p.Target == "" || p.DatasetFile == "" {
		return nil, podErr("the phase needs a generator command, a target and a dataset file")
	}
	scratch, err := os.MkdirTemp("", "vibesys-phase-")
	if err != nil {
		return nil, podErr("a scratch directory: %v", err)
	}
	defer os.RemoveAll(scratch)
	state := filepath.Join(scratch, "state")
	if err := os.Mkdir(state, 0o755); err != nil {
		return nil, podErr("the generator's state directory: %v", err)
	}
	results := filepath.Join(scratch, ResultsFile)
	if in.GetCapture() {
		results = filepath.Join(p.WorkDir, ResultsFile)
	}
	summary := filepath.Join(p.WorkDir, SummaryFile)
	generator := append(append([]string{}, p.Generator...),
		"generate", "--phase-input", filepath.Join(p.WorkDir, InputFile), "--dataset", p.DatasetFile, "--state", state)
	args := LoadgenArgs(in, p.Target, results, summary, p.MinSustainedRatio, generator)
	budget := seconds(in.GetWarmupSecs()+in.GetSecs()) + 2*StepTimeout + loadgenMargin
	event("loadgen", fmt.Sprintf("phase %d: loadgen runs %s: the generator starts, then about %s s of load",
		in.GetPhaseIndex(), Describe(in), g(in.GetWarmupSecs()+in.GetSecs())))
	began := time.Now()
	if err := p.loadgen(ctx, args, budget, began, event); err != nil {
		return nil, err
	}
	loadgenSecs := time.Since(began).Seconds()
	event("files", fmt.Sprintf("phase %d: loadgen ran %.0f s; writing the phase's files", in.GetPhaseIndex(), loadgenSecs))
	if in.GetCapture() {
		if err := copyFile(filepath.Join(state, KeptFile), filepath.Join(p.WorkDir, KeptFile)); err != nil {
			return nil, podErr("the generator kept no record of its operations (%v): is it built on vibesys-evaluator 0.3 or later?", err)
		}
	} else if err := pack(results, filepath.Join(p.WorkDir, PackedResultsFile)); err != nil {
		return nil, podErr("packing loadgen's results: %v", err)
	}
	s, err := ReadSummary(summary)
	if err != nil {
		return nil, podErr("%v", err)
	}
	output := &pb.PhaseOutput{Summary: s, LoadgenSecs: loadgenSecs}
	encoded, err := proto.Marshal(output)
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(filepath.Join(p.WorkDir, OutputFile), encoded, 0o644); err != nil {
		return nil, podErr("%v", err)
	}
	event("done", fmt.Sprintf("phase %d: %d operations in the window, %.1f ops/s, %d errors",
		in.GetPhaseIndex(), s.GetCompleted(), s.GetAchievedRate(), s.GetErrors()))
	return output, nil
}

// defaultLoadgen is the loadgen next to this process's executable, else loadgen on PATH.
func defaultLoadgen() string {
	if exe, err := os.Executable(); err == nil {
		beside := filepath.Join(filepath.Dir(exe), "loadgen")
		if info, err := os.Stat(beside); err == nil && info.Mode().IsRegular() && info.Mode()&0o111 != 0 {
			return beside
		}
	}
	return "loadgen"
}

// loadgen runs loadgen with args, its standard error to p.Stderr and to a tail for the message.
func (p Pod) loadgen(ctx context.Context, args []string, budget time.Duration, began time.Time, event func(string, string)) error {
	binary := p.Loadgen
	if binary == "" {
		binary = defaultLoadgen()
	}
	ctx, cancel := context.WithTimeout(ctx, budget)
	defer cancel()
	cmd := exec.CommandContext(ctx, binary, args...)
	cmd.Cancel = func() error { return cmd.Process.Signal(syscall.SIGTERM) }
	cmd.WaitDelay = 15 * time.Second
	tail := &tailWriter{max: stderrTail}
	var stderr io.Writer = tail
	if p.Stderr != nil {
		stderr = io.MultiWriter(p.Stderr, tail)
	}
	cmd.Stdout, cmd.Stderr = stderr, stderr
	if err := cmd.Start(); err != nil {
		return podErr("cannot run loadgen at %q: %v", binary, err)
	}
	done := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		ticker := time.NewTicker(heartbeat)
		defer ticker.Stop()
		for {
			select {
			case <-done:
				return
			case now := <-ticker.C:
				event("running", fmt.Sprintf("loadgen has run %.0f s", now.Sub(began).Seconds()))
			}
		}
	}()
	err := cmd.Wait()
	close(done)
	wg.Wait()
	switch {
	case errors.Is(ctx.Err(), context.DeadlineExceeded):
		return podErr("loadgen ran past %.0f s (the warmup, the window, the spill and %.0f s for the generator's start-up); its stderr ends:\n%s",
			budget.Seconds(), loadgenMargin.Seconds(), tail.String())
	case ctx.Err() != nil:
		return ctx.Err()
	case err != nil:
		var exit *exec.ExitError
		if errors.As(err, &exit) {
			return podErr("loadgen exited %d: %s", exit.ExitCode(), strings.TrimSpace(tail.String()))
		}
		return podErr("loadgen: %v", err)
	}
	return nil
}

// ReadSummary reads loadgen's summary file.
func ReadSummary(path string) (*pb.PhaseSummary, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer func() { _ = file.Close() }()
	reader, _, err := files.NewReader(file, files.Binpb, pb.FileKind_FILE_KIND_SUMMARY)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	summary := &pb.PhaseSummary{}
	if err := reader.Read(summary); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return summary, nil
}

// pack writes the gzip of from into to and removes from.
func pack(from, to string) error {
	in, err := os.Open(from)
	if err != nil {
		return err
	}
	defer func() { _ = in.Close() }()
	out, err := os.Create(to)
	if err != nil {
		return err
	}
	zipped := gzip.NewWriter(out)
	if _, err := io.Copy(zipped, in); err != nil {
		_ = out.Close()
		return err
	}
	if err := zipped.Close(); err != nil {
		_ = out.Close()
		return err
	}
	if err := out.Close(); err != nil {
		return err
	}
	return os.Remove(from)
}

func copyFile(from, to string) error {
	data, err := os.ReadFile(from)
	if err != nil {
		return err
	}
	return os.WriteFile(to, data, 0o644)
}

// tailWriter keeps the last max bytes written to it.
type tailWriter struct {
	mu  sync.Mutex
	max int
	buf []byte
}

func (t *tailWriter) Write(p []byte) (int, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.buf = append(t.buf, p...)
	if len(t.buf) > t.max {
		t.buf = bytes.Clone(t.buf[len(t.buf)-t.max:])
	}
	return len(p), nil
}

func (t *tailWriter) String() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return string(t.buf)
}
