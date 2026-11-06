package microservices

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/kube"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/version"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/wire"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/k8s"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/workload"
)

// K8s is an application on Kubernetes (package k8s; the Go form of Python's
// microservices.k8s.App): each sample builds its images, deploys it into a namespace of its own
// in the cluster the environment gives (KUBECONFIG, VIBESYS_K8S_REGISTRY, VIBESYS_K8S_BUILDER),
// waits until it serves and every ready step succeeds through the gateway, and deletes the
// namespace on every path. The seed and the phases run in pods of the task image there, as this
// binary's `seed` and `phase` commands at the path it has here: the task directory (app.Manifest
// and the task image's Dockerfile) is this binary's directory, as a Python task's is its
// script's. Restart, RestartAll and Exec act on app.Restartable Deployments.
func K8s(app k8s.App, ready ...workload.Step) System { return kubeSystem{app: app, ready: ready} }

type kubeSystem struct {
	app   k8s.App
	ready []workload.Step
}

func (s kubeSystem) deploy(ctx context.Context, req deployRequest) (deployment, error) {
	exe, err := executable()
	if err != nil {
		return nil, err
	}
	steps := make([]*pb.Step, len(s.ready))
	for n, step := range s.ready {
		steps[n] = wire.StepMessage(step)
	}
	plan, err := wire.ProbePlan(steps, "vibesys-evaluator-go "+version.Version)
	if err != nil {
		return nil, err
	}
	if len(steps) == 0 {
		return nil, errors.New("microservices.K8s needs at least one ready step: a request that succeeds once the application serves")
	}
	work, err := os.MkdirTemp("", "vse-k8s-")
	if err != nil {
		return nil, err
	}
	cluster, err := kube.Connect(filepath.Join(work, "core"), req.artifacts, req.log)
	if err != nil {
		os.RemoveAll(work)
		return nil, err
	}
	app := s.app
	app.HasSeed = req.seed != nil
	var seed *k8s.PodCommand
	if req.seed != nil {
		seed = &k8s.PodCommand{Argv: req.seed}
	}
	src := k8s.AppSource{App: app, TaskDir: filepath.Dir(exe), ProjectRoot: req.projectRoot, ProbePlan: plan}
	d, err := k8s.Deploy(ctx, src, seed, cluster)
	if err != nil {
		os.RemoveAll(work)
		return nil, err
	}
	if err := os.WriteFile(filepath.Join(req.artifacts, "namespace.txt"), []byte(d.Namespace+"\n"), 0o644); err != nil {
		req.log(fmt.Sprintf("namespace.txt: %v", err))
	}
	req.log(fmt.Sprintf("serving in namespace %s", d.Namespace))
	return kubeDeployment{Pods: kube.Pods{D: d}, d: d, work: work}, nil
}

type kubeDeployment struct {
	kube.Pods
	d    *k8s.Deployment
	work string
}

func (k kubeDeployment) restart(deployment string, every bool) func(context.Context) error {
	return kube.Restart(k.d, deployment, every)
}

func (k kubeDeployment) exec(deployment string, argv []string) func(context.Context) error {
	return kube.Exec(k.d, deployment, argv)
}

func (k kubeDeployment) flush(cache string) func(context.Context) error {
	return kube.Flush(k.d, cache)
}

func (k kubeDeployment) close(ctx context.Context) error {
	defer os.RemoveAll(k.work)
	return k.d.Close(ctx, k8s.CloseOptions{})
}
