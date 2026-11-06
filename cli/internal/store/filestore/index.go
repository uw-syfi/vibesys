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

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
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

// EnvStateHome names the variable that moves a user's state.
const EnvStateHome = "XDG_STATE_HOME"

// StateHome returns the directory an environment keeps state under: EnvStateHome when it is set to an
// absolute path, and otherwise the state directory of the user's home.
func StateHome(getenv func(string) string, home string) string {
	if state := getenv(EnvStateHome); state != "" && path.IsAbs(state) {
		return path.Clean(state)
	}
	return path.Join(home, ".local", "state")
}

// IndexDirIn returns where the index of a state home is.
func IndexDirIn(stateHome string) string { return path.Join(stateHome, "vibesys") }

// IndexDir returns where a user's index is, for the state home of the environment.
func IndexDir(getenv func(string) string, home string) string {
	return IndexDirIn(StateHome(getenv, home))
}

const tasksDir = "tasks"

// OpenIndex opens the index in the directory dir, making the directory and its parents when they
// are missing. WithFS is honoured and WithRules is not used.
func OpenIndex(dir string, opts ...Option) (*Index, error) {
	c := optionsOf(opts)
	dir = path.Clean(dir)
	// What it makes it keeps: the index belongs to the daemon's own state, which nothing refuses.
	var made []string
	if err := makeAll(c.fs, path.Join(dir, tasksDir), &made); err != nil {
		return nil, unavailable(dir, err)
	}
	return &Index{b: newBackend(c.fs, dir), dir: dir}, nil
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
	if err := x.b.publish(name, &cliv1.TaskFile{Format: formatVersion, Repository: t.Repository, Task: t.Name}); err != nil {
		return unavailable(x.dir, err)
	}
	return nil
}

// Remove forgets a task. It is idempotent.
func (x *Index) Remove(t Task) error {
	x.mu.Lock()
	defer x.mu.Unlock()
	err := x.b.fs.Remove(x.entry(t))
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		return unavailable(x.dir, err)
	}
	if err := x.b.fs.SyncDir(path.Join(x.dir, tasksDir)); err != nil {
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

// Tasks returns every task recorded, by repository and then name. An entry is only a pointer, so
// one of a version this program does not read is still read for its task when it has the shape
// of this version's entry and its name agrees with its content: the store it points to then
// refuses on its own when it is opened, naming its files, and the daemon keeps it known and
// refused while every other store works. An entry this program cannot read at all, damaged or
// of another shape, fails the call and names the file: a daemon that skipped it would leave
// that task's jobs unrecovered without a word.
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
		var file cliv1.TaskFile
		if err := decode(name, data, &file); err != nil {
			var f *core.Failure
			if !errors.As(err, &f) || f.Kind != core.FailureIncompatible || recordReader.Unmarshal(data, &file) != nil {
				return nil, err
			}
		}
		t := Task{Repository: file.GetRepository(), Name: file.GetTask()}
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
