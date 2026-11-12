package apptainer

import (
	"context"
	"errors"
	"fmt"
	"io"
	"maps"
	"path"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
)

// Defaults of what Config may leave out.
const (
	// DefaultPoll is the pause between two reads of a queued job's state.
	DefaultPoll = 5 * time.Second
	// DefaultQueueTimeout bounds the wait for an allocation.
	DefaultQueueTimeout = 24 * time.Hour
	// CallTimeout bounds a call that should answer at once, such as a signal to a box.
	CallTimeout = 2 * time.Minute
	// StateOutage is how long the reads of a queued allocation's state may keep failing before
	// the wait for it fails: a login node that refuses connections for a while, or a Slurm
	// controller that does not answer, says nothing of the allocation, which keeps its place.
	StateOutage = 15 * time.Minute
)

// DefaultConvertTimeout bounds the conversion of an image and the copy of its SIF when Config sets
// no bound.
const DefaultConvertTimeout = time.Hour

// StopMargin is added to a container's limit to make its allocation's time limit: the stop after
// the limit. The allocation is a backstop: the environment ends the commands first.
const StopMargin = 10 * time.Minute

// NoLimit is the time limit of an allocation whose container was given none.
const NoLimit = 24 * time.Hour

// Exporter writes a local image as a docker archive (docker save), says how large the image is
// unpacked and stored, as the local engine has it (stored, its layers are compressed under Docker's
// default containerd image store, about the size of its archive), and says what the image holds.
type Exporter interface {
	Save(ctx context.Context, image string, w io.Writer) error
	Sizes(ctx context.Context, image string) (unpacked, stored int64, err error)
	// Content returns the digest of what a container of the image runs, as "sha256:" and 64
	// lower-case hexadecimal digits: of its config (its environment, entrypoint, command, working
	// directory, user, labels and the rest), its platform, and its layers' diff IDs, in order. Two
	// images of one content have one digest whatever their IDs: under the containerd image store an
	// image's ID is the digest of its manifest or index, which differs from one build to the next of
	// the same Dockerfile and files when BuildKit attaches attestations to it.
	Content(ctx context.Context, image string) (string, error)
	// Export writes the files of a container of the image, as `docker export` does, a tar archive
	// of its root file system with every layer applied, streaming.
	Export(ctx context.Context, image string, w io.Writer) error
	// Env returns the image's environment, KEY=VALUE each, in the image's order.
	Env(ctx context.Context, image string) ([]string, error)
}

// Sizing is what an allocation asks for, by what its container runs.
type Sizing struct {
	Partition string
	// Gres is what a measured container's allocation asks for.
	Gres string
	// SharedGres is what an unmeasured container's allocation asks for instead of Gres, when set.
	SharedGres string
	// Exclusive gives a measured container's allocation the whole node.
	Exclusive bool
	// CPUs is how many CPUs an allocation that is not the whole node asks for; zero is 8.
	CPUs int
	// Memory, when set, is the memory such an allocation asks for, as Slurm writes a size.
	Memory string
	// Arguments are the operator's own options, after these.
	Arguments []string
}

// Options are the submit command's options of an allocation for a container that is measured or
// not.
func (s Sizing) Options(measured bool) []string {
	var out []string
	if s.Partition != "" {
		out = append(out, "--partition="+s.Partition)
	}
	gres := s.Gres
	if !measured && s.SharedGres != "" {
		gres = s.SharedGres
	}
	if gres != "" {
		out = append(out, "--gres="+gres)
	}
	if measured && s.Exclusive {
		out = append(out, "--exclusive")
	} else {
		cpus := s.CPUs
		if cpus <= 0 {
			cpus = 8
		}
		out = append(out, "--cpus-per-task="+strconv.Itoa(cpus))
		if s.Memory != "" {
			out = append(out, "--mem="+s.Memory)
		}
	}
	return append(out, s.Arguments...)
}

// Config configures New.
type Config struct {
	// Local is the Docker engine of the caller's machine, which builds the images and finds them.
	Local docker.Engine
	// Export writes the local engine's images as docker archives.
	Export Exporter
	// Cluster is the cluster the containers run on.
	Cluster Cluster
	// Root is the absolute directory of the shared file system that holds the images, the SIFs and
	// the readings of the nodes.
	Root string
	// Scope is the placement's: it names whose last reading of a node is kept (nodes/<scope>.json
	// under Root), and the reusable allocations, which every run of the placement claims.
	Scope string
	// Runs, when set, says which runs of the placement live, where its jobs run at once, each
	// job's runs in a scope of their own (their containers' docker.LabelScope) under Scope: an
	// allocation is commented with both (RunComment), and a run's sweep finds the allocations of
	// its own scope and those of every dead run of the placement, never a live one's. Without it,
	// one run at a time runs in a scope, and a run's sweep finds every allocation of its scope.
	Runs docker.Runs
	// Timers makes the engine's own waits: the pause between two reads of a queued job, the bound
	// on the queue, and the bound of a signal.
	Timers clock.Timers
	// Clock stamps the readings of the nodes.
	Clock clock.Clock
	// Poll is the pause between two reads of a queued job's state; zero is DefaultPoll.
	Poll time.Duration
	// QueueTimeout bounds the wait for an allocation; zero is DefaultQueueTimeout.
	QueueTimeout time.Duration
	// Sizing is what each allocation asks for.
	Sizing Sizing
	// NodeScripts are scripts each node runs with its facts, whose output Facts reads by name.
	NodeScripts map[string]string
	// Runtime is what runs the containers on the nodes, which says what an image becomes.
	Runtime Runtime
	// Converter converts the images to SIFs on this machine, under RuntimeApptainer.
	Converter Converter
	// LocalDir is the absolute directory of this machine a conversion writes in, under
	// RuntimeApptainer.
	LocalDir string
	// ConvertTimeout bounds an image's conversion and the copy of its SIF, or the stream of its
	// root file system and its squashfs; zero is DefaultConvertTimeout.
	ConvertTimeout time.Duration
	// Idle, when set, makes the allocations reusable: see Engine. It is how long one is kept
	// without a container before it ends itself. Zero gives each container an allocation of its
	// own, cancelled with it.
	Idle time.Duration
	// AllocationLimit is the time limit of a reusable allocation; zero is MaxTime, or NoLimit
	// without one. A container's allocation is never shorter than its own limit and StopMargin.
	AllocationLimit time.Duration
	// MaxTime, when set, is the longest time limit the cluster grants an allocation of the
	// placement, as a site's QOS or partition caps it, in whole minutes, as Slurm counts them. No
	// allocation asks for more: a container whose limit and StopMargin do not fit is refused before
	// anything is submitted, and one with no limit gets what fits. Zero is no cap.
	MaxTime time.Duration
}

// Engine is the docker.Engine of a cluster. Create it with New. It is safe for concurrent use.
//
// With Config.Idle, the allocations are reused. A container that has an allocation of its own
// claims (Node.Claim) a running reusable allocation of its scope, of any process, with its limit
// and StopMargin left, the one with the least time left first (a container with no limit claims
// the one with the most); with none to claim, it asks for a new one, of AllocationLimit or its own
// limit and StopMargin when longer, sized for a measured container, so any container may use it.
// No allocation asks for more than MaxTime. Its
// removal kills its processes, waits for none to be left, and releases the claim; the allocation
// then waits for the next claim, and ends itself after Idle without one. A claim is the
// allocation's alone, so no other container runs in it beside a measured one (but the container
// that joined it, of the same run), and a measured container holds the whole node as before. The
// allocations are not containers: Containers does not list them, and a run's sweep leaves them,
// since a claim clears what a dead one left.
type Engine struct {
	cfg Config

	mu      sync.Mutex
	boxes   map[string]*box // the containers this engine started, by name
	reading *Reading        // the last node this process read
	// survivors are the boxes of standing allocations whose processes a kill did not confirm gone,
	// by job and box name.
	survivors map[string]Survivor
}

// ErrSurvivors is what a kill of the processes of a container in a standing allocation wraps when it
// was not confirmed: the processes may still run in the allocation, beside the next job's. The error
// of Engine.Remove wraps it, and TakeSurvivors names each such box, so that a caller that can release
// the allocation (the daemon, through its Allocator) tests errors.Is(err, apptainer.ErrSurvivors) and
// does so, and no job runs beside the survivors.
var ErrSurvivors = errors.New("apptainer: processes of a container may survive in its standing allocation")

// Survivor is a box of a standing allocation whose processes were not confirmed killed.
type Survivor struct {
	// Job is the batch job of the allocation (alloc.Handle.Ref) and Box the container's name.
	Job, Box string
	Err      error // the failed kill, wrapping ErrSurvivors
}

// TakeSurvivors returns the boxes whose kill failed and was not made good since, and forgets them.
func (e *Engine) TakeSurvivors() []Survivor {
	e.mu.Lock()
	defer e.mu.Unlock()
	out := slices.SortedFunc(maps.Values(e.survivors), func(a, b Survivor) int { return strings.Compare(a.Job+"/"+a.Box, b.Job+"/"+b.Box) })
	e.survivors = map[string]Survivor{}
	return out
}

// reap kills the processes of the box of a standing allocation, confirmed, and records the box as a
// survivor when it was not.
func (e *Engine) reap(ctx context.Context, b *box) error {
	err := e.cfg.Cluster.Node(b.job).Reap(ctx, b.name)
	key := b.job + "/" + b.name
	e.mu.Lock()
	defer e.mu.Unlock()
	if err == nil {
		delete(e.survivors, key)
		return nil
	}
	err = fmt.Errorf("%w: killing the processes of %s: %w", ErrSurvivors, b.name, err)
	e.survivors[key] = Survivor{Job: b.job, Box: b.name, Err: err}
	return err
}

var _ docker.Engine = (*Engine)(nil)

// box is a container this engine started.
type box struct {
	name   string
	labels docker.Labels
	job    string // the allocation it runs in
	owner  bool   // the allocation is its own, or claimed for it
	// standing says the allocation is a standing one (standing.go), the run's and not the
	// container's: the container's removal kills its processes and leaves it.
	standing bool
	pool     string // the name of the reusable allocation claimed for it, when it is one
	spec     docker.ContainerSpec
	env      map[string]string // what its processes get: the image's environment, then the spec's
	sif      string
	hold     io.Closer
	gone     bool // removed or closed: its processes end with it
}

// New returns an engine, or a core.FailureInvalidInput naming what is missing.
func New(cfg Config) (*Engine, error) {
	missing := func(what string) error {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: "apptainer: the config has no " + what}
	}
	switch {
	case cfg.Local == nil:
		return nil, missing("Local")
	case cfg.Export == nil:
		return nil, missing("Export")
	case cfg.Cluster == nil:
		return nil, missing("Cluster")
	case cfg.Timers == nil:
		return nil, missing("Timers")
	case cfg.Clock == nil:
		return nil, missing("Clock")
	case !path.IsAbs(cfg.Root) || path.Clean(cfg.Root) == "/":
		return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("apptainer: the root %q is not an absolute directory other than /", cfg.Root)}
	}
	if err := checkScripts(cfg.NodeScripts); err != nil {
		return nil, err
	}
	if cfg.Poll <= 0 {
		cfg.Poll = DefaultPoll
	}
	if cfg.QueueTimeout <= 0 {
		cfg.QueueTimeout = DefaultQueueTimeout
	}
	if cfg.ConvertTimeout <= 0 {
		cfg.ConvertTimeout = DefaultConvertTimeout
	}
	switch {
	case cfg.MaxTime < 0 || cfg.MaxTime%time.Minute != 0:
		return nil, invalid("the longest allocation, %v, is not a whole number of minutes, as Slurm counts a time limit", cfg.MaxTime)
	case cfg.MaxTime > 0 && cfg.MaxTime <= StopMargin:
		return nil, invalid("the longest allocation, %v, leaves a container no time beyond the %v that stops it", cfg.MaxTime, StopMargin)
	case cfg.MaxTime > 0 && cfg.AllocationLimit > cfg.MaxTime:
		return nil, invalid("the reused allocations' limit, %v, is longer than the longest allocation, %v", cfg.AllocationLimit, cfg.MaxTime)
	}
	switch {
	case cfg.Runtime != RuntimeApptainer && cfg.Runtime != RuntimePyxis:
		return nil, invalid("the runtime %d is not one this engine runs", cfg.Runtime)
	case cfg.Runtime == RuntimePyxis:
		// The login node makes the squashfs: nothing is converted on this machine.
	case cfg.Converter == nil:
		return nil, missing("Converter")
	case !path.IsAbs(cfg.LocalDir) || path.Clean(cfg.LocalDir) == "/":
		return nil, invalid("the local conversion directory %q is not an absolute directory other than /", cfg.LocalDir)
	}
	return &Engine{cfg: cfg, boxes: map[string]*box{}, survivors: map[string]Survivor{}}, nil
}

// JobName is the name of the allocation of the container with the name.
func JobName(container string) string { return JobPrefix + container }

// JobPrefix starts the name of every allocation this package asks for.
const JobPrefix = "vibesys-apptainer-"

// PoolPrefix starts the name of every reusable allocation, which is named by the container it was
// asked for first.
const PoolPrefix = JobPrefix + "pool-"

func unavailable(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureUnavailable, Message: "apptainer: " + fmt.Sprintf(format, args...)}
}

func invalid(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: "apptainer: " + fmt.Sprintf(format, args...)}
}

// Image implements docker.Engine: the images are the local engine's.
func (e *Engine) Image(ctx context.Context, ref string) (docker.Image, bool, error) {
	return e.cfg.Local.Image(ctx, ref)
}

// Build implements docker.Engine: the local engine builds, and the image is converted to a SIF and
// copied to the cluster as part of its build, so that the build's log says what converted it.
func (e *Engine) Build(ctx context.Context, spec docker.BuildSpec, log io.Writer) error {
	if err := e.cfg.Local.Build(ctx, spec, log); err != nil {
		return err
	}
	img, ok, err := e.cfg.Local.Image(ctx, spec.Tag)
	if err != nil || !ok {
		// The environment finds the image missing, and says so.
		return err
	}
	_, err = e.publish(ctx, img.ID, log)
	return err
}

// Containers implements docker.Engine: the containers of this engine that carry the labels, and,
// with a scope among them, the scope's allocations, which are what a run of another process
// started. With Runs, those are the allocations of the scope's own runs, live or dead, and those of
// the placement's other runs that are dead; asking for them holds the scope's run (Runs.Hold),
// since a run's sweep comes before anything it leaves.
func (e *Engine) Containers(ctx context.Context, match docker.Labels) ([]string, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	var names []string
	e.mu.Lock()
	for name, b := range e.boxes {
		if !b.gone && hasLabels(b.labels, match) {
			names = append(names, name)
		}
	}
	e.mu.Unlock()
	if scope, ok := match[docker.LabelScope]; ok {
		if err := e.hold(ctx, scope); err != nil {
			return nil, err
		}
		placement := e.placementScope(scope)
		jobs, err := e.cfg.Cluster.Jobs(ctx, placement)
		if err != nil {
			return nil, err
		}
		for _, job := range jobs {
			if strings.HasPrefix(job.Name, PoolPrefix) {
				// A reusable allocation is not a container: a claim clears what it ran.
				continue
			}
			if strings.HasPrefix(job.Name, StandingPrefix) {
				// A standing allocation is a run's, whether the run lives or not: only its Allocator
				// and its time limit end it, never a sweep.
				continue
			}
			name, ok := strings.CutPrefix(job.Name, JobPrefix)
			if !ok || slices.Contains(names, name) {
				continue
			}
			run, _ := InScope(job.Comment, placement)
			if run != scope && !e.dead(run) {
				continue
			}
			names = append(names, name)
		}
	}
	slices.Sort(names)
	return names, nil
}

// placementScope is the scope of the placement whose run's scope is scope: Config.Scope with Runs,
// and the run's own without, where a run's scope is the placement's.
func (e *Engine) placementScope(scope string) string {
	if e.cfg.Runs == nil || e.cfg.Scope == "" {
		return scope
	}
	return e.cfg.Scope
}

// hold holds the run of the scope, with Runs, before it leaves anything on the cluster.
func (e *Engine) hold(ctx context.Context, scope string) error {
	if e.cfg.Runs == nil || e.cfg.Scope == "" {
		return nil
	}
	if err := e.cfg.Runs.Hold(ctx, scope); err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return unavailable("cannot mark the run %s live: %v", scope, err)
	}
	return nil
}

// dead reports whether the run of the placement's scope is dead, so that what it left is a
// leftover. Without Runs every run of a scope but the caller's is. A run whose scope cannot be
// told is not taken to be dead: its allocation is left alone. The run is not held while its
// allocation is removed: an allocation is named by its container, which no later run reuses.
func (e *Engine) dead(run string) bool {
	if e.cfg.Runs == nil || e.cfg.Scope == "" {
		return true
	}
	release, dead, err := e.cfg.Runs.Claim(run)
	if err != nil || !dead {
		return false
	}
	release()
	return true
}

func hasLabels(have, want docker.Labels) bool {
	for k, v := range want {
		if got, ok := have[k]; !ok || got != v {
			return false
		}
	}
	return true
}

// Remove implements docker.Engine. A container that joined another's allocation has its processes
// killed; any other has its allocation cancelled, confirmed, which ends everything in it. A
// container this process did not start is the allocation of its name.
func (e *Engine) Remove(ctx context.Context, name string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if strings.HasPrefix(JobName(name), StandingPrefix) {
		return invalid("%s is a standing allocation, which only its Allocator releases", JobName(name))
	}
	e.mu.Lock()
	b := e.boxes[name]
	var owner *box
	if b != nil && !b.owner {
		owner = e.ownerOf(b)
	}
	var pool string
	var hold io.Closer
	if b != nil {
		pool, hold = b.pool, b.hold
	}
	e.mu.Unlock()
	if pool != "" {
		return e.release(ctx, b, pool, hold)
	}
	if b != nil && b.standing {
		// The allocation is the run's: the box's processes are killed, confirmed, and it runs on.
		if err := e.reap(ctx, b); err != nil {
			return err
		}
		e.forget(b)
		return nil
	}
	if b != nil && !b.owner {
		if owner != nil && b.job != "" {
			if err := e.cfg.Cluster.Node(b.job).Signal(ctx, name, SIGKILL); err != nil {
				return fmt.Errorf("killing the processes of %s: %w", name, err)
			}
		}
		e.forget(b)
		return nil
	}
	if err := e.cfg.Cluster.Cancel(ctx, JobName(name)); err != nil {
		return fmt.Errorf("the allocation of %s was not confirmed ended: %w", name, err)
	}
	if b != nil {
		e.forget(b)
	}
	return nil
}

// release releases the claim of a reusable allocation of the box, which kills its processes and
// waits for none to be left. A release that is not confirmed cancels the allocation, confirmed, so
// that nothing of the box is left in an allocation another container claims.
func (e *Engine) release(ctx context.Context, b *box, pool string, hold io.Closer) error {
	var err error
	if hold != nil {
		err = hold.Close()
	}
	if hold == nil || err != nil {
		if cerr := e.cfg.Cluster.Cancel(ctx, pool); cerr != nil {
			return fmt.Errorf("the allocation %s of %s was neither released (%v) nor confirmed ended: %w", pool, b.name, err, cerr)
		}
	}
	e.forget(b)
	return nil
}

// ownerOf is the live container whose allocation b joined. e.mu is held.
func (e *Engine) ownerOf(b *box) *box {
	for _, o := range e.boxes {
		if o.owner && o.job == b.job && !o.gone {
			return o
		}
	}
	return nil
}

// forget drops a container that is gone: its processes end with it.
func (e *Engine) forget(b *box) {
	e.mu.Lock()
	defer e.mu.Unlock()
	b.gone = true
	if e.boxes[b.name] == b {
		delete(e.boxes, b.name)
	}
}

// Start implements docker.Engine. A container whose network is another's joins that one's
// allocation; any other asks for an allocation of its own and waits for it, which may take as long
// as the queue does, up to QueueTimeout. Its image is converted to a SIF on this machine and copied
// to the cluster first, unless the cluster has the SIF.
func (e *Engine) Start(ctx context.Context, spec docker.ContainerSpec) (docker.Container, error) {
	if err := spec.Validate(); err != nil {
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if spec.GPUs.Devices != "" {
		return nil, invalid("the container %s is given GPUs by device (--gpus %s), and a node of this placement gives only AMD GPUs, with a rocm resource", spec.Name, spec.GPUs.Devices)
	}
	standing := spec.Labels[LabelAllocation]
	joined, joins := strings.CutPrefix(spec.Network, "container:")
	if !joins && spec.Network != "" && spec.Network != "host" {
		return nil, invalid("the container %s asks for the network %q, and a container on a cluster's node has the node's network", spec.Name, spec.Network)
	}
	if standing != "" && strings.Trim(standing, "0123456789") != "" {
		return nil, invalid("the container %s names the standing allocation %q, which is not a batch job's id", spec.Name, standing)
	}
	if !joins && standing == "" {
		if err := e.fits(spec); err != nil {
			return nil, err
		}
	}
	b := &box{name: spec.Name, labels: maps.Clone(spec.Labels), spec: spec, owner: !joins && standing == "", standing: !joins && standing != ""}
	if b.standing {
		b.job = standing
	}
	e.mu.Lock()
	if _, taken := e.boxes[spec.Name]; taken {
		e.mu.Unlock()
		return nil, &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("apptainer: the container name %q is already in use", spec.Name)}
	}
	if joins {
		o := e.boxes[joined]
		if o == nil || o.gone || !(o.owner || o.standing) {
			e.mu.Unlock()
			return nil, unavailable("cannot join the allocation of container %q, which is not running", joined)
		}
		b.job, b.standing = o.job, o.standing
	}
	e.boxes[spec.Name] = b
	e.mu.Unlock()

	// The image was converted as part of its build, whose log says so; this is the rare start of an
	// image no build of this process made, and the run's log carries nothing but the wait below.
	sif, err := e.publish(ctx, spec.Image, io.Discard)
	if err != nil {
		e.forget(b)
		return nil, err
	}
	imageEnv, err := e.cfg.Export.Env(ctx, spec.Image)
	if err != nil {
		e.forget(b)
		return nil, fmt.Errorf("reading the environment of the image %s: %w", spec.Image, err)
	}
	e.mu.Lock()
	b.env = BoxEnv(imageEnv, spec.Env)
	e.mu.Unlock()
	log := spec.Log
	if log == nil {
		log = io.Discard
	}
	if b.standing {
		if err := e.enter(ctx, b, log); err != nil {
			e.forget(b)
			return nil, err
		}
	} else if !joins {
		allocate := e.allocate
		if e.cfg.Idle > 0 {
			allocate = e.claim
		}
		if err := allocate(ctx, b, log); err != nil {
			// The allocation may exist: the box stays known, so Remove cancels it.
			return nil, err
		}
	}
	node := e.cfg.Cluster.Node(b.job)
	e.mu.Lock()
	b.sif = sif
	e.mu.Unlock()
	return &handle{e: e, b: b, node: node}, nil
}

// enter checks that the standing allocation the box runs in runs, and reads its node. A job that
// is not running starts nothing, so the container is refused, and the allocation is left as it is.
func (e *Engine) enter(ctx context.Context, b *box, log io.Writer) error {
	st, err := e.cfg.Cluster.State(ctx, b.job)
	if err != nil {
		return err
	}
	if st.Phase != Running {
		return unavailable("the standing allocation %s does not run (Slurm says %s), so the container %s cannot run in it", b.job, orUnknown(st.Name), b.name)
	}
	_, _ = fmt.Fprintf(log, "[apptainer] the container %s runs in the standing allocation %s on %s\n", b.name, b.job, orUnknown(st.Node))
	return e.read(ctx, e.cfg.Cluster.Node(b.job))
}

// allocate asks for the box's allocation, waits for it to run, saying to log what Slurm says of the
// wait, holds it for this process, and reads its node.
func (e *Engine) allocate(ctx context.Context, b *box, log io.Writer) error {
	limit := e.need(b.spec)
	if limit == 0 {
		limit = e.capped(NoLimit + StopMargin)
	}
	scope := b.labels[docker.LabelScope]
	if err := e.hold(ctx, scope); err != nil {
		return err
	}
	job, err := e.cfg.Cluster.Submit(ctx, Allocation{
		Name: JobName(b.name), Comment: RunComment(e.placementScope(scope), scope), Limit: limit,
		Options: e.cfg.Sizing.Options(b.spec.Measured),
	})
	if err != nil {
		return err
	}
	e.mu.Lock()
	b.job = job
	e.mu.Unlock()
	if err := e.await(ctx, job, log); err != nil {
		return err
	}
	node := e.cfg.Cluster.Node(job)
	hold, err := node.Hold(ctx)
	if err != nil {
		return err
	}
	e.mu.Lock()
	b.hold = hold
	e.mu.Unlock()
	return e.read(ctx, node)
}

// need is the time limit an allocation of a container of the spec needs, its limit and
// StopMargin, which MaxTime leaves room for (Start refuses the container otherwise); zero for a
// container with no limit, whose allocation is as long as it is asked for.
func (e *Engine) need(spec docker.ContainerSpec) time.Duration {
	if spec.Limit <= 0 {
		return 0
	}
	return spec.Limit + StopMargin
}

// capped is the time limit d, at most MaxTime.
func (e *Engine) capped(d time.Duration) time.Duration {
	if e.cfg.MaxTime > 0 {
		return min(d, e.cfg.MaxTime)
	}
	return d
}

// fits refuses a container that asks for an allocation of its own whose limit and StopMargin are
// longer than MaxTime, which the cluster would refuse to submit.
func (e *Engine) fits(spec docker.ContainerSpec) error {
	need := e.need(spec)
	if e.cfg.MaxTime <= 0 || need <= e.cfg.MaxTime {
		return nil
	}
	return invalid("the container %s may run for %v and is stopped %v after that, so its allocation needs %v, "+
		"longer than the placement's longest allocation, [slurm] max_time_seconds = %d (%v); "+
		"lower the command's timeout_seconds, or raise max_time_seconds if the cluster allows it",
		spec.Name, spec.Limit, StopMargin, need, int64(e.cfg.MaxTime/time.Second), e.cfg.MaxTime)
}

// claim claims a reusable allocation for the box, of those that run with the time it needs left,
// or asks for a new one, waits for it, and claims it. A new one that fails to run, or whose claim
// fails, is cancelled. A new one that another container of the placement claimed first (it found
// it running in the listing) is that container's: it is left to it, ended by its idle release
// later, and the box looks again, for a running one to claim or a new one to ask for. Each new one
// the box asks for has a name of its own (PoolPrefix, the box's name, and from the second on "+"
// and its count), since an allocation is cancelled by its name: a cancel of the box's next one
// never ends the one another container holds.
func (e *Engine) claim(ctx context.Context, b *box, log io.Writer) error {
	for n := 1; ; n++ {
		taken, err := e.claimOnce(ctx, b, n, log)
		if !taken {
			return err
		}
		if err := ctx.Err(); err != nil {
			return err
		}
	}
}

// claimOnce is claim's n-th look. It reports taken when the allocation it asked for was claimed
// by another container first, and the box has none.
func (e *Engine) claimOnce(ctx context.Context, b *box, n int, log io.Writer) (taken bool, _ error) {
	need := e.need(b.spec)
	// The reusable allocations are the placement's, claimed by any of its runs.
	scope := e.placementScope(b.labels[docker.LabelScope])
	jobs, err := e.cfg.Cluster.Jobs(ctx, scope)
	if err != nil {
		return false, err
	}
	jobs = slices.DeleteFunc(jobs, func(j Job) bool {
		return !strings.HasPrefix(j.Name, PoolPrefix) || j.Comment != scope || j.Phase != Running || j.Left < max(need, StopMargin)
	})
	if need > 0 {
		// The one with the least time left first, which keeps the longer ones for longer limits.
		slices.SortStableFunc(jobs, func(a, b Job) int { return int((a.Left - b.Left) / time.Second) })
	} else {
		// A container with no limit of its own runs as long as its allocation does: the longest.
		slices.SortStableFunc(jobs, func(a, b Job) int { return int((b.Left - a.Left) / time.Second) })
	}
	for _, j := range jobs {
		node := e.cfg.Cluster.Node(j.ID)
		hold, err := node.Claim(ctx)
		if errors.Is(err, ErrTaken) {
			continue
		}
		if err != nil {
			if ctx.Err() != nil {
				return false, ctx.Err()
			}
			_, _ = fmt.Fprintf(log, "[apptainer] the allocation %s could not be claimed, so it is passed over: %v\n", j.ID, err)
			continue
		}
		e.mu.Lock()
		b.job, b.pool, b.hold = j.ID, j.Name, hold
		e.mu.Unlock()
		return false, e.read(ctx, node)
	}
	allocation := e.cfg.AllocationLimit
	if allocation <= 0 {
		allocation = e.capped(NoLimit)
	}
	name := PoolPrefix + b.name
	if n > 1 {
		name += "+" + strconv.Itoa(n)
	}
	job, err := e.cfg.Cluster.Submit(ctx, Allocation{
		Name: name, Comment: scope, Limit: max(allocation, need), Options: e.cfg.Sizing.Options(true), Idle: e.cfg.Idle,
	})
	if err != nil {
		return false, err
	}
	e.mu.Lock()
	b.job, b.pool = job, name
	e.mu.Unlock()
	node := e.cfg.Cluster.Node(job)
	err = e.await(ctx, job, log)
	var hold io.Closer
	if err == nil {
		hold, err = node.Claim(ctx)
		if errors.Is(err, ErrTaken) {
			// Another container of the placement found it running and claimed it first: it is that
			// container's now, and Remove of this box must not cancel it.
			e.mu.Lock()
			b.job, b.pool = "", ""
			e.mu.Unlock()
			_, _ = fmt.Fprintf(log, "[apptainer] the allocation %s was claimed by another container first (or is ending), "+
				"so it is left to that one and this one looks again\n", job)
			return true, nil
		}
	}
	if err != nil {
		// No container uses it: what Remove would cancel, cancelled now.
		cancel, stop := clock.Deadline(context.WithoutCancel(ctx), e.cfg.Timers, CallTimeout)
		_ = e.cfg.Cluster.Cancel(cancel, name)
		stop()
		return false, err
	}
	e.mu.Lock()
	b.hold = hold
	e.mu.Unlock()
	return false, e.read(ctx, node)
}

// await waits for the job to run, pausing between reads of its state, up to QueueTimeout. When
// the job waits in the queue, what Slurm says of it (its state, its reason and its estimate of the
// start) goes to log when the wait begins and whenever it changes, and its node and the length of
// the wait once it runs, so that the run's log says why nothing ran yet. A job that runs at once
// writes nothing.
//
// A read of the state that fails as unavailable (the login node, or Slurm, did not answer) is no
// news of the job, which still waits or runs where it is: the reads go on, the first failure said to
// log, and only reads that keep failing for StateOutage fail the wait. A job that waited hours in
// the queue is not cancelled because the login node refused connections for a minute.
func (e *Engine) await(ctx context.Context, job string, log io.Writer) error {
	limit, stop := e.cfg.Timers.Timeout(ctx, e.cfg.QueueTimeout)
	defer stop()
	since := e.cfg.Clock.Now()
	var said *JobState
	var outage <-chan struct{} // fires once the reads have failed for StateOutage
	stopOutage := func() {}
	defer func() { stopOutage() }()
	for {
		st, err := e.cfg.Cluster.State(ctx, job)
		switch {
		case err != nil && (ctx.Err() != nil || core.AsFailure(err).Kind != core.FailureUnavailable):
			return err
		case err != nil:
			if outage == nil {
				_, _ = fmt.Fprintf(log, "[apptainer] the state of the allocation %s could not be read (%v); "+
					"it is read again every %v, for up to %v\n", job, err, e.cfg.Poll, StateOutage)
				outage, stopOutage = e.cfg.Timers.Timeout(ctx, StateOutage)
			}
			// What was said of the job stands, and the wait goes on.
			if said != nil {
				st = *said
			} else {
				st = JobState{Phase: Queued}
			}
		case outage != nil:
			stopOutage()
			outage, stopOutage = nil, func() {}
			_, _ = fmt.Fprintf(log, "[apptainer] the state of the allocation %s is read again\n", job)
		}
		if err == nil {
			switch st.Phase {
			case Running:
				if said != nil {
					_, _ = fmt.Fprintf(log, "[apptainer] the allocation %s runs on %s, after %v in Slurm's queue\n",
						job, orUnknown(st.Node), e.cfg.Clock.Now().Sub(since).Round(time.Second))
				}
				return nil
			case Queued:
				if said == nil || st.Name != said.Name || st.Reason != said.Reason || st.Start != said.Start {
					_, _ = fmt.Fprintf(log, "[apptainer] the allocation %s waits in Slurm's queue: %s\n", job, queued(st))
					said = &st
				}
			case Suspended:
				// It ran and is suspended: its caller waits for it, as for a queued one, until the queue timeout.
			case Ended:
				return unavailable("the allocation %s ended before it ran (Slurm says %s)", job, orUnknown(st.Name))
			}
		}
		pause, stopPause := e.cfg.Timers.Timeout(ctx, e.cfg.Poll)
		select {
		case <-pause:
			stopPause()
		case <-outage:
			stopPause()
			return unavailable("the state of the allocation %s could not be read for %v, so it is cancelled; the last read: %v", job, StateOutage, err)
		case <-limit:
			stopPause()
			why := orUnknown(st.Name)
			if st.Reason != "" {
				why += ", " + st.Reason
			}
			return unavailable("the allocation %s did not start within %v (Slurm says %s); it is cancelled", job, e.cfg.QueueTimeout, why)
		case <-ctx.Done():
			stopPause()
			return ctx.Err()
		}
	}
}

// queued says what Slurm says of a queued job, for a person.
func queued(st JobState) string {
	out := orUnknown(st.Name)
	if st.Reason != "" {
		out += " (" + st.Reason + ")"
	}
	if st.Start != "" {
		out += "; Slurm expects it to start at " + st.Start
	} else {
		out += "; Slurm gives no estimate of its start"
	}
	return out
}

func orUnknown(s string) string {
	if s == "" {
		return "nothing of it"
	}
	return s
}

// sifPath is where the cluster keeps the image of an image's content, named by the content's
// digest (Exporter.Content): its SIF, or its squashfs under RuntimePyxis. It is not named by the
// image's ID, which two builds of one content may not share; a SIF named by an ID, as earlier
// versions named them, is used by nothing.
func (e *Engine) sifPath(content string) string {
	ext := ".sif"
	if e.cfg.Runtime == RuntimePyxis {
		ext = ".sqsh"
	}
	return path.Join(e.cfg.Root, "images", strings.TrimPrefix(content, "sha256:")+ext)
}

// contentOf is the digest of the image's content, checked to be one, since it names files.
func (e *Engine) contentOf(ctx context.Context, image string) (string, error) {
	content, err := e.cfg.Export.Content(ctx, image)
	if err != nil {
		return "", fmt.Errorf("reading the content of the image %s: %w", image, err)
	}
	hexPart, ok := strings.CutPrefix(content, "sha256:")
	if !ok || len(hexPart) != 64 || strings.Trim(hexPart, "0123456789abcdef") != "" {
		return "", unavailable("the digest of the content of the image %s, %q, is not sha256: and 64 hexadecimal digits", image, content)
	}
	return content, nil
}

// BuiltBy is the suffix of the file beside a SIF that says what converted it, in one line for a
// person.
const BuiltBy = ".built-by"

// CopyAttempts bounds the copies of a SIF to the cluster that each lose their connection to it.
const CopyAttempts = 3

// publish returns the image's SIF on the cluster, converting the image on this machine and copying
// the SIF there unless the cluster has a SIF of the image's content: of this image, or of another
// build of the same content.
//
// The publishers of one image on this machine, in this process or others, convert it one at a
// time, under the image's lock of the Converter: one that finds the lock held says so to log and
// waits for it, then uses the SIF the holder published, or carries on from where the holder
// stopped. Publishers on different machines each convert the image, and the cluster keeps one of
// their SIFs, whole.
//
// The conversion and the copy are two steps, bounded together by ConvertTimeout. The conversion
// keeps the SIF in the local directory, so a copy is made only once there is a SIF to send, and a
// copy that fails does not lose the conversion: a copy that loses its connection to the cluster is
// made again, up to CopyAttempts, and a later publisher of the image copies the SIF kept here
// instead of converting the image again. The kept SIF is removed once the cluster has it, or by
// the next conversion of this machine once it is a day old.
func (e *Engine) publish(ctx context.Context, image string, log io.Writer) (string, error) {
	content, err := e.contentOf(ctx, image)
	if err != nil {
		return "", err
	}
	sif := e.sifPath(content)
	has, err := e.cfg.Cluster.Has(ctx, sif)
	if err != nil || has {
		return sif, err
	}
	if e.cfg.Runtime == RuntimePyxis {
		return sif, e.squash(ctx, image, content, sif, log)
	}
	lock, err := e.lockConversion(ctx, image, content, log)
	if err != nil {
		return "", err
	}
	defer func() { _ = lock.Close() }()
	kept := path.Base(sif)
	// A holder this publisher waited for may have published the SIF.
	if has, err := e.cfg.Cluster.Has(ctx, sif); err != nil || has {
		if has {
			e.evict(ctx, image, kept, log)
		}
		return sif, err
	}
	ctx, stop := clock.Deadline(ctx, e.cfg.Timers, e.cfg.ConvertTimeout)
	defer stop()
	if err := e.convert(ctx, image, sif, log); err != nil {
		// Every step runs under the bound, so any of them may be the one it ends, and each then
		// fails with context.Canceled, as the bound's context cannot report otherwise.
		if errors.Is(context.Cause(ctx), context.DeadlineExceeded) {
			return "", unavailable("converting the image %s to a SIF on this machine and copying it did not end within %v "+
				"([apptainer] convert_timeout_seconds)", image, e.cfg.ConvertTimeout)
		}
		return "", err
	}
	return sif, nil
}

// squash makes the image's squashfs at the path on the cluster from its root file system, streamed
// to the login node as it is exported, bounded by ConvertTimeout: nothing of it is kept on this
// machine, and a squash that fails leaves nothing there, for the next publisher to make again.
func (e *Engine) squash(ctx context.Context, image, content, sqsh string, log io.Writer) error {
	ctx, stop := clock.Deadline(ctx, e.cfg.Timers, e.cfg.ConvertTimeout)
	defer stop()
	by, err := e.squashOnce(ctx, image, content, sqsh, log)
	if err == nil {
		err = e.copy(ctx, sqsh+BuiltBy, func(context.Context) (io.ReadCloser, error) {
			return io.NopCloser(strings.NewReader(by + ", from docker export\n")), nil
		}, log)
	}
	if err != nil {
		if errors.Is(context.Cause(ctx), context.DeadlineExceeded) {
			return unavailable("streaming the image %s to the login node and making its squashfs did not end within %v "+
				"([apptainer] convert_timeout_seconds)", image, e.cfg.ConvertTimeout)
		}
		return err
	}
	_, _ = fmt.Fprintf(log, "[apptainer] %s is %s, made by %s\n", image, sqsh, by)
	return nil
}

func (e *Engine) squashOnce(ctx context.Context, image, content, sqsh string, log io.Writer) (string, error) {
	env, err := e.cfg.Export.Env(ctx, image)
	if err != nil {
		return "", fmt.Errorf("reading the environment of the image %s: %w", image, err)
	}
	_, _ = fmt.Fprintf(log, "[apptainer] streaming the files of %s to the login node, which makes its squashfs %s\n", image, sqsh)
	exported, exportW := io.Pipe()
	rootfs, rootfsW := io.Pipe()
	var wg sync.WaitGroup
	wg.Go(func() {
		err := e.cfg.Export.Export(ctx, image, exportW)
		if err != nil {
			err = fmt.Errorf("exporting the files of the image %s: %w", image, err)
		}
		exportW.CloseWithError(err)
	})
	wg.Go(func() {
		err := RootFS(exported, env, content, rootfsW)
		// A RootFS that stopped reading leaves the export nothing to write to.
		exported.CloseWithError(errors.New("apptainer: the root file system's stream ended"))
		rootfsW.CloseWithError(err)
	})
	by, err := e.cfg.Cluster.Squash(ctx, sqsh, content, rootfs)
	// A login node that stopped reading, having the squashfs already or failing, ends the stream.
	rootfs.CloseWithError(errors.New("apptainer: the login node stopped reading"))
	wg.Wait()
	if err != nil {
		return "", fmt.Errorf("making the squashfs of the image %s: %w", image, err)
	}
	return by, nil
}

// convert converts the image to a SIF kept in the local directory, copies it to sif on the cluster,
// and removes the kept one, all under ctx, the bound of the conversion.
func (e *Engine) convert(ctx context.Context, image, sif string, log io.Writer) error {
	kept := path.Base(sif)
	by, err := e.cfg.Converter.Describe(ctx)
	if err != nil {
		return err
	}
	err = e.keep(ctx, image, kept, by, log)
	if err == nil {
		err = e.copy(ctx, sif, func(ctx context.Context) (io.ReadCloser, error) {
			r, ok, err := e.cfg.Converter.Open(ctx, e.cfg.LocalDir, kept)
			if err == nil && !ok {
				err = unavailable("the SIF of the image %s kept in %s is gone", image, e.cfg.LocalDir)
			}
			return r, err
		}, log)
	}
	if err == nil {
		err = e.copy(ctx, sif+BuiltBy, func(context.Context) (io.ReadCloser, error) {
			return io.NopCloser(strings.NewReader(by + "\n")), nil
		}, log)
	}
	if err != nil {
		return err
	}
	_, _ = fmt.Fprintf(log, "[apptainer] %s is %s, built by %s\n", image, sif, by)
	e.evict(ctx, image, kept, log)
	return nil
}

// lockConversion takes the lock of the conversions of the image's content on this machine, trying
// again after each Poll while another holds it, up to ConvertTimeout: the bound of the conversion
// it waits for.
func (e *Engine) lockConversion(ctx context.Context, image, content string, log io.Writer) (io.Closer, error) {
	name := "vibesys-convert-" + strings.TrimPrefix(content, "sha256:") + ".lock"
	var limit <-chan struct{}
	for {
		lock, err := e.cfg.Converter.Lock(ctx, e.cfg.LocalDir, name)
		if !errors.Is(err, ErrConverting) {
			if err != nil {
				return nil, fmt.Errorf("locking the conversion of the image %s in %s: %w", image, e.cfg.LocalDir, err)
			}
			return lock, nil
		}
		if limit == nil {
			_, _ = fmt.Fprintf(log, "[apptainer] another process of this machine is converting %s to a SIF; "+
				"waiting for it to end, to use its SIF\n", image)
			var stop func()
			limit, stop = e.cfg.Timers.Timeout(ctx, e.cfg.ConvertTimeout)
			defer stop()
		}
		pause, stopPause := e.cfg.Timers.Timeout(ctx, e.cfg.Poll)
		select {
		case <-pause:
			stopPause()
		case <-limit:
			stopPause()
			return nil, unavailable("another process of this machine converted the image %s to a SIF for longer than %v "+
				"([apptainer] convert_timeout_seconds), and this one stopped waiting for it", image, e.cfg.ConvertTimeout)
		case <-ctx.Done():
			stopPause()
			return nil, ctx.Err()
		}
	}
}

// keep makes the image's SIF kept in the local directory under the name, converting the image
// unless the directory keeps it from an earlier conversion. The conversion is checked first
// against the space of the local directory: docker save streams the archive into the converter.
//
// The check of the space does not count what conversions of other images in progress on this
// machine are about to write: two of them that start together may each find room for itself and
// not for both.
func (e *Engine) keep(ctx context.Context, image, name, by string, log io.Writer) error {
	r, ok, err := e.cfg.Converter.Open(ctx, e.cfg.LocalDir, name)
	if err != nil {
		return fmt.Errorf("reading the SIF of the image %s kept in %s: %w", image, e.cfg.LocalDir, err)
	}
	if ok {
		_ = r.Close()
		_, _ = fmt.Fprintf(log, "[apptainer] using the SIF of %s that an earlier conversion kept in %s\n", image, e.cfg.LocalDir)
		return nil
	}
	unpacked, stored, err := e.cfg.Export.Sizes(ctx, image)
	if err != nil {
		return fmt.Errorf("reading the size of the image %s: %w", image, err)
	}
	free, err := e.cfg.Converter.Free(ctx, e.cfg.LocalDir)
	if err != nil {
		return fmt.Errorf("reading the free space of %s: %w", e.cfg.LocalDir, err)
	}
	// Apptainer unpacks the image's tree beside its archive and writes the SIF there; the archive and
	// the SIF each hold the image's files compressed, about as its layers are stored.
	if need := unpacked + 2*stored; free < need {
		return unavailable(
			"converting the image %s to a SIF on this machine needs about %s free in %s (the image's %s unpacked, and its archive "+
				"and its SIF, about %s each), and its file system has %s free; free space there, or set [apptainer] "+
				"local_scratch_dir to a directory of a file system with more",
			image, Size(need), e.cfg.LocalDir, Size(unpacked), Size(stored), Size(free))
	}
	_, _ = fmt.Fprintf(log, "[apptainer] converting %s to a SIF on this machine with %s, in %s\n", image, by, e.cfg.LocalDir)
	archive, archiveW := io.Pipe()
	var wg sync.WaitGroup
	wg.Go(func() {
		err := e.cfg.Export.Save(ctx, image, archiveW)
		if err != nil {
			err = fmt.Errorf("exporting the image %s: %w", image, err)
		}
		archiveW.CloseWithError(err)
	})
	err = e.cfg.Converter.Build(ctx, e.cfg.LocalDir, archive, name)
	// A converter that stopped reading leaves the export nothing to write to.
	archive.CloseWithError(errors.New("apptainer: the converter ended"))
	wg.Wait()
	if err != nil {
		return fmt.Errorf("converting the image %s to a SIF with %s: %w", image, by, err)
	}
	return nil
}

// copy publishes what open gives at the path of the cluster, opening it again for each attempt. A
// copy that loses its connection to the cluster is made again after a Poll, up to CopyAttempts in
// all, each said to log.
func (e *Engine) copy(ctx context.Context, p string, open func(context.Context) (io.ReadCloser, error), log io.Writer) error {
	for attempt := 1; ; attempt++ {
		r, err := open(ctx)
		if err != nil {
			return err
		}
		err = e.cfg.Cluster.Publish(ctx, p, r)
		_ = r.Close()
		if err == nil || !errors.Is(err, ErrConnectionLost) || ctx.Err() != nil {
			return err
		}
		if attempt == CopyAttempts {
			return fmt.Errorf("the copy to %s lost its connection to the cluster %d times; the last: %w", p, CopyAttempts, err)
		}
		_, _ = fmt.Fprintf(log, "[apptainer] the copy to %s lost its connection to the cluster (%v); copying again, attempt %d of %d\n",
			p, err, attempt+1, CopyAttempts)
		if !clock.Sleep(ctx, e.cfg.Timers, e.cfg.Poll) {
			return ctx.Err()
		}
	}
}

// evict removes the image's SIF kept in the local directory, which the cluster has. A SIF that
// stays is removed by a later conversion once it is a day old.
func (e *Engine) evict(ctx context.Context, image, name string, log io.Writer) {
	if err := e.cfg.Converter.Remove(ctx, e.cfg.LocalDir, name); err != nil {
		_, _ = fmt.Fprintf(log, "[apptainer] the SIF of %s kept in %s, which the cluster has, was not removed: %v\n", image, e.cfg.LocalDir, err)
	}
}

// Size writes a number of bytes for a person.
func Size(n int64) string {
	const gib = 1 << 30
	if n >= gib {
		return fmt.Sprintf("%.1f GiB", float64(n)/gib)
	}
	if n >= 1<<20 {
		return fmt.Sprintf("%.1f MiB", float64(n)/(1<<20))
	}
	return fmt.Sprintf("%d bytes", n)
}

// BoxEnv is the environment a box's processes get: the image's (KEY=VALUE each, as
// Exporter.Env gives it), over which own is set. A runtime that starts a process in an image
// does not promise the image's variables to it: under Pyxis the step inherits the login node's
// PATH, so a tool the image's PATH names (Go under /usr/local/go/bin) was not found. The box
// names every variable instead, as a container of the docker environment has them.
func BoxEnv(image []string, own map[string]string) map[string]string {
	env := make(map[string]string, len(image)+len(own))
	for _, kv := range image {
		if k, v, ok := strings.Cut(kv, "="); ok && k != "" {
			env[k] = v
		}
	}
	maps.Copy(env, own)
	return env
}

// handle is a started container, and a docker.Container.
type handle struct {
	e    *Engine
	b    *box
	node Node

	mu     sync.Mutex
	procs  []*proc
	closed bool
	once   sync.Once
}

var _ docker.Container = (*handle)(nil)

// boxOf is what the node runs the container's processes with.
func (h *handle) boxOf() Box {
	h.e.mu.Lock()
	defer h.e.mu.Unlock()
	return Box{
		Name: h.b.name, Image: h.b.sif, Dir: h.b.spec.Dir, Mounts: slices.Clone(h.b.spec.Mounts),
		Env: maps.Clone(h.b.env), ROCm: h.b.spec.GPUs.ROCm,
	}
}

// Start implements environment.Launcher.
func (h *handle) Start(argv []string, sink io.Writer) (environment.Process, error) {
	if len(argv) == 0 || argv[0] == "" {
		return nil, invalid("no command to run")
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.closed {
		return nil, unavailable("the container %s is closed", h.b.name)
	}
	inner, err := h.node.Start(h.boxOf(), argv, sink)
	if err != nil {
		return nil, err
	}
	p := &proc{h: h, inner: inner, exited: make(chan environment.Exit, 1)}
	p.wg.Go(func() { p.exited <- h.finish(<-inner.Exited()) })
	h.procs = append(h.procs, p)
	return p, nil
}

// finish says how a process ended: one killed by a signal after its container went away was ended
// by its removal, which says nothing of how the command would have ended.
func (h *handle) finish(st environment.Exit) environment.Exit {
	h.e.mu.Lock()
	gone := h.b.gone
	h.e.mu.Unlock()
	if st.Err == nil && st.Kind == "" && st.Code >= 128 && gone {
		return docker.Lost(fmt.Sprintf("the container %s went away under the command", h.b.name))
	}
	return st
}

// Port implements docker.Container: a port of the node that no process listens on, since the
// node's network is shared with every other job on it.
func (h *handle) Port(ctx context.Context, _ string) (int, error) {
	return h.node.Port(ctx)
}

// signal sends the signal to the container's processes, bounded.
func (h *handle) signal(sig Signal) error {
	ctx, stop := clock.Deadline(context.Background(), h.e.cfg.Timers, CallTimeout)
	defer stop()
	return h.node.Signal(ctx, h.b.name, sig)
}

// Close implements docker.Container. The container of an allocation releases its hold, which ends
// the allocation; a container that joined one has its processes killed. Then the clients of its
// processes are ended and waited for, so nothing writes to a sink afterwards.
func (h *handle) Close() {
	h.once.Do(func() {
		h.mu.Lock()
		h.closed = true
		procs := slices.Clone(h.procs)
		h.mu.Unlock()
		h.e.mu.Lock()
		hold, owner := h.b.hold, h.b.owner
		h.b.gone = true
		h.e.mu.Unlock()
		switch {
		case h.b.standing:
			// The allocation is the run's, and stays: only the box's processes end.
			ctx, stop := clock.Deadline(context.Background(), h.e.cfg.Timers, CallTimeout)
			// A failure is kept by the engine (TakeSurvivors), where the caller of Close reads it.
			_ = h.e.reap(ctx, h.b)
			stop()
		case owner:
			if hold != nil {
				_ = hold.Close()
			}
		default:
			_ = h.signal(SIGKILL)
		}
		for _, p := range procs {
			p.Kill()
		}
		for _, p := range procs {
			p.wg.Wait()
		}
	})
}

// proc is a process of a container.
type proc struct {
	h      *handle
	inner  environment.Process
	exited chan environment.Exit
	wg     sync.WaitGroup
}

// Exited implements environment.Process.
func (p *proc) Exited() <-chan environment.Exit { return p.exited }

// Terminate implements environment.Process: SIGTERM to every process of the container, as a
// container's processes are one machine. A container that cannot be reached is not an error here:
// its processes end when it is removed, which the caller does after the grace period either way.
func (p *proc) Terminate() { _ = p.h.signal(SIGTERM) }

// Kill implements environment.Process: it ends the client of the command at once. The command
// itself ends no later than the container's removal.
func (p *proc) Kill() { p.inner.Kill() }
