package daemontest

import (
	"context"
	"io/fs"
	"sync"

	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// FakeExampleTrees is a daemon.ExampleTrees over trees a test sets. It stands in for the release's
// examples tree. An example it was not given is unavailable as not found.
type FakeExampleTrees struct {
	mu      sync.Mutex
	sources map[string]daemon.ExampleSource
	errs    map[string]error
}

var _ daemon.ExampleTrees = (*FakeExampleTrees)(nil)

// Set makes example's source source.
func (f *FakeExampleTrees) Set(example string, source daemon.ExampleSource) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.sources == nil {
		f.sources = map[string]daemon.ExampleSource{}
	}
	f.sources[example] = source
}

// SetVendored makes example's source the files of tree and nothing else.
func (f *FakeExampleTrees) SetVendored(example string, tree fs.FS) {
	f.Set(example, daemon.ExampleSource{Vendored: tree})
}

// Fail makes asking for example answer err.
func (f *FakeExampleTrees) Fail(example string, err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.errs == nil {
		f.errs = map[string]error{}
	}
	f.errs[example] = err
}

// Source implements daemon.ExampleTrees.
func (f *FakeExampleTrees) Source(_ context.Context, example string) (daemon.ExampleSource, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.errs[example]; err != nil {
		return daemon.ExampleSource{}, err
	}
	if source, ok := f.sources[example]; ok {
		return source, nil
	}
	return daemon.ExampleSource{}, &daemon.ExampleUnavailable{Reason: daemon.ExampleNotFound, Detail: example}
}
