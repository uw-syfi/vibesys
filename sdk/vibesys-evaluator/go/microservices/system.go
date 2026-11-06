package microservices

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

// System is where a sample's application runs and its phase pods with it: Local (an application
// already running), or a Kubernetes namespace of its own (package k8s).
type System interface {
	deploy(ctx context.Context, req deployRequest) (deployment, error)
}

// deployRequest is what a System is given for one sample.
type deployRequest struct {
	// projectRoot is the candidate's checkout (--project-root); "" when not given.
	projectRoot string
	// artifacts is the sample's artifacts directory, for the deploy's logs.
	artifacts string
	// seed is the seed command, nil when the task has no seed.
	seed []string
	log  func(string)
}

// deployment is a deployed application: it runs pods next to it until closed.
type deployment interface {
	pods
	// close tears the application down; it is called on every path once deploy returned.
	close(ctx context.Context) error
}

// pods runs commands of the task next to the application.
type pods interface {
	runPod(ctx context.Context, spec podSpec) (podRun, error)
}

// podSpec is one pod: its command, given the work directory and the target as the pod sees
// them; the files written into the work directory first; where the files it leaves go.
type podSpec struct {
	name    string
	command func(workDir, target string) []string
	inputs  map[string][]byte
	outputs string
	timeout time.Duration
}

// podRun is a finished pod. A nonzero exit is a result; a pod that could not run is an error.
type podRun struct {
	exitCode int
	log      string
	seconds  float64
}

// Local is an application already running at target (a base URL, http://host:port), started by
// whoever runs the benchmark: the seed and each phase run as child processes here, with the
// same files and command lines a phase pod gets, so a benchmark runs without a cluster.
func Local(target string) System { return local{target: target} }

type local struct{ target string }

// seedTimeout bounds a local seed.
const seedTimeout = 10 * time.Minute

func (l local) deploy(ctx context.Context, req deployRequest) (deployment, error) {
	if l.target == "" {
		return nil, errors.New("microservices.Local needs the application's base URL")
	}
	if req.seed != nil {
		out, code, err := runChild(ctx, req.seed, "", seedTimeout)
		_ = os.WriteFile(filepath.Join(req.artifacts, "seed.log"), out, 0o644)
		if err != nil {
			return nil, fmt.Errorf("the seed: %w; log tail:\n%s", err, tail(string(out), 3000))
		}
		if code != 0 {
			return nil, fmt.Errorf("the seed exited %d; log tail:\n%s", code, tail(string(out), 3000))
		}
	}
	req.log(fmt.Sprintf("local system at %s", l.target))
	return l, nil
}

func (l local) close(context.Context) error { return nil }

func (l local) runPod(ctx context.Context, spec podSpec) (podRun, error) {
	work, err := os.MkdirTemp("", "vse-"+spec.name+"-")
	if err != nil {
		return podRun{}, err
	}
	defer os.RemoveAll(work)
	for name, data := range spec.inputs {
		if err := os.WriteFile(filepath.Join(work, name), data, 0o644); err != nil {
			return podRun{}, err
		}
	}
	began := time.Now()
	out, code, err := runChild(ctx, spec.command(work, l.target), work, spec.timeout)
	ran := podRun{exitCode: code, log: string(out), seconds: time.Since(began).Seconds()}
	if err != nil {
		return ran, fmt.Errorf("%s: %w; log tail:\n%s", spec.name, err, tail(ran.log, 3000))
	}
	entries, err := os.ReadDir(work)
	if err != nil {
		return ran, err
	}
	for _, entry := range entries {
		if _, input := spec.inputs[entry.Name()]; input || !entry.Type().IsRegular() {
			continue
		}
		data, err := os.ReadFile(filepath.Join(work, entry.Name()))
		if err != nil {
			return ran, err
		}
		if err := os.WriteFile(filepath.Join(spec.outputs, entry.Name()), data, 0o644); err != nil {
			return ran, err
		}
	}
	return ran, nil
}

// runChild runs argv with its standard output and error combined; a nonzero exit is its code,
// not an error. Past timeout, or when ctx ends, it is sent SIGTERM.
func runChild(ctx context.Context, argv []string, dir string, timeout time.Duration) ([]byte, int, error) {
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
