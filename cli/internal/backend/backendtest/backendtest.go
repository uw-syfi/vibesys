// Package backendtest is the contract suite every backend.Backend passes, so a job flow written
// against the interface is correct for the fake it is tested with and for the machine it runs on.
package backendtest

import (
	"context"
	"math"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/backend"
)

// Backstop bounds every call in the suite. It only stops a hung run from hanging the build.
const Backstop = time.Minute

// Run runs the suite. newBackend makes a fresh backend for each test.
func Run(t *testing.T, newBackend func(t *testing.T) backend.Backend) {
	t.Helper()
	cases := []struct {
		name string
		fn   func(t *testing.T, b backend.Backend)
	}{
		{"the kind is a known backend", testKind},
		{"the machine is described and does not change", testMachine},
		{"a load sample is finite and in range", testLoad},
		{"samples are taken in order", testOrder},
		{"a cancelled context fails and changes nothing", testCancelled},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) { c.fn(t, newBackend(t)) })
	}
}

func ctxFor(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), Backstop)
	t.Cleanup(cancel)
	return ctx
}

func testKind(t *testing.T, b backend.Backend) {
	if !b.Kind().Known() {
		t.Errorf("Kind() = %q, which is not a known backend", b.Kind())
	}
}

func testMachine(t *testing.T, b backend.Backend) {
	ctx := ctxFor(t)
	first, err := b.Machine(ctx)
	if err != nil {
		t.Fatalf("Machine: %v", err)
	}
	if first.HostID == "" || first.OS == "" || first.Arch == "" || first.CPUModel == "" || first.LogicalCPUs == 0 || first.MemoryBytes == 0 {
		t.Errorf("Machine() = %+v: every fact must be set", first)
	}
	second, err := b.Machine(ctx)
	if err != nil || second != first {
		t.Errorf("Machine() again = %+v, %v; want %+v: what describes the machine does not change", second, err, first)
	}
}

func testLoad(t *testing.T, b backend.Backend) {
	ctx := ctxFor(t)
	for i := 0; i < 3; i++ {
		l, err := b.Load(ctx)
		if err != nil {
			t.Fatalf("Load: %v", err)
		}
		if l.SampledAt.IsZero() {
			t.Errorf("sample %d: no time", i)
		}
		if math.IsNaN(l.LoadAverage1m) || math.IsInf(l.LoadAverage1m, 0) || l.LoadAverage1m < 0 {
			t.Errorf("sample %d: load average %v is not a finite non-negative number", i, l.LoadAverage1m)
		}
		if math.IsNaN(l.CPUBusyFraction) || l.CPUBusyFraction < 0 || l.CPUBusyFraction > 1 {
			t.Errorf("sample %d: CPU busy fraction %v is not in [0, 1]", i, l.CPUBusyFraction)
		}
	}
}

func testOrder(t *testing.T, b backend.Backend) {
	ctx := ctxFor(t)
	first, err := b.Load(ctx)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	second, err := b.Load(ctx)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if second.SampledAt.Before(first.SampledAt) {
		t.Errorf("the second sample (%v) is before the first (%v)", second.SampledAt, first.SampledAt)
	}
}

func testCancelled(t *testing.T, b backend.Backend) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := b.Machine(ctx); err == nil {
		t.Error("Machine with a cancelled context succeeded")
	}
	if _, err := b.Load(ctx); err == nil {
		t.Error("Load with a cancelled context succeeded")
	}
	if _, err := b.Machine(ctxFor(t)); err != nil {
		t.Errorf("Machine after a cancelled call: %v", err)
	}
}
