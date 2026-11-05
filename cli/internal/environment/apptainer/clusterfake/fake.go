// Package clusterfake is a Slurm cluster whose nodes run Apptainer, kept in memory: an
// apptainer.Cluster for tests. Its jobs queue and run as a test says, its nodes run the fake
// world's scripts as the containers' processes, and each process that uses it holds its own
// Client, whose death (Crash) ends what it held, as the death of a real caller ends its ssh
// connections.
//
// It is no more forgiving than a real cluster: a step of a job that does not run starts nothing, a
// box whose SIF is not on the shared file system cannot start a process, and a file published from
// a stream that was cut short is not there.
//
// Converter is the caller's machine's apptainer program, kept in memory: its SIF of an archive
// names the archive's digest, and a test sets its free space and makes it fail.
package clusterfake

import (
	"context"
	"fmt"
	"io"
	"maps"
	"slices"
	"strconv"
	"strings"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
)

// Machine is what a fake node says of itself for the engine's machine scripts.
var Machine = map[string]string{
	"uname":   "Linux\nx86_64\nnode-a\n",
	"cpuinfo": "processor\t: 0\nmodel name\t: AMD EPYC 7V13 64-Core Processor\nprocessor\t: 1\nmodel name\t: AMD EPYC 7V13 64-Core Processor\n",
	"meminfo": "MemTotal:       65000000 kB\nMemAvailable:   60000000 kB\n",
	"loadavg": "0.50 0.40 0.30 1/200 12345\n",
	"stat":    "cpu  100 0 100 800 0 0 0 0 0 0\n",
}

// Cluster is the cluster's state. Create it with New. It is safe for concurrent use.
type Cluster struct {
	gates fakeenv.Gates

	mu        sync.Mutex
	files     map[string][]byte
	published map[string]int // how many publishes of each path finished
	jobs      map[string]*job
	ids       int
	ports     int
	says      map[string]string
	queued    int // how many reads of its state a new job waits for before it runs
	ends      bool
	fail      func(op string) error
	started   []apptainer.Box
	submitted []apptainer.Allocation
}

// job is a batch allocation.
type job struct {
	id, name, comment string
	phase             apptainer.Phase
	state             string
	waits             int
	boxes             map[string]*box
	ports             []int // the ports the node chose in it
}

// box is a container's processes on the job's node.
type box struct {
	spec    apptainer.Box
	machine *fakeenv.Machine
	procs   []environment.Process
}

// New returns an empty cluster whose node scripts wait at the gates.
func New(gates fakeenv.Gates) *Cluster {
	return &Cluster{
		gates: gates, files: map[string][]byte{}, published: map[string]int{}, jobs: map[string]*job{},
		says: maps.Clone(Machine),
	}
}

// Queue makes every later job wait in the queue for n reads of its state before it runs. With
// ends, it ends instead, as a job Slurm refused or cancelled does.
func (c *Cluster) Queue(n int, ends bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.queued, c.ends = n, ends
}

// Say sets what every node prints for the script of the name.
func (c *Cluster) Say(script, out string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.says[script] = out
}

// SetFaults makes the calls named by op fail with what f returns, or none with nil. The ops are
// "has", "read", "publish", "submit", "state", "jobs", "cancel", "hold", "readnode", "port", "start"
// and "signal".
func (c *Cluster) SetFaults(f func(op string) error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.fail = f
}

func (c *Cluster) fault(op string) error {
	c.mu.Lock()
	f := c.fail
	c.mu.Unlock()
	if f == nil {
		return nil
	}
	return f(op)
}

// File returns a file of the shared file system.
func (c *Cluster) File(path string) ([]byte, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	data, ok := c.files[path]
	return slices.Clone(data), ok
}

// Files returns the paths of the shared file system's files, sorted.
func (c *Cluster) Files() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return slices.Sorted(maps.Keys(c.files))
}

// Put writes a file of the shared file system, as something other than the engine did.
func (c *Cluster) Put(path string, data []byte) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.files[path] = slices.Clone(data)
}

// Published returns how many publishes of the path finished.
func (c *Cluster) Published(path string) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.published[path]
}

// Started returns the box of every process the nodes started, in order.
func (c *Cluster) Started() []apptainer.Box {
	c.mu.Lock()
	defer c.mu.Unlock()
	return slices.Clone(c.started)
}

// Submitted returns the allocations asked for, in order.
func (c *Cluster) Submitted() []apptainer.Allocation {
	c.mu.Lock()
	defer c.mu.Unlock()
	return slices.Clone(c.submitted)
}

// Plant adds a running job nobody here asked for: what a run of a process that died left, or a job
// of something else.
func (c *Cluster) Plant(name, comment string) string {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.ids++
	id := strconv.Itoa(1000 + c.ids)
	c.jobs[id] = &job{id: id, name: name, comment: comment, phase: apptainer.Running, state: "RUNNING", boxes: map[string]*box{}}
	return id
}

// Live returns the names of the jobs that are queued or running, sorted.
func (c *Cluster) Live() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	var out []string
	for _, j := range c.jobs {
		if j.phase != apptainer.Ended {
			out = append(out, j.name)
		}
	}
	slices.Sort(out)
	return out
}

// Running returns the boxes whose processes run, by job name: what is left on the nodes.
func (c *Cluster) Running() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	var out []string
	for _, j := range c.jobs {
		if j.phase == apptainer.Running {
			for name := range j.boxes {
				out = append(out, j.name+"/"+name)
			}
		}
	}
	slices.Sort(out)
	return out
}

// end ends the job and every process on its node.
func (c *Cluster) end(j *job, state string) {
	c.mu.Lock()
	if j.phase == apptainer.Ended {
		c.mu.Unlock()
		return
	}
	j.phase, j.state = apptainer.Ended, state
	boxes := slices.Collect(maps.Values(j.boxes))
	j.boxes = map[string]*box{}
	c.mu.Unlock()
	for _, b := range boxes {
		b.machine.End()
	}
}

// Connect returns the cluster as one process reaches it.
func (c *Cluster) Connect() *Client { return &Client{c: c} }

// Client is one process's view of the cluster, and an apptainer.Cluster.
type Client struct {
	c *Cluster

	mu    sync.Mutex
	dead  bool
	holds []*hold
}

var _ apptainer.Cluster = (*Client)(nil)

// Crash ends the client as the death of its process does: what it held is let go, which ends the
// allocations it held, and later calls fail.
func (cl *Client) Crash() {
	cl.mu.Lock()
	cl.dead = true
	holds := cl.holds
	cl.holds = nil
	cl.mu.Unlock()
	for _, h := range holds {
		_ = h.Close()
	}
}

func lost() error {
	return &core.Failure{Kind: core.FailureUnavailable, Message: "clusterfake: the connection to the login node is gone"}
}

func (cl *Client) enter(ctx context.Context, op string) error {
	cl.mu.Lock()
	dead := cl.dead
	cl.mu.Unlock()
	if dead {
		return lost()
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	return cl.c.fault(op)
}

// Has implements apptainer.Cluster.
func (cl *Client) Has(ctx context.Context, path string) (bool, error) {
	if err := cl.enter(ctx, "has"); err != nil {
		return false, err
	}
	_, ok := cl.c.File(path)
	return ok, nil
}

// Read implements apptainer.Cluster.
func (cl *Client) Read(ctx context.Context, path string) ([]byte, bool, error) {
	if err := cl.enter(ctx, "read"); err != nil {
		return nil, false, err
	}
	data, ok := cl.c.File(path)
	return data, ok, nil
}

// Publish implements apptainer.Cluster.
func (cl *Client) Publish(ctx context.Context, path string, r io.Reader) error {
	if err := cl.enter(ctx, "publish"); err != nil {
		return err
	}
	data, err := io.ReadAll(r)
	if err != nil {
		return &core.Failure{Kind: core.FailureUnavailable, Message: "clusterfake: the copy was cut short: " + err.Error()}
	}
	cl.c.mu.Lock()
	defer cl.c.mu.Unlock()
	cl.c.files[path] = data
	cl.c.published[path]++
	return nil
}

// Submit implements apptainer.Cluster.
func (cl *Client) Submit(ctx context.Context, a apptainer.Allocation) (string, error) {
	if err := cl.enter(ctx, "submit"); err != nil {
		return "", err
	}
	c := cl.c
	c.mu.Lock()
	defer c.mu.Unlock()
	c.ids++
	id := strconv.Itoa(1000 + c.ids)
	j := &job{id: id, name: a.Name, comment: a.Comment, phase: apptainer.Queued, state: "PENDING", waits: c.queued, boxes: map[string]*box{}}
	if c.queued == 0 && !c.ends {
		j.phase, j.state = apptainer.Running, "RUNNING"
	}
	c.jobs[id] = j
	c.submitted = append(c.submitted, a)
	return id, nil
}

// State implements apptainer.Cluster.
func (cl *Client) State(ctx context.Context, id string) (apptainer.JobState, error) {
	if err := cl.enter(ctx, "state"); err != nil {
		return apptainer.JobState{}, err
	}
	c := cl.c
	c.mu.Lock()
	defer c.mu.Unlock()
	j := c.jobs[id]
	if j == nil {
		return apptainer.JobState{Phase: apptainer.Ended}, nil
	}
	if j.phase == apptainer.Queued {
		if j.waits--; j.waits < 0 {
			j.phase, j.state = apptainer.Running, "RUNNING"
			if c.ends {
				j.phase, j.state = apptainer.Ended, "FAILED"
			}
		}
	}
	return apptainer.JobState{Phase: j.phase, Name: j.state, Reason: map[bool]string{true: "Priority"}[j.phase == apptainer.Queued]}, nil
}

// Jobs implements apptainer.Cluster.
func (cl *Client) Jobs(ctx context.Context, scope string) ([]string, error) {
	if err := cl.enter(ctx, "jobs"); err != nil {
		return nil, err
	}
	c := cl.c
	c.mu.Lock()
	defer c.mu.Unlock()
	var out []string
	for _, j := range c.jobs {
		if j.phase != apptainer.Ended && j.comment == scope {
			out = append(out, j.name)
		}
	}
	slices.Sort(out)
	return out, nil
}

// Cancel implements apptainer.Cluster.
func (cl *Client) Cancel(ctx context.Context, name string) error {
	if err := cl.enter(ctx, "cancel"); err != nil {
		return err
	}
	c := cl.c
	c.mu.Lock()
	var named []*job
	for _, j := range c.jobs {
		if j.name == name {
			named = append(named, j)
		}
	}
	c.mu.Unlock()
	for _, j := range named {
		c.end(j, "CANCELLED")
	}
	return nil
}

// Node implements apptainer.Cluster.
func (cl *Client) Node(id string) apptainer.Node { return &node{cl: cl, id: id} }

// node is a job's node.
type node struct {
	cl *Client
	id string
}

var _ apptainer.Node = (*node)(nil)

// running is the job when it runs. c.mu is held.
func (n *node) running() *job {
	j := n.cl.c.jobs[n.id]
	if j == nil || j.phase != apptainer.Running {
		return nil
	}
	return j
}

func (n *node) enter(ctx context.Context, op string) (*job, error) {
	if err := n.cl.enter(ctx, op); err != nil {
		return nil, err
	}
	n.cl.c.mu.Lock()
	defer n.cl.c.mu.Unlock()
	j := n.running()
	if j == nil {
		return nil, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("clusterfake: the job %s does not run, so no step of it starts", n.id)}
	}
	return j, nil
}

// hold holds a job for a client.
type hold struct {
	c    *Cluster
	j    *job
	once sync.Once
}

// Close ends the holder, which cancels its allocation.
func (h *hold) Close() error {
	h.once.Do(func() { h.c.end(h.j, "CANCELLED") })
	return nil
}

// Hold implements apptainer.Node.
func (n *node) Hold(ctx context.Context) (io.Closer, error) {
	j, err := n.enter(ctx, "hold")
	if err != nil {
		return nil, err
	}
	h := &hold{c: n.cl.c, j: j}
	n.cl.mu.Lock()
	n.cl.holds = append(n.cl.holds, h)
	n.cl.mu.Unlock()
	return h, nil
}

// Read implements apptainer.Node.
func (n *node) Read(ctx context.Context, scripts map[string]string) (map[string]string, error) {
	if _, err := n.enter(ctx, "readnode"); err != nil {
		return nil, err
	}
	c := n.cl.c
	c.mu.Lock()
	defer c.mu.Unlock()
	out := map[string]string{}
	for name := range scripts {
		out[name] = c.says[name]
	}
	return out, nil
}

// Port implements apptainer.Node.
func (n *node) Port(ctx context.Context) (int, error) {
	j, err := n.enter(ctx, "port")
	if err != nil {
		return 0, err
	}
	n.cl.c.mu.Lock()
	defer n.cl.c.mu.Unlock()
	n.cl.c.ports++
	port := 20000 + n.cl.c.ports
	j.ports = append(j.ports, port)
	return port, nil
}

// errLost is how a process ends whose box was killed or whose allocation ended under it.
var errLost = fmt.Errorf("%w: its allocation or its container ended", docker.ErrLost)

// Start implements apptainer.Node.
func (n *node) Start(spec apptainer.Box, argv []string, sink io.Writer) (environment.Process, error) {
	j, err := n.enter(context.Background(), "start")
	if err != nil {
		return lostProcess(err.Error()), nil
	}
	c := n.cl.c
	c.mu.Lock()
	if _, ok := c.files[spec.Image]; !ok {
		c.mu.Unlock()
		// apptainer exits with its own 255 for an image it cannot open, which is not the command's.
		return lostProcess("no SIF at " + spec.Image), nil
	}
	b := j.boxes[spec.Name]
	if b == nil {
		b = &box{spec: spec, machine: fakeenv.NewMachine(c.gates, spec.Dir, errLost)}
		j.boxes[spec.Name] = b
	}
	c.started = append(c.started, spec)
	if len(argv) == 3 && argv[0] == docker.ProbeMount && argv[1] == docker.ProbeCommand {
		// The probe asks a port the node chose in this allocation, which another box's server
		// answers on once it is ready: the node's network is the boxes' one network.
		code := 1
		mounted := slices.ContainsFunc(spec.Mounts, func(m docker.Mount) bool { return m.Container == docker.ProbeMount })
		asked := slices.ContainsFunc(j.ports, func(p int) bool {
			rest, ok := strings.CutPrefix(argv[2], fmt.Sprintf("http://127.0.0.1:%d", p))
			return ok && (rest == "" || strings.HasPrefix(rest, "/"))
		})
		for name, other := range j.boxes {
			if name != spec.Name && other.machine.Ready() && asked {
				code = 0
			}
		}
		if !mounted {
			code = 127
		}
		c.mu.Unlock()
		return exitedProcess(code), nil
	}
	c.mu.Unlock()
	p, err := b.machine.Start(argv, sink)
	if err != nil {
		return nil, err
	}
	c.mu.Lock()
	b.procs = append(b.procs, p)
	c.mu.Unlock()
	return p, nil
}

// Signal implements apptainer.Node.
func (n *node) Signal(ctx context.Context, name string, sig apptainer.Signal) error {
	j, err := n.enter(ctx, "signal")
	if err != nil {
		return err
	}
	c := n.cl.c
	c.mu.Lock()
	b := j.boxes[name]
	if b == nil {
		c.mu.Unlock()
		return nil
	}
	procs := slices.Clone(b.procs)
	if sig == apptainer.SIGKILL {
		delete(j.boxes, name)
	}
	c.mu.Unlock()
	switch sig {
	case apptainer.SIGTERM:
		for _, p := range procs {
			p.Terminate()
		}
	case apptainer.SIGKILL:
		b.machine.End()
	}
	return nil
}

// simple is a process that has already ended.
type simple struct{ exited chan environment.Exit }

func (p *simple) Exited() <-chan environment.Exit { return p.exited }
func (*simple) Terminate()                        {}
func (*simple) Kill()                             {}

func exitedProcess(code int) environment.Process {
	p := &simple{exited: make(chan environment.Exit, 1)}
	p.exited <- environment.Exit{Code: code}
	return p
}

func lostProcess(why string) environment.Process {
	p := &simple{exited: make(chan environment.Exit, 1)}
	p.exited <- docker.Lost(why)
	return p
}
