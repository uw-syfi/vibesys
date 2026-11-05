package docker

import (
	"context"
	"errors"
	"fmt"
	"io"
	"regexp"
	"strings"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// Engine is the Docker engine as the docker environment uses it. It is the environment's whole
// I/O, kept narrow so that a Fake can stand in for it and a test can make any call fail.
//
// Every method returns by a bound of its own and never blocks for ever on an engine that does
// not answer. It reports a failure to reach the engine, or one the engine gives that has no
// better kind, as a *core.Failure of kind FailureUnavailable that names what to do; a failure
// that is the caller's context ending is that context's error.
type Engine interface {
	// Image looks up a local image by tag or by ID. It reports false for an image that is not
	// there, which is not an error.
	Image(ctx context.Context, ref string) (Image, bool, error)

	// Build builds an image from a build context, a tar with the Dockerfile at its root, and tags
	// it. The build's output goes to log as it happens, and a build that fails says why in its
	// error, with the end of that output. Building the same context again may reuse the engine's
	// layers, and may produce a different image if the Dockerfile is not reproducible.
	Build(ctx context.Context, spec BuildSpec, log io.Writer) error

	// Containers returns the names of the containers, running or not, that carry every one of
	// the labels.
	Containers(ctx context.Context, match Labels) ([]string, error)

	// Remove removes the container and ends everything in it, running or not. A container that
	// is not there is removed.
	Remove(ctx context.Context, name string) error

	// Start creates the container and starts it, and returns once it is running. It is made to
	// stay up until it is removed, with nothing in it but what the caller starts through the
	// returned Container; when the caller dies, however it dies, the container stops by itself.
	// A container that failed to start may still exist, and Remove removes it.
	Start(ctx context.Context, spec ContainerSpec) (Container, error)
}

// Keeper is an Engine whose containers each wait for what they run in, such as a cluster's
// allocation, and that can keep it for the job's next container instead of giving it up, so that
// the next one does not wait again.
type Keeper interface {
	Engine
	// Keep removes the container and ends everything in it, as Remove does, and keeps what it ran
	// in for the next container this engine starts, when the container's spec said one follows
	// (ContainerSpec.Later) and what it ran in can serve it. What is kept and not taken is given up
	// by the engine within a bound of its own.
	Keep(ctx context.Context, name string) error
}

// Container is a running container, and the way into it.
type Container interface {
	// Start starts argv as a process in the container, as the user and in the working directory
	// of the spec the container was made from, with its standard output and standard error
	// written to sink as one stream in the order the process wrote them. The processes of one
	// container are the machine the command sees: they share its files and its network.
	//
	// A process whose program cannot be started in the container exits with 127, as a shell
	// does. The process ends when the container is removed. Its Exit reports the code, or an
	// Err that is ErrLost if the engine cannot say how it ended, such as a container lost under it.
	environment.Launcher

	// Port returns a TCP port of the container's network on which no process listens now, for a
	// server the run starts in the container. An engine whose containers have a network of their
	// own may return any port, and takes it from seed, the run's id, so that no two runs assume one
	// fixed port; an engine whose containers share a machine's network with other work reads which
	// ports are free there.
	Port(ctx context.Context, seed string) (int, error)

	// Close releases everything the engine holds for the container and waits for it, so that
	// nothing writes to a sink afterwards. It does not remove the container: one that still
	// exists stops by itself, because Close ends the caller's side of the container's input.
	Close()
}

// ErrLost is the Err of an Exit whose command the engine cannot say the end of: the container was
// lost under it, or the client that followed it was killed.
var ErrLost = errors.New("docker: the engine cannot say how the command ended")

// Lost is an Exit of a command whose end the engine cannot say, and why.
func Lost(why string) environment.Exit {
	return environment.Exit{Err: fmt.Errorf("%w: %s", ErrLost, why)}
}

// Labels are the labels of an image or a container.
type Labels map[string]string

// Image is a local image.
type Image struct {
	// ID is the image's exact digest, sha256: and 64 hexadecimal digits.
	ID string
	// Platform is the operating system and architecture the image is for, such as linux/amd64.
	Platform string
}

// imageID is the form of an exact image digest.
var imageID = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

// BuildSpec is an image to build.
type BuildSpec struct {
	// Tag names the image.
	Tag string
	// Context is the whole build input: a tar with the Dockerfile at its root, made by Context.
	Context []byte
	Labels  Labels
}

// Mount is a directory of the host made visible in the container.
type Mount struct {
	Host, Container string
	ReadOnly        bool
}

// User is the user and group a container's processes run as.
type User struct{ UID, GID int }

// GPUs says which GPUs a container gets, and how.
type GPUs struct {
	// Devices is a docker --gpus value of NVIDIA GPUs, such as "all" or "device=0,1". Empty passes
	// no NVIDIA GPU.
	Devices string
	// Runtime, when set, is the container runtime that passes them ("nvidia"), with the devices in
	// NVIDIA_VISIBLE_DEVICES, instead of --gpus.
	Runtime string
	// ROCm passes the machine's AMD GPUs through the ROCm kernel driver: its devices, /dev/kfd and
	// /dev/dri, and the video group that may use them. It is never set with Devices.
	ROCm bool
}

// ContainerSpec is a container to start.
type ContainerSpec struct {
	// Name is the container's unique name.
	Name string
	// Image is the exact image digest the container is made from.
	Image  string
	Labels Labels
	User   User
	// Dir is the working directory of every process.
	Dir    string
	Mounts []Mount
	// Env is set for every process, over the image's own environment.
	Env map[string]string
	// GPUs are the GPUs the container gets. The zero value gives it none.
	GPUs GPUs
	// Network is the container's network mode (docker run --network). Empty leaves the engine's
	// default. NetworkOf a container joins that container's network, as the command's container of
	// a task of two images joins its candidate's server's.
	Network string
	// Limit is the longest the container's commands may run. An engine that places the container
	// in an allocation with a time limit of its own bounds the allocation by it; zero means the
	// caller gave none.
	Limit time.Duration
	// Later is the summed Limit of the run's job's containers like this one that follow it
	// (core.Execution.Later). An engine that places the container in an allocation may ask for
	// one long enough for them too, and keep it for the next of them (Keeper); zero says none
	// follows.
	Later time.Duration
	// Measured says the container's commands are timed: an engine that places the container in
	// an allocation gives it the whole machine, and an unmeasured container only its share.
	Measured bool
	// Log, when set, is the run's output, where an engine that waits while it starts the container,
	// as for a cluster's queue, says why in whole lines (environment.Environment.Run). It writes
	// nothing there otherwise, and nothing once Start has returned.
	Log io.Writer
}

var (
	containerNamePattern = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$`)
	labelKeyPattern      = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]*$`)
	envNamePattern       = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
	gpuDevicesPattern    = regexp.MustCompile(`^[A-Za-z0-9=,:._-]*$`)
	runtimePattern       = regexp.MustCompile(`^[a-z0-9._-]*$`)
	networkPattern       = regexp.MustCompile(`^((container:)?[A-Za-z0-9][A-Za-z0-9_.-]*)?$`)
)

// Validate refuses a spec an engine would have to refuse or misread, with a *core.Failure of kind
// FailureInvalidInput that names the field. Every engine calls it first, so that the Fake is no
// more forgiving than the real one: the characters a path may not hold are the ones that end a
// field of docker's mount syntax.
func (s ContainerSpec) Validate() error {
	invalid := func(format string, args ...any) error {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: "container: " + fmt.Sprintf(format, args...)}
	}
	switch {
	case !containerNamePattern.MatchString(s.Name):
		return invalid("%q is not a container name", s.Name)
	case !imageID.MatchString(s.Image):
		return invalid("the image %q is not an exact digest (sha256:<64 hex digits>)", s.Image)
	case s.User.UID < 0 || s.User.GID < 0:
		return invalid("the user %d:%d is negative", s.User.UID, s.User.GID)
	case !gpuDevicesPattern.MatchString(s.GPUs.Devices):
		return invalid("%q is not a --gpus value", s.GPUs.Devices)
	case !runtimePattern.MatchString(s.GPUs.Runtime):
		return invalid("%q is not a container runtime name", s.GPUs.Runtime)
	case s.GPUs.Runtime != "" && s.GPUs.Devices == "":
		return invalid("the runtime %q is for GPUs, and no GPU is given", s.GPUs.Runtime)
	case s.GPUs.ROCm && s.GPUs.Devices != "":
		return invalid("the GPUs are passed both as AMD GPUs and with the --gpus value %q", s.GPUs.Devices)
	case !networkPattern.MatchString(s.Network):
		return invalid("%q is not a network mode", s.Network)
	case s.Limit < 0:
		return invalid("the limit %v is negative", s.Limit)
	case s.Later < 0:
		return invalid("the later containers' limit %v is negative", s.Later)
	}
	if err := checkPath("the working directory", s.Dir); err != nil {
		return invalid("%v", err)
	}
	for i, m := range s.Mounts {
		if err := checkPath(fmt.Sprintf("mount %d host path", i+1), m.Host); err != nil {
			return invalid("%v", err)
		}
		if err := checkPath(fmt.Sprintf("mount %d container path", i+1), m.Container); err != nil {
			return invalid("%v", err)
		}
	}
	for k, v := range s.Labels {
		if !labelKeyPattern.MatchString(k) || strings.ContainsAny(v, "\x00\n") {
			return invalid("the label %q=%q is not a valid label", k, v)
		}
	}
	for k, v := range s.Env {
		if !envNamePattern.MatchString(k) || strings.ContainsAny(v, "\x00\n") {
			return invalid("the environment variable %q=%q is not valid", k, v)
		}
	}
	return nil
}

// checkPath refuses a path that is not absolute, or that holds a character that ends a field of
// docker's mount syntax or cannot be a path.
func checkPath(what, p string) error {
	switch {
	case p == "" || p[0] != '/':
		return fmt.Errorf("%s must be an absolute path, got %q", what, p)
	case strings.ContainsAny(p, ":,\"\x00\n\r"):
		return fmt.Errorf("%s %q holds a character docker cannot mount (one of : , \" or a line break)", what, p)
	}
	return nil
}

// Source reads what a task asks of its image, from the repository at the commit. The checkout a
// job runs in does not exist yet when the fingerprint is taken, so the task's files are read from
// the repository and not from a working copy, which may differ from the commit.
type Source interface {
	// Spec returns the task's image spec. It fails with core.FailureNotFound if the task has no
	// Dockerfile at the commit, and core.FailureUnavailable if the repository cannot be read.
	Spec(ctx context.Context, target environment.Target) (Spec, error)
}

// Spec is what a task asks of the image it runs in.
type Spec struct {
	// Dockerfile is the text of the task's Dockerfile.
	Dockerfile []byte
	// Files are the other files of the task's directory, by their path from it: with the
	// Dockerfile they are the image's build context. A "Dockerfile" among them is replaced by the
	// Dockerfile above.
	Files task.Tree
	// Backend is the compute stack the task targets. Empty means cpu.
	Backend core.BackendKind
	// NestedDocker says the task starts containers of its own, which a container does not give.
	NestedDocker bool
	// Needs are the kinds of resource the task needs its placement to give the container.
	Needs []core.ResourceKind
}

// IDs makes the ids that name runs. Two calls never return the same id, in this process or
// another's.
type IDs interface {
	New() string
}
