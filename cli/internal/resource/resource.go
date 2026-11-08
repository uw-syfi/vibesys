// Package resource declares Resource: something a placement gives every container it runs, such
// as GPUs, access to a Kubernetes cluster, or read-only directories. A task says which kinds it
// needs ([environment] needs) and a placement says which it offers ([placements.<name>.resources]).
//
// Each kind is a package of its own under this one, and they import this package and core, never
// each other; only the binary's wiring names a concrete kind. The docker environment applies what
// a resource attaches without knowing which kind it is, and records what the resource's fingerprint
// says with each result, to tell where it was taken; that takes no part in which results are
// compared, since placements, and the resources they give, are interchangeable. Every
// implementation passes the contract in resourcetest.
package resource

import (
	"context"
	"fmt"
	"maps"
	"regexp"
	"slices"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Resource is something a placement gives every container it runs.
type Resource interface {
	// Kind is the kind of resource this is. A placement has at most one of each kind.
	Kind() core.ResourceKind

	// Claims are what a job that uses the resource holds while it runs, for scheduling to account
	// for. Each claim's resource id starts with the kind and a colon, and names the thing itself
	// and not the placement, so two placements that give the same cluster or the same GPUs claim
	// the same id. It does no I/O and returns the same claims on every call.
	Claims() []core.Claim

	// Fingerprint returns what identifies the resource, recorded with each result, as facts by
	// name, such as the GPU model or the cluster's server and version. The names are FactName and
	// are the resource's contract: they never collide with another kind's. It fails with a
	// *core.Failure of kind FailureUnavailable when the resource cannot be read.
	Fingerprint(ctx context.Context) (map[string]string, error)

	// Offers reports the accelerators given to a container, or an error when unreadable.
	Offers(ctx context.Context) ([]*cliv1.AcceleratorOffer, error)

	// Attach returns what a container is given to use the resource. An Attachment is only data
	// for the container's creation: a resource makes nothing outside the container, so removing
	// the container, which the docker environment does on every path, undoes it.
	Attach(ctx context.Context) (Attachment, error)
}

// Attachment is what a container is given to use a resource: what docker run is told besides
// what the docker environment gives every container.
type Attachment struct {
	// Mounts are the directories or files of the engine's machine the container sees.
	Mounts []Mount
	// Env is set for every process of the container.
	Env map[string]string
	// Network is the container's network mode (docker run --network). Empty leaves the engine's
	// default.
	Network string
	// GPUs are the GPUs the container gets. The zero value gives it none.
	GPUs GPUs
	// GPUEnv is set, beside Env, for every process of a container that gets the GPUs: the variables
	// that pass them, which a container that gets none, such as the task's container beside a
	// candidate's server, does not get either.
	GPUEnv map[string]string
	// Check, when set, is a command the container runs before anything else: a program and its
	// arguments that exits 0 when the resource works in the container's image, and otherwise prints
	// why it does not, which is what the run that needed it fails with.
	Check []string
}

// Mount is a directory or a file of the engine's machine that the container sees.
type Mount struct {
	Host, Container string
	ReadOnly        bool
}

// GPUs says which GPUs a container gets, and how.
type GPUs struct {
	// Devices is a docker --gpus value, such as "all" or "device=0,1", of NVIDIA GPUs. Empty passes
	// no NVIDIA GPU.
	Devices string
	// Runtime, when set, is the container runtime that passes the devices instead of --gpus.
	Runtime string
	// ROCm passes the machine's AMD GPUs through the ROCm kernel driver: its devices and the group
	// that may use them under docker, --rocm under Apptainer. It is never set with Devices.
	ROCm bool
}

// Given reports whether the container gets GPUs of either kind.
func (g GPUs) Given() bool { return g.Devices != "" || g.ROCm }

var (
	factName    = regexp.MustCompile(`^[a-z][a-z0-9_]{0,62}$`)
	envName     = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
	networkName = regexp.MustCompile(`^([A-Za-z0-9][A-Za-z0-9_.-]*)?$`)
	gpuValue    = regexp.MustCompile(`^[A-Za-z0-9=,:._/+-]*$`)
	runtimeName = regexp.MustCompile(`^[a-z0-9._-]*$`)
)

// FactName says whether name is a fact name a resource may report: lower-case letters, digits and
// '_', starting with a letter.
func FactName(name string) bool { return factName.MatchString(name) }

// CheckPath refuses a path a container cannot be given: one that is not absolute, or that holds a
// character that ends a field of docker's mount syntax.
func CheckPath(what, p string) error {
	switch {
	case p == "" || p[0] != '/':
		return invalid("%s must be an absolute path, got %q", what, p)
	case strings.ContainsAny(p, ":,\"\x00\n\r"):
		return invalid("%s %q holds a character docker cannot mount (one of : , \" or a line break)", what, p)
	}
	return nil
}

// Validate refuses an attachment no container can be given, naming what is wrong.
func (a Attachment) Validate() error {
	for i, m := range a.Mounts {
		if err := CheckPath(fmt.Sprintf("mount %d host path", i+1), m.Host); err != nil {
			return err
		}
		if err := CheckPath(fmt.Sprintf("mount %d container path", i+1), m.Container); err != nil {
			return err
		}
	}
	for _, env := range []map[string]string{a.Env, a.GPUEnv} {
		for _, k := range slices.Sorted(maps.Keys(env)) {
			if !envName.MatchString(k) || strings.ContainsAny(env[k], "\x00\n") {
				return invalid("the environment variable %q=%q is not valid", k, env[k])
			}
			if _, both := a.Env[k]; both && len(a.GPUEnv) > 0 && a.GPUEnv[k] != "" {
				return invalid("the environment variable %q is set with the GPUs and without them", k)
			}
		}
	}
	if len(a.GPUEnv) > 0 && !a.GPUs.Given() {
		return invalid("variables are set to pass GPUs, and no GPU is given")
	}
	if len(a.Check) > 0 && a.Check[0] == "" {
		return invalid("the check has no program")
	}
	switch {
	case !networkName.MatchString(a.Network):
		return invalid("%q is not a network mode", a.Network)
	case !gpuValue.MatchString(a.GPUs.Devices):
		return invalid("%q is not a --gpus value", a.GPUs.Devices)
	case !runtimeName.MatchString(a.GPUs.Runtime):
		return invalid("%q is not a container runtime name", a.GPUs.Runtime)
	case a.GPUs.Runtime != "" && a.GPUs.Devices == "":
		return invalid("the runtime %q is for GPUs, and no GPU is given", a.GPUs.Runtime)
	case a.GPUs.ROCm && a.GPUs.Devices != "":
		return invalid("the GPUs are passed both as AMD GPUs and with the --gpus value %q", a.GPUs.Devices)
	}
	return nil
}

// Combine joins the attachments of a placement's resources into one, and refuses two that ask
// for different values of one thing: one container path, one variable, the network or the GPUs.
func Combine(attachments []Attachment) (Attachment, error) {
	out := Attachment{Env: map[string]string{}, GPUEnv: map[string]string{}}
	targets := map[string]bool{}
	for _, a := range attachments {
		if err := a.Validate(); err != nil {
			return Attachment{}, err
		}
		for _, m := range a.Mounts {
			if targets[m.Container] {
				return Attachment{}, invalid("two resources mount %s", m.Container)
			}
			targets[m.Container] = true
			out.Mounts = append(out.Mounts, m)
		}
		for k, v := range a.Env {
			if old, ok := out.Env[k]; ok && old != v {
				return Attachment{}, invalid("two resources set %s, to %q and %q", k, old, v)
			}
			out.Env[k] = v
		}
		for k, v := range a.GPUEnv {
			if old, ok := out.GPUEnv[k]; ok && old != v {
				return Attachment{}, invalid("two resources set %s, to %q and %q", k, old, v)
			}
			out.GPUEnv[k] = v
		}
		if a.Network != "" {
			if out.Network != "" && out.Network != a.Network {
				return Attachment{}, invalid("two resources ask for the networks %q and %q", out.Network, a.Network)
			}
			out.Network = a.Network
		}
		if a.GPUs != (GPUs{}) {
			if out.GPUs != (GPUs{}) && out.GPUs != a.GPUs {
				return Attachment{}, invalid("two resources pass GPUs")
			}
			out.GPUs = a.GPUs
		}
		if len(a.Check) > 0 {
			if len(out.Check) > 0 {
				return Attachment{}, invalid("two resources check the container")
			}
			out.Check = slices.Clone(a.Check)
		}
	}
	return out, nil
}

func invalid(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: "resource: " + fmt.Sprintf(format, args...)}
}
