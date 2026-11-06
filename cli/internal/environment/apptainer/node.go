package apptainer

import (
	"context"
	"encoding/json"
	"fmt"
	"maps"
	"path"
	"slices"
	"strings"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/backend"
	"github.com/uw-syfi/vibesys/cli/internal/backend/cpu"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Reading is what a node said of itself inside an allocation, and when: what each script printed,
// by its name.
type Reading struct {
	At      time.Time         `json:"at"`
	Outputs map[string]string `json:"outputs"`
}

// machineScripts are what a node runs to say what its machine is and how loaded it is.
var machineScripts = map[string]string{
	"uname":   "uname -s; uname -m; hostname",
	"cpuinfo": "cat /proc/cpuinfo",
	"meminfo": "cat /proc/meminfo",
	"loadavg": "cat /proc/loadavg",
	"stat":    "head -n 1 /proc/stat",
}

// scripts are every script a node runs when its allocation starts.
func (e *Engine) scripts() map[string]string {
	all := maps.Clone(machineScripts)
	maps.Copy(all, e.cfg.NodeScripts)
	return all
}

// checkScripts refuses node scripts whose names are the engine's own.
func checkScripts(s map[string]string) error {
	for _, name := range slices.Sorted(maps.Keys(s)) {
		if _, own := machineScripts[name]; own || name == "" {
			return invalid("the node script %q has a name the engine reads the machine with", name)
		}
	}
	return nil
}

// readingPath is where the last reading of the scope's nodes is kept on the cluster.
func (e *Engine) readingPath() string {
	return path.Join(e.cfg.Root, "nodes", e.cfg.Scope+".json")
}

// read reads the node and keeps what it said: for this process, and on the cluster for every
// process of the scope, whose fingerprints are taken before they have a node of their own.
func (e *Engine) read(ctx context.Context, node Node) error {
	out, err := node.Read(ctx, e.scripts())
	if err != nil {
		return fmt.Errorf("reading the node: %w", err)
	}
	r := &Reading{At: e.cfg.Clock.Now(), Outputs: out}
	e.mu.Lock()
	e.reading = r
	e.mu.Unlock()
	if e.cfg.Scope != "" {
		if data, err := json.Marshal(r); err == nil {
			// The kept reading only says what the scope's nodes are like; a run does not depend on
			// it, so one that could not be kept fails nothing.
			_ = e.cfg.Cluster.Publish(ctx, e.readingPath(), strings.NewReader(string(data)))
		}
	}
	return nil
}

// last is the last reading of this process's own node, or, with kept, the last one a process of the
// scope kept on the cluster when this process read none. It is nil when there is none.
func (e *Engine) last(ctx context.Context, kept bool) *Reading {
	e.mu.Lock()
	r := e.reading
	e.mu.Unlock()
	if r != nil || !kept || e.cfg.Scope == "" {
		return r
	}
	data, ok, err := e.cfg.Cluster.Read(ctx, e.readingPath())
	if err != nil || !ok {
		return nil
	}
	var got Reading
	if json.Unmarshal(data, &got) != nil || got.Outputs == nil {
		return nil
	}
	return &got
}

// Backend is the backend.Backend of the nodes: what the last node this process ran on said of
// itself, read on the node inside the allocation, never on the login node.
//
// Its Machine is, before this process ran on a node, the last node a process of the scope ran on,
// and with none the zero facts: a daemon fingerprints a job before the job has a node, and its
// fingerprint's facts are recorded and compared by nothing. Its Load is only ever of this
// process's own node.
func (e *Engine) Backend() backend.Backend { return nodeBackend{e: e} }

type nodeBackend struct{ e *Engine }

func (nodeBackend) Kind() core.BackendKind { return core.BackendCPU }

func (b nodeBackend) Machine(ctx context.Context) (core.MachineFacts, error) {
	if err := ctx.Err(); err != nil {
		return core.MachineFacts{}, err
	}
	r := b.e.last(ctx, true)
	if r == nil {
		return core.MachineFacts{}, nil
	}
	return MachineOf(r.Outputs)
}

func (b nodeBackend) Load(ctx context.Context) (core.MachineLoad, error) {
	if err := ctx.Err(); err != nil {
		return core.MachineLoad{}, err
	}
	r := b.e.last(ctx, false)
	if r == nil {
		return core.MachineLoad{}, unavailable("the load is read on a job's node, and this process has run on none")
	}
	return LoadOf(r)
}

// goArch names a kernel's machine type as Go does, as the facts of a host name it.
var goArch = map[string]string{"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64", "ppc64le": "ppc64le", "s390x": "s390x", "riscv64": "riscv64"}

// MachineOf reads a node's machine from what its machine scripts printed.
func MachineOf(out map[string]string) (core.MachineFacts, error) {
	f := strings.Fields(out["uname"])
	if len(f) != 3 || f[0] != "Linux" {
		return core.MachineFacts{}, unavailable("the node is not a Linux machine this build can read (%q)", out["uname"])
	}
	arch, ok := goArch[f[1]]
	if !ok {
		arch = f[1]
	}
	model, processors := cpu.ParseCPUInfo(out["cpuinfo"])
	if model == "" {
		model = "unknown"
	}
	total, _, ok := cpu.ParseMemInfo(out["meminfo"])
	if !ok || processors <= 0 {
		return core.MachineFacts{}, unavailable("the node's /proc does not say how many CPUs or how much memory it has")
	}
	return core.MachineFacts{HostID: f[2], OS: "linux", Arch: arch, CPUModel: model, LogicalCPUs: uint32(processors), MemoryBytes: total}, nil
}

// LoadOf reads a node's load from what its machine scripts printed, when they printed it.
func LoadOf(r *Reading) (core.MachineLoad, error) {
	load, err := cpu.ParseLoadAvg(r.Outputs["loadavg"])
	if err != nil {
		return core.MachineLoad{}, unavailable("the node's load: %v", err)
	}
	counters, err := cpu.ParseStat(r.Outputs["stat"])
	if err != nil {
		return core.MachineLoad{}, unavailable("the node's load: %v", err)
	}
	_, available, _ := cpu.ParseMemInfo(r.Outputs["meminfo"])
	return core.MachineLoad{SampledAt: r.At, LoadAverage1m: load, CPUBusyFraction: cpu.BusyFraction(nil, counters), MemoryAvailableBytes: available}, nil
}

// Facts reads fingerprint facts from what a node script printed, as the Backend reads the machine:
// the last node this process ran on, or the last one of the scope, and Unread before any.
type Facts struct {
	Engine *Engine
	// Script names the node script whose output Parse reads.
	Script string
	Parse  func(string) (map[string]string, error)
	// Unread are the facts before any node of the scope was read: what says they are not known.
	Unread map[string]string
}

// GPUFacts returns the facts.
func (f Facts) GPUFacts(ctx context.Context) (map[string]string, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	r := f.Engine.last(ctx, true)
	if r == nil {
		return maps.Clone(f.Unread), nil
	}
	out, ok := r.Outputs[f.Script]
	if !ok {
		return nil, unavailable("the node's reading of %s has no output of the script %q", r.At.UTC().Format(time.RFC3339), f.Script)
	}
	return f.Parse(out)
}

// Unread is the value of a fact that no node has said yet.
const Unread = "unread: read on a compute node by the placement's first job"
