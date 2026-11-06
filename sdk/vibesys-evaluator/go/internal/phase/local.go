package phase

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"
)

// LocalPods runs each pod as a child process here, against an application already running at
// Target: its work directory is a new temporary one, and the files the command leaves there are
// copied into the spec's outputs, as a phase pod's are.
type LocalPods struct{ Target string }

// RunPod implements Pods.
func (l LocalPods) RunPod(ctx context.Context, spec PodSpec) (PodRun, error) {
	work, err := os.MkdirTemp("", "vse-"+spec.Name+"-")
	if err != nil {
		return PodRun{}, err
	}
	defer os.RemoveAll(work)
	for name, data := range spec.Inputs {
		if err := os.WriteFile(filepath.Join(work, name), data, 0o644); err != nil {
			return PodRun{}, err
		}
	}
	began := time.Now()
	out, code, err := RunChild(ctx, spec.Command(work, l.Target), work, spec.Timeout)
	ran := PodRun{ExitCode: code, Log: string(out), Seconds: time.Since(began).Seconds()}
	if err != nil {
		return ran, fmt.Errorf("%s: %w; log tail:\n%s", spec.Name, err, Tail(ran.Log, 3000))
	}
	entries, err := os.ReadDir(work)
	if err != nil {
		return ran, err
	}
	for _, entry := range entries {
		if _, input := spec.Inputs[entry.Name()]; input || !entry.Type().IsRegular() {
			continue
		}
		data, err := os.ReadFile(filepath.Join(work, entry.Name()))
		if err != nil {
			return ran, err
		}
		if err := os.WriteFile(filepath.Join(spec.Outputs, entry.Name()), data, 0o644); err != nil {
			return ran, err
		}
	}
	return ran, nil
}

// SeedTimeout bounds a local seed.
const SeedTimeout = 10 * time.Minute

// LocalSeed runs a local application's seed command here and writes its output into log; an
// error names its exit or why it did not run, with the output's tail.
func LocalSeed(ctx context.Context, argv []string, log string) error {
	out, code, err := RunChild(ctx, argv, "", SeedTimeout)
	_ = os.WriteFile(log, out, 0o644)
	if err != nil {
		return fmt.Errorf("the seed: %w; log tail:\n%s", err, Tail(string(out), 3000))
	}
	if code != 0 {
		return fmt.Errorf("the seed exited %d; log tail:\n%s", code, Tail(string(out), 3000))
	}
	return nil
}

// RunChild runs argv in dir with its standard output and error combined; a nonzero exit is its
// code, not an error. Past timeout, or when ctx ends, it is sent SIGTERM.
func RunChild(ctx context.Context, argv []string, dir string, timeout time.Duration) ([]byte, int, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Dir = dir
	cmd.Cancel = func() error { return cmd.Process.Signal(syscall.SIGTERM) }
	cmd.WaitDelay = 15 * time.Second
	var out bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &out
	err := cmd.Run()
	var exit *exec.ExitError
	switch {
	case errors.Is(ctx.Err(), context.DeadlineExceeded):
		return out.Bytes(), -1, fmt.Errorf("ran past %.0f s", timeout.Seconds())
	case ctx.Err() != nil:
		return out.Bytes(), -1, ctx.Err()
	case errors.As(err, &exit):
		return out.Bytes(), exit.ExitCode(), nil
	case err != nil:
		return out.Bytes(), -1, fmt.Errorf("cannot run %q: %w", argv[0], err)
	}
	return out.Bytes(), 0, nil
}
