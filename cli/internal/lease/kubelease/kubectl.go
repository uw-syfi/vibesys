package kubelease

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
)

// CallTimeout bounds each call of kubectl.
const CallTimeout = 30 * time.Second

// Kubectl is the API of a cluster as kubectl on the machine the shell reaches sees it.
type Kubectl struct {
	Shell remote.Shell
	// Kubeconfig is the kubeconfig file there, and Context the context of it; empty uses kubectl's
	// own.
	Kubeconfig, Context string
	// Namespace holds the Lease objects; empty is "default".
	Namespace string
	Timers    clock.Timers
}

var _ API = Kubectl{}

// Where implements API.
func (k Kubectl) Where() string {
	where := "the cluster"
	if k.Context != "" {
		where += " " + k.Context
	}
	return where + " (namespace " + k.namespace() + ")"
}

func (k Kubectl) namespace() string {
	if k.Namespace == "" {
		return "default"
	}
	return k.Namespace
}

func (k Kubectl) run(ctx context.Context, stdin string, args ...string) (string, error) {
	argv := []string{"kubectl"}
	if k.Kubeconfig != "" {
		argv = append(argv, "--kubeconfig", k.Kubeconfig)
	}
	if k.Context != "" {
		argv = append(argv, "--context", k.Context)
	}
	argv = append(argv, "--namespace", k.namespace(), "--request-timeout", "20s")
	argv = append(argv, args...)
	bounded, done := remote.Bounded(ctx, k.Timers, CallTimeout)
	defer done()
	var in *strings.Reader
	if stdin != "" {
		in = strings.NewReader(stdin)
	}
	var out string
	var err error
	if in != nil {
		out, err = remote.Output(bounded, k.Shell, argv, in)
	} else {
		out, err = remote.Output(bounded, k.Shell, argv, nil)
	}
	switch {
	case err == nil:
		return out, nil
	case ctx.Err() != nil:
		return "", ctx.Err()
	case bounded.Err() != nil:
		return "", &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("lease: kubectl on %s did not answer within %s", k.Shell.Where(), CallTimeout)}
	}
	return "", err
}

// Get implements API.
func (k Kubectl) Get(ctx context.Context, name string) (*Object, error) {
	out, err := k.run(ctx, "", "get", "lease", name, "--ignore-not-found", "--output", "json")
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(out) == "" {
		return nil, nil
	}
	var o Object
	if err := json.Unmarshal([]byte(out), &o); err != nil {
		return nil, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("lease: kubectl printed no lease: %v", err)}
	}
	return &o, nil
}

// Create implements API.
func (k Kubectl) Create(ctx context.Context, o *Object) error {
	return k.write(ctx, "create", o)
}

// Replace implements API.
func (k Kubectl) Replace(ctx context.Context, o *Object) error {
	return k.write(ctx, "replace", o)
}

func (k Kubectl) write(ctx context.Context, verb string, o *Object) error {
	c := *o
	c.Metadata.Namespace = k.namespace()
	b, err := json.Marshal(c)
	if err != nil {
		return err
	}
	_, err = k.run(ctx, string(b), verb, "--filename", "-")
	if err == nil {
		return nil
	}
	msg := err.Error()
	switch {
	case strings.Contains(msg, "AlreadyExists") || strings.Contains(msg, "already exists"):
		return ErrExists
	case strings.Contains(msg, "the object has been modified") || strings.Contains(msg, "Conflict"):
		return ErrConflict
	}
	return err
}
