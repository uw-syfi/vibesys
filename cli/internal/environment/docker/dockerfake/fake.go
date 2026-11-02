package dockerfake

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"maps"
	"slices"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
)

// Platform is the platform of every image the fake builds.
const Platform = "linux/amd64"

// Op names a call of the engine.
type Op string

// The calls a fault can fail.
const (
	OpImage      Op = "image"
	OpBuild      Op = "build"
	OpContainers Op = "containers"
	OpRemove     Op = "remove"
	OpStart      Op = "start"
	// OpExec is the start of a process in a container.
	OpExec Op = "exec"
)

// Ops lists every call a fault can fail.
var Ops = []Op{OpImage, OpBuild, OpContainers, OpRemove, OpStart, OpExec}

// Call is one call of the engine, as a fault sees it.
type Call struct {
	Op Op
	// Name is what the call is about: an image reference, a build's tag, a container's name, or
	// the labels of a listing, sorted.
	Name string
}

// Fault says how a call fails.
type Fault struct {
	Err error
	// Late says the call did its work before it failed, as when the engine's reply is lost: a
	// removed container is gone although Remove failed, a started container exists although
	// Start failed (and stopped, since the connection that held it is gone), a built image is
	// there although Build failed, and a started process runs although its start failed.
	Late bool
}

// Faults decides, for each call, whether it fails: it returns nil to let the call through.
type Faults func(Call) *Fault

// Daemon is an in-memory Docker daemon. Create it with NewDaemon. It is safe for concurrent use.
type Daemon struct {
	gates        fakeenv.Gates
	reproducible bool

	mu     sync.Mutex
	faults Faults
	images map[string]docker.Image // by tag
	builds int
	boxes  map[string]*box // containers, by name
}

// box is a container.
type box struct {
	spec    docker.ContainerSpec
	machine *fakeenv.Machine
	running bool
	owner   *Client // the connection that holds its input, or nil
}

// Option configures NewDaemon.
type Option func(*Daemon)

// NonReproducibleBuilds makes every build produce a different image, as a Dockerfile that
// downloads something that changes does. By default the same Dockerfile builds the same image.
func NonReproducibleBuilds() Option { return func(d *Daemon) { d.reproducible = false } }

// NewDaemon returns an empty daemon whose containers run scripts that wait at the gates.
func NewDaemon(gates fakeenv.Gates, opts ...Option) *Daemon {
	d := &Daemon{gates: gates, reproducible: true, images: map[string]docker.Image{}, boxes: map[string]*box{}}
	for _, opt := range opts {
		opt(d)
	}
	return d
}

// SetFaults sets the faults the daemon's calls are subject to, or none with nil.
func (d *Daemon) SetFaults(f Faults) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.faults = f
}

// Connect returns a new connection to the daemon: the engine of one process.
func (d *Daemon) Connect() *Client { return &Client{d: d} }

// Builds returns how many images the daemon has built.
func (d *Daemon) Builds() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.builds
}

// Prune removes every image, as an image prune does.
func (d *Daemon) Prune() {
	d.mu.Lock()
	defer d.mu.Unlock()
	clear(d.images)
}

// Info describes a container.
type Info struct {
	Name    string
	Running bool
	Spec    docker.ContainerSpec
}

// Containers returns every container the daemon has, running or not, sorted by name.
func (d *Daemon) Containers() []Info {
	d.mu.Lock()
	defer d.mu.Unlock()
	var out []Info
	for _, name := range slices.Sorted(maps.Keys(d.boxes)) {
		b := d.boxes[name]
		out = append(out, Info{Name: name, Running: b.running, Spec: b.spec})
	}
	return out
}

// Running returns the names of the running containers, sorted.
func (d *Daemon) Running() []string {
	var out []string
	for _, c := range d.Containers() {
		if c.Running {
			out = append(out, c.Name)
		}
	}
	return out
}

// Plant adds a container that nobody here started, running or not: a leftover of a process that
// died, or a container of someone else.
func (d *Daemon) Plant(name string, labels docker.Labels, running bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	spec := docker.ContainerSpec{Name: name, Labels: maps.Clone(labels), Dir: "/"}
	d.boxes[name] = &box{spec: spec, machine: fakeenv.NewMachine(d.gates, "/"), running: running}
}

func (d *Daemon) fault(c Call) *Fault {
	d.mu.Lock()
	f := d.faults
	d.mu.Unlock()
	if f == nil {
		return nil
	}
	return f(c)
}

// stop ends everything in the container and leaves it stopped, as its input closing does.
func (d *Daemon) stop(b *box) {
	d.mu.Lock()
	b.running = false
	b.owner = nil
	d.mu.Unlock()
	b.machine.End()
}

// remove ends the container and removes it.
func (d *Daemon) remove(name string) {
	d.mu.Lock()
	b := d.boxes[name]
	delete(d.boxes, name)
	d.mu.Unlock()
	if b != nil {
		d.stop(b)
	}
}

// Client is one process's connection to a Daemon, and a docker.Engine.
type Client struct {
	d *Daemon

	mu   sync.Mutex
	dead bool
	held []*box // containers whose input this connection holds
}

var _ docker.Engine = (*Client)(nil)

// Crash ends the connection as the death of its process does: the containers it started stop,
// and stay until something removes them. Later calls fail.
func (c *Client) Crash() {
	c.mu.Lock()
	c.dead = true
	held := c.held
	c.held = nil
	c.mu.Unlock()
	for _, b := range held {
		c.d.stop(b)
	}
}

func lost() error {
	return &core.Failure{Kind: core.FailureUnavailable, Message: "docker: the connection to the engine is gone"}
}

// enter starts a call: it fails if the connection is gone or ctx has ended, and returns the fault
// the call is subject to.
func (c *Client) enter(ctx context.Context, call Call) (*Fault, error) {
	c.mu.Lock()
	dead := c.dead
	c.mu.Unlock()
	if dead {
		return nil, lost()
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return c.d.fault(call), nil
}

// Image implements docker.Engine.
func (c *Client) Image(ctx context.Context, ref string) (docker.Image, bool, error) {
	fault, err := c.enter(ctx, Call{OpImage, ref})
	if err != nil {
		return docker.Image{}, false, err
	}
	if fault != nil {
		return docker.Image{}, false, fault.Err
	}
	c.d.mu.Lock()
	defer c.d.mu.Unlock()
	if img, ok := c.d.images[ref]; ok {
		return img, true, nil
	}
	for _, img := range c.d.images {
		if img.ID == ref {
			return img, true, nil
		}
	}
	return docker.Image{}, false, nil
}

// Build implements docker.Engine.
func (c *Client) Build(ctx context.Context, spec docker.BuildSpec, log io.Writer) error {
	fault, err := c.enter(ctx, Call{OpBuild, spec.Tag})
	if err != nil {
		return err
	}
	if fault != nil && !fault.Late {
		return fault.Err
	}
	if spec.Tag == "" || len(spec.Dockerfile) == 0 {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: "docker: a build needs a tag and a Dockerfile"}
	}
	c.d.mu.Lock()
	c.d.builds++
	seed := spec.Dockerfile
	if !c.d.reproducible {
		seed = fmt.Appendf(slices.Clone(spec.Dockerfile), "\nbuild %d", c.d.builds)
	}
	sum := sha256.Sum256(seed)
	c.d.images[spec.Tag] = docker.Image{ID: "sha256:" + hex.EncodeToString(sum[:]), Platform: Platform}
	c.d.mu.Unlock()
	_, _ = fmt.Fprintf(log, "built %s\n", spec.Tag)
	if fault != nil {
		return fault.Err
	}
	return nil
}

// Containers implements docker.Engine.
func (c *Client) Containers(ctx context.Context, match docker.Labels) ([]string, error) {
	fault, err := c.enter(ctx, Call{OpContainers, labelsKey(match)})
	if err != nil {
		return nil, err
	}
	if fault != nil {
		return nil, fault.Err
	}
	c.d.mu.Lock()
	defer c.d.mu.Unlock()
	var out []string
	for name, b := range c.d.boxes {
		if hasLabels(b.spec.Labels, match) {
			out = append(out, name)
		}
	}
	slices.Sort(out)
	return out, nil
}

func hasLabels(have, want docker.Labels) bool {
	for k, v := range want {
		if got, ok := have[k]; !ok || got != v {
			return false
		}
	}
	return true
}

func labelsKey(l docker.Labels) string {
	var parts []string
	for _, k := range slices.Sorted(maps.Keys(l)) {
		parts = append(parts, k+"="+l[k])
	}
	return fmt.Sprint(parts)
}

// Remove implements docker.Engine.
func (c *Client) Remove(ctx context.Context, name string) error {
	fault, err := c.enter(ctx, Call{OpRemove, name})
	if err != nil {
		return err
	}
	if fault != nil && !fault.Late {
		return fault.Err
	}
	c.d.remove(name)
	if fault != nil {
		return fault.Err
	}
	return nil
}

// Start implements docker.Engine.
func (c *Client) Start(ctx context.Context, spec docker.ContainerSpec) (docker.Container, error) {
	fault, err := c.enter(ctx, Call{OpStart, spec.Name})
	if err != nil {
		return nil, err
	}
	if fault != nil && !fault.Late {
		return nil, fault.Err
	}
	if err := spec.Validate(); err != nil {
		return nil, err
	}
	c.d.mu.Lock()
	known := false
	for _, img := range c.d.images {
		known = known || img.ID == spec.Image
	}
	_, taken := c.d.boxes[spec.Name]
	if !known || taken {
		c.d.mu.Unlock()
		if taken {
			return nil, &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("docker: the container name %q is already in use", spec.Name)}
		}
		return nil, &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("docker: no such image: %s", spec.Image)}
	}
	b := &box{spec: spec, machine: fakeenv.NewMachine(c.d.gates, spec.Dir), running: true, owner: c}
	b.spec.Labels = maps.Clone(spec.Labels)
	c.d.boxes[spec.Name] = b
	c.d.mu.Unlock()
	if fault != nil {
		// The reply was lost: the container exists, and with the caller gone it stops.
		c.d.stop(b)
		return nil, fault.Err
	}
	c.mu.Lock()
	c.held = append(c.held, b)
	c.mu.Unlock()
	return &handle{c: c, b: b}, nil
}

// handle is a started container, and a docker.Container.
type handle struct {
	c *Client
	b *box
}

var _ docker.Container = (*handle)(nil)

// Start implements environment.Launcher.
func (h *handle) Start(argv []string, sink io.Writer) (environment.Process, error) {
	fault, err := h.c.enter(context.Background(), Call{OpExec, h.b.spec.Name})
	if err != nil || (fault != nil && !fault.Late) {
		if err == nil {
			err = fault.Err
		}
		return nil, err
	}
	h.c.d.mu.Lock()
	running := h.b.running
	h.c.d.mu.Unlock()
	if !running {
		// The container was lost under the caller: an engine that cannot find it says nothing
		// about the command, which is how the process reports it.
		return newLostProcess(), nil
	}
	if fault != nil {
		// The process runs, but the caller never learns of it, and its output has nowhere to go.
		_, _ = h.b.machine.Start(argv, io.Discard)
		return nil, fault.Err
	}
	return h.b.machine.Start(argv, sink)
}

// Close implements docker.Container: the connection stops holding the container's input, so the
// container stops. It does not remove it.
func (h *handle) Close() {
	h.c.mu.Lock()
	h.c.held = slices.DeleteFunc(h.c.held, func(b *box) bool { return b == h.b })
	h.c.mu.Unlock()
	h.c.d.stop(h.b)
}

// lostProcess is the process of a container the engine could not find: it has already ended, and
// the engine cannot say how.
type lostProcess struct{ exited chan environment.Exit }

func newLostProcess() *lostProcess {
	p := &lostProcess{exited: make(chan environment.Exit, 1)}
	p.exited <- environment.Exit{Kind: core.ExecUnknown}
	return p
}

func (p *lostProcess) Exited() <-chan environment.Exit { return p.exited }

func (*lostProcess) Kill() {}
