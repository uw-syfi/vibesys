package filestores

import (
	"context"
	"errors"
	"fmt"
	"os"
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

// Close gives up every store. Process death does the same, so a daemon need not call it.
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
// jobs and no entry, which loses nothing, and the next Open records it.
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
	opts := append([]filestore.Option{filestore.WithRules(f.rules), filestore.WithOwner(f.owner)}, f.opts...)
	s, err := filestore.OpenTask(scope.Repository, scope.Task, opts...)
	if err != nil {
		return nil, err
	}
	if err := f.index.Add(filestore.Task{Repository: scope.Repository, Name: scope.Task}); err != nil {
		_ = s.Close()
		return nil, fmt.Errorf("recording the store of task %s in %s: %w", scope.Task, scope.Repository, err)
	}
	f.stores[scope] = s
	return s, nil
}

// Known implements app.Stores. A task whose repository is gone from the disk is left out: there
// is nothing to recover in it, and opening its store would make the directory again.
func (f *FileStores) Known(ctx context.Context) ([]app.Scope, error) {
	tasks, err := f.index.Tasks()
	if err != nil {
		return nil, err
	}
	out := make([]app.Scope, 0, len(tasks))
	for _, t := range tasks {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		info, err := os.Stat(t.Repository)
		if err != nil || !info.IsDir() {
			continue
		}
		out = append(out, app.Scope{Repository: t.Repository, Task: t.Name})
	}
	return out, nil
}
