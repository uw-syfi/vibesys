package cpu

import (
	"context"
	"fmt"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/backend"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Counters are the machine's cumulative CPU time, in any one unit: Busy is the time spent running
// something and Total is Busy plus the time spent idle. Both only grow.
type Counters struct {
	Busy, Total uint64
}

// Reading is what the machine says about its load right now.
type Reading struct {
	LoadAverage1m        float64
	CPU                  Counters
	MemoryAvailableBytes uint64
}

// System is the machine as the backend reads it.
type System interface {
	// Machine returns what does not change while the machine runs.
	Machine(ctx context.Context) (core.MachineFacts, error)
	// Read returns the current load. It fails with a *core.Failure of kind FailureUnavailable
	// when the machine cannot be read.
	Read(ctx context.Context) (Reading, error)
}

// Backend is a backend.Backend for the CPU stack. Create it with New. It is safe for concurrent
// use.
type Backend struct {
	sys   System
	clock clock.Clock

	mu      sync.Mutex
	machine *core.MachineFacts
	last    *Counters
}

var _ backend.Backend = (*Backend)(nil)

// New returns the CPU backend over a system, stamping load samples with the clock.
func New(sys System, clk clock.Clock) *Backend { return &Backend{sys: sys, clock: clk} }

// Kind implements backend.Backend.
func (*Backend) Kind() core.BackendKind { return core.BackendCPU }

// Machine implements backend.Backend. The machine is read once and the answer kept.
func (b *Backend) Machine(ctx context.Context) (core.MachineFacts, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.machine != nil {
		return *b.machine, nil
	}
	facts, err := b.sys.Machine(ctx)
	if err != nil {
		return core.MachineFacts{}, err
	}
	b.machine = &facts
	return facts, nil
}

// Load implements backend.Backend.
func (b *Backend) Load(ctx context.Context) (core.MachineLoad, error) {
	r, err := b.sys.Read(ctx)
	if err != nil {
		return core.MachineLoad{}, err
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	busy := BusyFraction(b.last, r.CPU)
	next := r.CPU
	b.last = &next
	return core.MachineLoad{
		SampledAt:            b.clock.Now(),
		LoadAverage1m:        finiteNonNegative(r.LoadAverage1m),
		CPUBusyFraction:      busy,
		MemoryAvailableBytes: r.MemoryAvailableBytes,
	}, nil
}

// BusyFraction is the fraction of CPU time spent busy between two readings of the counters, from
// 0 to 1. With no earlier reading, or when the counters did not move forward, which is a machine
// that was reset or a reading that did not change, it is the fraction since the counters began.
func BusyFraction(prev *Counters, now Counters) float64 {
	busy, total := now.Busy, now.Total
	if prev != nil && now.Total > prev.Total && now.Busy >= prev.Busy {
		busy, total = now.Busy-prev.Busy, now.Total-prev.Total
	}
	if total == 0 {
		return 0
	}
	f := float64(busy) / float64(total)
	switch {
	case f < 0:
		return 0
	case f > 1:
		return 1
	}
	return f
}

func finiteNonNegative(x float64) float64 {
	if x != x || x < 0 || x > 1e12 {
		return 0
	}
	return x
}

// FakeSystem is a System whose readings a test scripts. It is a real implementation of System,
// not a stub: it returns the facts and the readings it was given, in order, and fails the way a
// machine that cannot be read does.
type FakeSystem struct {
	Facts core.MachineFacts

	mu       sync.Mutex
	readings []Reading
	next     int
	machines int
	fail     error
}

var _ System = (*FakeSystem)(nil)

// NewFakeSystem returns a system with the machine facts. Its readings are queued with Queue; once
// they run out, each Read returns the last one, as a machine that is idle does.
func NewFakeSystem(facts core.MachineFacts) *FakeSystem { return &FakeSystem{Facts: facts} }

// Queue adds readings to return from Read, in order.
func (f *FakeSystem) Queue(readings ...Reading) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.readings = append(f.readings, readings...)
}

// FailWith makes Machine and Read fail with err, as a machine that cannot be read does. A nil err
// restores it.
func (f *FakeSystem) FailWith(err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.fail = err
}

// MachineCalls returns how many times Machine was called.
func (f *FakeSystem) MachineCalls() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.machines
}

// Machine implements System.
func (f *FakeSystem) Machine(ctx context.Context) (core.MachineFacts, error) {
	if err := ctx.Err(); err != nil {
		return core.MachineFacts{}, err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.machines++
	if f.fail != nil {
		return core.MachineFacts{}, f.fail
	}
	return f.Facts, nil
}

// Read implements System.
func (f *FakeSystem) Read(ctx context.Context) (Reading, error) {
	if err := ctx.Err(); err != nil {
		return Reading{}, err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.fail != nil {
		return Reading{}, f.fail
	}
	switch {
	case len(f.readings) == 0:
		return Reading{}, nil
	case f.next >= len(f.readings):
		return f.readings[len(f.readings)-1], nil
	}
	r := f.readings[f.next]
	f.next++
	return r, nil
}

func unavailable(format string, args ...any) *core.Failure {
	return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(format, args...)}
}
