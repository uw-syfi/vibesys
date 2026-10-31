package machinestatstest

import (
	"context"
	"sync/atomic"

	"github.com/uw-syfi/vibesys/gateway/internal/machinestats"
)

// Call names one Host call: its method and its target, a path or a tool name.
type Call struct{ Method, Target string }

// Wedged is a machine on which chosen calls never return until Release is
// closed, whatever their context says: a PATH entry on a hung mount, a tool in
// uninterruptible sleep. It is the failure the sampler must bound without help
// from the Host, which a FakeHost tool that honors its context cannot show.
type Wedged struct {
	machinestats.Host
	// Hangs says which calls block. The rest pass through to Host.
	Hangs func(Call) bool
	// Release unblocks every blocked call. Close it before the test ends.
	Release chan struct{}
	// Blocked counts calls that blocked, the machine's load from stuck samples.
	Blocked atomic.Int64
}

// Wedge wraps host so that the calls Hangs selects block.
func Wedge(host machinestats.Host, hangs func(Call) bool) *Wedged {
	return &Wedged{Host: host, Hangs: hangs, Release: make(chan struct{})}
}

// Everything is a Hangs that blocks every call.
func Everything(Call) bool { return true }

func (w *Wedged) block(call Call) {
	if w.Hangs(call) {
		w.Blocked.Add(1)
		<-w.Release
	}
}

// ReadFile blocks if Hangs selects the path.
func (w *Wedged) ReadFile(path string) ([]byte, error) {
	w.block(Call{"ReadFile", path})
	return w.Host.ReadFile(path)
}

// Find blocks if Hangs selects the tool.
func (w *Wedged) Find(tool string, candidates ...string) (string, error) {
	w.block(Call{"Find", tool})
	return w.Host.Find(tool, candidates...)
}

// Run blocks if Hangs selects the path.
func (w *Wedged) Run(ctx context.Context, path string, args ...string) ([]byte, error) {
	w.block(Call{"Run", path})
	return w.Host.Run(ctx, path, args...)
}
