package filestores

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"slices"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
)

// DefaultSamples is the sample count of a measurement that asks for none, until tasks declare
// their own. It is above the three samples a side that a verdict needs.
const DefaultSamples = 5

// FileStores is app.Stores over filestore. Create it with New.
type FileStores struct {
	index *filestore.Index
	rules store.Rules
	owner filestore.Owner
	opts  []filestore.Option

	// mu is never held across a call to the disk: a store on a hard mount that does not answer
	// holds up the callers of that store and no other.
	mu      sync.Mutex
	closed  bool
	stores  map[app.Scope]*filestore.Store
	opening map[app.Scope]*opening
}

// opening is one open of a store in progress, which every other caller for the scope waits for.
type opening struct {
	done  chan struct{}
	store *filestore.Store
	err   error
}

var _ app.Stores = (*FileStores)(nil)

// New returns the stores of a user. Every store is opened with the rules and as owner, and with
// opts, which tests use to put the stores on a fake file system. The index records the tasks that
// have a store.
func New(index *filestore.Index, rules store.Rules, owner filestore.Owner, opts ...filestore.Option) *FileStores {
	return &FileStores{
		index: index, rules: rules, owner: owner, opts: opts,
		stores: map[app.Scope]*filestore.Store{}, opening: map[app.Scope]*opening{},
	}
}

// Close implements app.Stores. An open still in progress gives its store up when it returns.
func (f *FileStores) Close() error {
	f.mu.Lock()
	f.closed = true
	open := slices.Collect(maps.Values(f.stores))
	clear(f.stores)
	f.mu.Unlock()
	var errs []error
	for _, s := range open {
		errs = append(errs, s.Close())
	}
	return errors.Join(errs...)
}

// Forget implements app.Stores.
func (f *FileStores) Forget(_ context.Context, scope app.Scope) error {
	f.drop(scope, nil)
	return f.index.Remove(filestore.Task{Repository: scope.Repository, Name: scope.Task})
}

// drop gives up the store of a scope, if it is still the one a caller saw.
func (f *FileStores) drop(scope app.Scope, seen *filestore.Store) {
	f.mu.Lock()
	s, ok := f.stores[scope]
	if ok && (seen == nil || s == seen) {
		delete(f.stores, scope)
	} else {
		ok = false
	}
	f.mu.Unlock()
	if ok {
		_ = s.Close()
	}
}

// Find implements app.Stores. A store is opened once and then shared, since opening it makes this
// process its one owner. A store on disk that the index lost is taken up again.
func (f *FileStores) Find(ctx context.Context, scope app.Scope) (store.Store, error) {
	return f.get(ctx, scope, false)
}

// Create implements app.Stores.
//
// The store is made before the index records it. A crash between the two leaves a store with no
// jobs and no entry, which loses nothing, and the next call records it. So a store the index has
// was made: if it is missing, its disk is away, and it is not made again over the mount point.
func (f *FileStores) Create(ctx context.Context, scope app.Scope) (store.Store, error) {
	return f.get(ctx, scope, true)
}

func (f *FileStores) get(ctx context.Context, scope app.Scope, create bool) (store.Store, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if err := scope.Validate(); err != nil {
		return nil, err
	}
	for {
		f.mu.Lock()
		if f.closed {
			f.mu.Unlock()
			return nil, errClosed
		}
		if s, ok := f.stores[scope]; ok {
			f.mu.Unlock()
			// A store this process lost, to another owner or with its disk, is opened again: the
			// handle it has now would refuse for good.
			if s.Owned() == nil {
				return s, nil
			}
			f.drop(scope, s)
			continue
		}
		op, busy := f.opening[scope]
		if !busy {
			op = &opening{done: make(chan struct{})}
			f.opening[scope] = op
			f.mu.Unlock()
			f.open(scope, create, op)
			if op.err != nil {
				return nil, op.err
			}
			return op.store, nil
		}
		f.mu.Unlock()
		select {
		case <-op.done:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
		switch {
		case op.err == nil:
			return op.store, nil
		case !create || !errors.Is(op.err, store.ErrNoStore):
			return nil, op.err
		}
		// The caller it waited for only looked. This one makes the store.
	}
}

var errClosed = store.Mark(&core.Failure{Kind: core.FailureUnavailable, Message: "the daemon has given up its stores"}, store.ErrAway)

// open does the work of one opening, with no lock held.
func (f *FileStores) open(scope app.Scope, create bool, op *opening) {
	s, err := f.openTask(scope, create)
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.opening, scope)
	switch {
	case err != nil:
	case f.closed:
		_ = s.Close()
		s, err = nil, errClosed
	default:
		f.stores[scope] = s
	}
	op.store, op.err = s, err
	close(op.done)
}

func (f *FileStores) openTask(scope app.Scope, create bool) (*filestore.Store, error) {
	task := filestore.Task{Repository: scope.Repository, Name: scope.Task}
	entry, err := f.index.Entry(task)
	if err != nil {
		return nil, err
	}
	opts := append([]filestore.Option{filestore.WithRules(f.rules), filestore.WithOwner(f.owner)}, f.opts...)
	switch {
	case entry != "":
		opts = append(opts, filestore.Existing(entry))
	case !create:
		opts = append(opts, filestore.LookOnly())
	}
	s, err := filestore.OpenTask(scope.Repository, scope.Task, opts...)
	if err != nil {
		return nil, err
	}
	if err := f.index.Add(task); err != nil {
		_ = s.Close()
		return nil, fmt.Errorf("recording the store of task %s in %s: %w", scope.Task, scope.Repository, err)
	}
	return s, nil
}

// Known implements app.Stores. It returns every task the index has, whether or not its repository
// is there now: a repository that is away is what recovery must know of, to hold the runs of that
// store until it returns. Find says whether a store can be reached.
func (f *FileStores) Known(context.Context) ([]app.Scope, error) {
	tasks, err := f.index.Tasks()
	if err != nil {
		return nil, err
	}
	out := make([]app.Scope, 0, len(tasks))
	for _, t := range tasks {
		out = append(out, app.Scope{Repository: t.Repository, Task: t.Name})
	}
	return out, nil
}
