package kubernetes

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

// Timeout bounds the reading of a cluster's identity.
const Timeout = time.Minute

// Kubectl is the Cluster that asks kubectl on the machine the shell reaches.
type Kubectl struct {
	Shell  remote.Shell
	Timers clock.Timers
}

var _ Cluster = Kubectl{}

// Identify implements Cluster.
func (k Kubectl) Identify(ctx context.Context, kubeconfig string) (Identity, error) {
	bounded, done := remote.Bounded(ctx, k.Timers, Timeout)
	defer done()
	run := func(args ...string) (string, error) {
		argv := append([]string{"kubectl", "--kubeconfig", kubeconfig}, args...)
		out, err := remote.Output(bounded, k.Shell, argv, nil)
		switch {
		case err == nil:
			return out, nil
		case ctx.Err() != nil:
			return "", ctx.Err()
		case bounded.Err() != nil:
			return "", unavailable("kubectl on %s did not answer within %s", k.Shell.Where(), Timeout)
		}
		return "", unavailable("reading the cluster of %s with kubectl on %s: %s", kubeconfig, k.Shell.Where(), core.AsFailure(err).Message)
	}
	server, err := run("config", "view", "--minify", "--output", "jsonpath={.clusters[0].cluster.server}")
	if err != nil {
		return Identity{}, err
	}
	version, err := run("version", "--output", "json", "--request-timeout", "30s")
	if err != nil {
		return Identity{}, err
	}
	v, err := ParseVersion(version)
	if err != nil {
		return Identity{}, err
	}
	return Identity{Server: strings.TrimSpace(server), Version: v}, nil
}

// ParseVersion reads the API server's version from what kubectl version --output json prints.
func ParseVersion(out string) (string, error) {
	var v struct {
		ServerVersion *struct {
			GitVersion string `json:"gitVersion"`
		} `json:"serverVersion"`
	}
	if err := json.Unmarshal([]byte(out), &v); err != nil {
		return "", unavailable("kubectl version printed no JSON: %v", err)
	}
	if v.ServerVersion == nil || v.ServerVersion.GitVersion == "" {
		return "", unavailable("kubectl version did not say the server's version; is the cluster up?")
	}
	return v.ServerVersion.GitVersion, nil
}

func unavailable(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(format, args...)}
}
