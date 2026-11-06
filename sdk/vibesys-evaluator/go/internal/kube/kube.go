// Package kube runs the phase runner's pods and fault actions on a deployment of package k8s:
// what the Go microservices.K8s system and the core's `accuracy run` share.
package kube

import (
	"context"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/phase"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/k8s"
)

// ExecTimeout bounds a fault's command.
const ExecTimeout = 5 * time.Minute

// Connect is the cluster the environment gives (KUBECONFIG, VIBESYS_K8S_REGISTRY,
// VIBESYS_K8S_BUILDER), with workDir as the deploy layer's scratch directory and artifacts (when
// not empty) for its logs.
func Connect(workDir, artifacts string, log func(string)) (k8s.Cluster, error) {
	env, err := k8s.ParseEnvironment(os.Getenv, workDir, artifacts)
	if err != nil {
		return k8s.Cluster{}, err
	}
	return k8s.Cluster{
		Env: env, Kube: k8s.Kubectl{}, Clock: k8s.SystemClock{}, Log: log,
		Builder: k8s.BuildctlBuilder{Address: env.Builder, Clock: k8s.SystemClock{}},
	}, nil
}

// Pods runs each phase pod as a pod of the deployment's namespace (k8s.RolePhase), its command
// given k8s.WorkDir and the gateway, and logs each pod's CPU use after it.
type Pods struct {
	D *k8s.Deployment
}

// RunPod implements phase.Pods.
func (p Pods) RunPod(ctx context.Context, spec phase.PodSpec) (phase.PodRun, error) {
	command := k8s.PodCommand{Argv: spec.Command(k8s.WorkDir, p.D.Gateway.URL), Inputs: spec.Inputs}
	ran, err := p.D.RunPod(ctx, k8s.RolePhase, command, spec.Timeout+podOverhead, k8s.RunOptions{Outputs: spec.Outputs})
	if err != nil {
		return phase.PodRun{}, err
	}
	_, _ = p.D.Usage(ctx, spec.Name)
	return phase.PodRun{ExitCode: ran.ExitCode, Log: ran.Log, Seconds: ran.Seconds}, nil
}

// podOverhead is the slack over a phase's own timeout for its pod to start and its files to be
// copied back.
const podOverhead = 30 * time.Minute

// Restart deletes one pod of the Deployment (every pod with every) and waits until it serves
// again.
func Restart(d *k8s.Deployment, deployment string, every bool) func(context.Context) error {
	return func(ctx context.Context) error {
		_, err := d.Restart(ctx, []string{deployment}, k8s.RestartOptions{EveryPod: every})
		return err
	}
}

// Flush empties the cache Deployment in place, in every pod.
func Flush(d *k8s.Deployment, cache string) func(context.Context) error {
	return func(ctx context.Context) error { return d.Flush(ctx, cache) }
}

// Exec runs argv in the Deployment's first pod; a nonzero exit is an error with its output.
func Exec(d *k8s.Deployment, deployment string, argv []string) func(context.Context) error {
	return func(ctx context.Context) error {
		answers, err := d.Exec(ctx, k8s.ExecTarget{Deployment: deployment}, argv, ExecTimeout)
		if err != nil {
			return err
		}
		for _, a := range answers {
			if a.ExitCode != 0 {
				return fmt.Errorf("%q exited %d in %s: %s", strings.Join(argv, " "), a.ExitCode, a.Pod, phase.Tail(a.Output, 1500))
			}
		}
		return nil
	}
}
