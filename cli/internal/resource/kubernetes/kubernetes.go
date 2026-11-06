// Package kubernetes is the resource of a Kubernetes cluster: a container that has it gets the
// cluster's kubeconfig, read-only, at the path KUBECONFIG names, the network mode that reaches the
// cluster's API server, and the addresses of the registry the cluster pulls from and of the image
// builder, as the fields of KubernetesEnvironment. It is what
// [placements.<name>.resources.kubernetes] configures. A task that deploys to a cluster needs it
// ([environment] needs = ["kubernetes"]).
//
// The fingerprint is the cluster's API server address and version, read with kubectl on the
// engine's machine, and recorded with each result to tell which cluster it was taken against.
//
// The daemon gives a job the cluster and does not manage it: what the job deploys, and cleaning it
// up, is the task's.
package kubernetes

import (
	"context"
	"fmt"
	"regexp"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
)

// What a container is given.
const (
	// ConfigPath is where the container finds the kubeconfig.
	ConfigPath = "/vibesys/kubeconfig"
)

// The fingerprint facts.
const (
	FactServer  = "kubernetes_server"
	FactVersion = "kubernetes_version"
)

// Identity is what identifies a cluster.
type Identity struct {
	// Server is the API server's address, as the kubeconfig names it.
	Server string
	// Version is the API server's version, such as v1.31.0.
	Version string
}

// Cluster reads the identity of the cluster a kubeconfig names. It fails with a *core.Failure of
// kind FailureUnavailable when the cluster or the kubeconfig cannot be read, and returns by a
// bound of its own.
type Cluster interface {
	Identify(ctx context.Context, kubeconfig string) (Identity, error)
}

// Config configures New.
type Config struct {
	// Kubeconfig is the absolute path of the kubeconfig on the engine's machine. It is required.
	Kubeconfig string
	// Registry and Builder are addresses the container gets; empty sets nothing.
	Registry, Builder string
	// Network is the container's network mode; empty leaves the engine's default.
	Network string
	// Machine names the engine's machine, so that the clusters of two machines' kubeconfigs are
	// two claims.
	Machine string
	// Cluster reads the cluster's identity for the fingerprint.
	Cluster Cluster
}

var (
	address = regexp.MustCompile(`^[A-Za-z0-9._:/@+-]*$`)
	network = regexp.MustCompile(`^([A-Za-z0-9][A-Za-z0-9_.-]*)?$`)
	machine = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.@-]*$`)
)

// Kubernetes is the resource. Create it with New.
type Kubernetes struct{ cfg Config }

var _ resource.Resource = (*Kubernetes)(nil)

// New returns the resource, or a core.FailureInvalidInput naming the setting that is wrong.
func New(cfg Config) (*Kubernetes, error) {
	if err := resource.CheckPath("kubeconfig", cfg.Kubeconfig); err != nil {
		return nil, invalid("%s", core.AsFailure(err).Message)
	}
	switch {
	case !address.MatchString(cfg.Registry):
		return nil, invalid("registry: %q is not an address", cfg.Registry)
	case !address.MatchString(cfg.Builder):
		return nil, invalid("builder: %q is not an address", cfg.Builder)
	case !network.MatchString(cfg.Network):
		return nil, invalid("network: %q is not a network mode", cfg.Network)
	case !machine.MatchString(cfg.Machine):
		return nil, invalid("the machine %q is not a machine name", cfg.Machine)
	case cfg.Cluster == nil:
		return nil, invalid("no reader of the cluster's identity")
	}
	return &Kubernetes{cfg: cfg}, nil
}

// Kind implements resource.Resource.
func (*Kubernetes) Kind() core.ResourceKind { return core.ResourceKubernetes }

// Claims implements resource.Resource: the cluster the kubeconfig of the machine names, held alone
// by every job, of every kind, that runs with it. A job deploys what it runs into the cluster, with
// CPU and memory the daemon does not see, so two jobs on one cluster can starve each other's pods
// whatever the placement's max_jobs: a check that cannot schedule its pods fails as if the change
// were wrong. The claim is exclusive, so the scheduler runs one job at a time on the cluster, and the
// cluster is leased for every job, so the daemons that share it take turns too.
func (k *Kubernetes) Claims() []core.Claim {
	return []core.Claim{{Resource: string(core.ResourceKubernetes) + ":" + k.cfg.Machine + k.cfg.Kubeconfig, Amount: 1, Exclusive: true}}
}

// Fingerprint implements resource.Resource: the API server's address and version.
func (k *Kubernetes) Fingerprint(ctx context.Context) (map[string]string, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	id, err := k.cfg.Cluster.Identify(ctx, k.cfg.Kubeconfig)
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, &core.Failure{Kind: core.FailureUnavailable, Message: "kubernetes: " + core.AsFailure(err).Message}
	}
	if id.Server == "" || id.Version == "" {
		return nil, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(
			"kubernetes: the cluster of %s did not say its server and version (%+v)", k.cfg.Kubeconfig, id)}
	}
	return map[string]string{FactServer: id.Server, FactVersion: id.Version}, nil
}

// Attach implements resource.Resource.
func (k *Kubernetes) Attach(ctx context.Context) (resource.Attachment, error) {
	if err := ctx.Err(); err != nil {
		return resource.Attachment{}, err
	}
	// The registry and the builder are left out of the message when the config names none.
	cluster, err := envcodec.Encode(&cliv1.KubernetesEnvironment{Kubeconfig: ConfigPath, Registry: k.cfg.Registry, Builder: k.cfg.Builder})
	if err != nil {
		return resource.Attachment{}, invalid("the cluster is not a valid kubernetes environment: %v", err)
	}
	a := resource.Attachment{
		Mounts:  []resource.Mount{{Host: k.cfg.Kubeconfig, Container: ConfigPath, ReadOnly: true}},
		Env:     cluster.Set,
		Network: k.cfg.Network,
	}
	return a, nil
}

func invalid(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: "kubernetes: " + fmt.Sprintf(format, args...)}
}
