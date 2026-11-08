package daemontest

import (
	"context"
	"sync"

	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// Directories is a daemon.Directories over folders a test sets, and remembers what it was asked.
type Directories struct {
	mu      sync.Mutex
	folders map[string]daemon.Listing
	errs    map[string]error
	calls   []DirectoriesCall
}

// DirectoriesCall is one question put to Directories.
type DirectoriesCall struct {
	Path   string
	Hidden bool
}

// Set makes path list as listing.
func (d *Directories) Set(path string, listing daemon.Listing) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.folders == nil {
		d.folders = map[string]daemon.Listing{}
	}
	d.folders[path] = listing
}

// Fail makes path fail with err.
func (d *Directories) Fail(path string, err error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.errs == nil {
		d.errs = map[string]error{}
	}
	d.errs[path] = err
}

// Calls lists the questions asked so far, in order.
func (d *Directories) Calls() []DirectoriesCall {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]DirectoriesCall(nil), d.calls...)
}

// List implements daemon.Directories.
func (d *Directories) List(_ context.Context, path string, hidden bool) (daemon.Listing, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.calls = append(d.calls, DirectoriesCall{path, hidden})
	if err := d.errs[path]; err != nil {
		return daemon.Listing{}, err
	}
	return d.folders[path], nil
}
