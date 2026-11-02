package filestores

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/app"
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

	mu     sync.Mutex
	stores map[app.Scope]*filestore.Store
}

var _ app.Stores = (*FileStores)(nil)

// New returns the stores of a user. Every store is opened with the rules and as owner, and with
// opts, which tests use to put the stores on a fake file system. The index records the tasks that
// have a store.
func New(index *filestore.Index, rules store.Rules, owner filestore.Owner, opts ...filestore.Option) *FileStores {
	return &FileStores{index: index, rules: rules, owner: owner, opts: opts, stores: map[app.Scope]*filestore.Store{}}
}

// Close implements app.Stores.
func (f *FileStores) Close() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	var errs []error
	for scope, s := range f.stores {
		errs = append(errs, s.Close())
		delete(f.stores, scope)
	}
	return errors.Join(errs...)
}

// Open implements app.Stores. A store is opened once and then shared, since opening it makes this
// process its one owner.
//
// The store is made before the index records it. A crash between the two leaves a store with no
// jobs and no entry, which loses nothing, and the next Open records it. So a store the index has
// was made: if it is missing, its disk is away, and it is not made again over the mount point.
func (f *FileStores) Open(ctx context.Context, scope app.Scope) (store.Store, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if err := scope.Validate(); err != nil {
		return nil, err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if s, ok := f.stores[scope]; ok {
		return s, nil
	}
	task := filestore.Task{Repository: scope.Repository, Name: scope.Task}
	entry, err := f.index.Entry(task)
	if err != nil {
		return nil, err
	}
	opts := append([]filestore.Option{filestore.WithRules(f.rules), filestore.WithOwner(f.owner)}, f.opts...)
	if entry != "" {
		opts = append(opts, filestore.Existing(entry))
	}
	s, err := filestore.OpenTask(scope.Repository, scope.Task, opts...)
	if err != nil {
		return nil, err
	}
	if err := f.index.Add(task); err != nil {
		_ = s.Close()
		return nil, fmt.Errorf("recording the store of task %s in %s: %w", scope.Task, scope.Repository, err)
	}
	f.stores[scope] = s
	return s, nil
}

// Known implements app.Stores. It returns every task the index has, whether or not its repository
// is there now: a repository that is away is what recovery must know of, to hold the runs of that
// store until it returns. Open says whether a store can be reached.
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
