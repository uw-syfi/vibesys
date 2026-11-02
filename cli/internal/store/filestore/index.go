package filestore

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"path"
	"slices"
	"strings"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/ledger"
)

// Task names the store of one task in one repository.
type Task struct {
	Repository string
	Name       string
}

// Index is the per-user list of the tasks that have a store, kept so that a daemon that starts
// can find every store with jobs to recover without searching the disk. It holds pointers only: a
// store never depends on it, and a lost index loses the way to the stores, not their contents.
//
// It lives apart from the stores, in a directory the user owns, one file per task, each written
// whole by one rename like a record of a store. Create it with OpenIndex.
type Index struct {
	mu  sync.Mutex
	b   *backend
	dir string
}

// IndexDir returns where a user's index is: under XDG_STATE_HOME when it is set to an absolute
// path, and otherwise under the state directory of the user's home.
func IndexDir(getenv func(string) string, home string) string {
	if state := getenv("XDG_STATE_HOME"); state != "" && path.IsAbs(state) {
		return path.Join(state, "vibesys")
	}
	return path.Join(home, ".local", "state", "vibesys")
}

const tasksDir = "tasks"

// OpenIndex opens the index in the directory dir, making the directory and its parents when they
// are missing. WithFS is honoured and WithRules is not used.
func OpenIndex(dir string, opts ...Option) (*Index, error) {
	c := optionsOf(opts)
	dir = path.Clean(dir)
	if err := makeAll(c.fs, path.Join(dir, tasksDir)); err != nil {
		return nil, unavailable(dir, err)
	}
	return &Index{b: newBackend(c.fs, dir), dir: dir}, nil
}

type taskFile struct {
	Format     int    `json:"format"`
	Repository string `json:"repository"`
	Task       string `json:"task"`
}

func (x *Index) entry(t Task) string {
	sum := sha256.Sum256([]byte(t.Repository + "\x00" + t.Name))
	return path.Join(x.dir, tasksDir, hex.EncodeToString(sum[:12])+recordSuffix)
}

// Add records that the task has a store. It does nothing for a task already recorded.
func (x *Index) Add(t Task) error {
	if !path.IsAbs(t.Repository) {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("repository %q: the index records an absolute path", t.Repository)}
	}
	if err := ledger.CheckName("task", t.Name); err != nil {
		return err
	}
	x.mu.Lock()
	defer x.mu.Unlock()
	name := x.entry(t)
	if _, err := x.b.fs.ReadFile(name); err == nil {
		return nil
	} else if !errors.Is(err, fs.ErrNotExist) {
		return unavailable(x.dir, err)
	}
	if err := x.b.publish(name, taskFile{Format: formatVersion, Repository: t.Repository, Task: t.Name}); err != nil {
		return unavailable(x.dir, err)
	}
	return nil
}

// Entry returns the file that records the task, which is empty when the index has no record of it.
func (x *Index) Entry(t Task) (string, error) {
	x.mu.Lock()
	defer x.mu.Unlock()
	name := x.entry(t)
	_, err := x.b.fs.ReadFile(name)
	switch {
	case err == nil:
		return name, nil
	case errors.Is(err, fs.ErrNotExist):
		return "", nil
	}
	return "", unavailable(x.dir, err)
}

// Tasks returns every task recorded, by repository and then name. An entry this program cannot
// read, such as one of a newer format, fails the call and names the file: a daemon that skipped
// it would leave that task's jobs unrecovered without a word.
func (x *Index) Tasks() ([]Task, error) {
	x.mu.Lock()
	defer x.mu.Unlock()
	entries, err := x.b.fs.ReadDir(path.Join(x.dir, tasksDir))
	if err != nil {
		return nil, unavailable(x.dir, err)
	}
	var out []Task
	for _, e := range entries {
		if hidden(e.Name) || e.IsDir || !strings.HasSuffix(e.Name, recordSuffix) {
			continue
		}
		name := path.Join(x.dir, tasksDir, e.Name)
		data, err := x.b.fs.ReadFile(name)
		if err != nil {
			return nil, unavailable(x.dir, err)
		}
		var file taskFile
		if err := decode(name, data, &file); err != nil {
			return nil, err
		}
		t := Task{Repository: file.Repository, Name: file.Task}
		if x.entry(t) != name {
			return nil, corrupt(name, "its name and content disagree")
		}
		out = append(out, t)
	}
	slices.SortFunc(out, func(a, b Task) int {
		if c := strings.Compare(a.Repository, b.Repository); c != 0 {
			return c
		}
		return strings.Compare(a.Name, b.Name)
	})
	return out, nil
}
