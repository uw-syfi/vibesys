package microservices

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/accuracy"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/phase"
)

// System is where a sample's application runs and its phase pods with it: Local (an application
// already running), or K8s (a Kubernetes namespace of its own).
type System interface {
	deploy(ctx context.Context, req deployRequest) (deployment, error)
}

// deployRequest is what a System is given for one sample.
type deployRequest struct {
	// projectRoot is the candidate's checkout (--project-root); "" when not given.
	projectRoot string
	// artifacts is the job's artifacts directory (.../<job>/<step>), for the deploy's logs and
	// the namespace's job label.
	artifacts string
	// seed is the seed command, nil when the task has no seed.
	seed []string
	log  func(string)
}

// deployment is a deployed application: it runs pods next to it until closed.
type deployment interface {
	phase.Pods
	// restart and exec are the fault actions on a Deployment of the application (Restart,
	// RestartAll, Exec): on Kubernetes they act; elsewhere they fail.
	restart(deployment string, every bool) func(context.Context) error
	exec(deployment string, argv []string) func(context.Context) error
	// close tears the application down; it is called on every path once deploy returned.
	close(ctx context.Context) error
}

// Local is an application already running at target (a base URL, http://host:port), started by
// whoever runs the benchmark: the seed and each phase run as child processes here, with the
// same files and command lines a phase pod gets, so a benchmark runs without a cluster.
func Local(target string) System { return local{phase.LocalPods{Target: target}} }

type local struct{ phase.LocalPods }

func (l local) deploy(ctx context.Context, req deployRequest) (deployment, error) {
	if l.Target == "" {
		return nil, errors.New("microservices.Local needs the application's base URL")
	}
	if req.seed != nil {
		if err := phase.LocalSeed(ctx, req.seed, filepath.Join(req.artifacts, "seed.log")); err != nil {
			return nil, err
		}
	}
	req.log(fmt.Sprintf("local system at %s", l.Target))
	return l, nil
}

func (l local) close(context.Context) error { return nil }

func (l local) restart(deployment string, _ bool) func(context.Context) error {
	return accuracy.Unsupported("microservices.Local", "restart "+deployment)
}

func (l local) exec(deployment string, _ []string) func(context.Context) error {
	return accuracy.Unsupported("microservices.Local", "exec in "+deployment)
}
