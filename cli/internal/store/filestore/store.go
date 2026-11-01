package filestore

import (
	"errors"
	"fmt"
	"io/fs"
	"path"
	"slices"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
	"github.com/uw-syfi/vibesys/cli/internal/store/ledger"
)

// Store is a store.Store on disk, safe for concurrent use within the process that opened it.
// Create it with Open or OpenTask.
type Store struct {
	*ledger.Engine
}

// Option configures Open and OpenTask.
type Option func(*config)

type config struct {
	fs    fsys.FS
	rules store.Rules
}

// WithFS sets the filesystem the store works on. Tests give it a fault-injecting one. The
// default is the operating system's.
func WithFS(f fsys.FS) Option { return func(c *config) { c.fs = f } }

// WithRules sets the decisions of internal/core that depend on the task. Without them every saved
// result with a submission's key answers it, and a submission that leaves Samples at zero joins
// nothing.
func WithRules(rules store.Rules) Option { return func(c *config) { c.rules = rules } }

// Open opens the store in the directory root, creating the directory and its parents when they
// are not there. A directory that holds anything else, or records of a newer format, is refused
// with an error that names the file.
func Open(root string, opts ...Option) (*Store, error) {
	c := config{fs: fsys.OS{}}
	for _, opt := range opts {
		opt(&c)
	}
	root = path.Clean(root)
	if err := makeAll(c.fs, root); err != nil {
		return nil, unavailable(root, err)
	}
	if err := initialize(c.fs, root); err != nil {
		return nil, err
	}
	engine, err := ledger.NewEngine(newBackend(c.fs, root), c.rules)
	if err != nil {
		return nil, err
	}
	return &Store{Engine: engine}, nil
}

// OpenTask opens the store of one task of a repository.
func OpenTask(repository, task string, opts ...Option) (*Store, error) {
	if err := ledger.CheckName("task", task); err != nil {
		return nil, err
	}
	return Open(taskDir(repository, task), opts...)
}

func unavailable(root string, err error) error {
	return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("the store at %s cannot be opened: %v", root, err)}
}

// backend is the on-disk ledger.Backend. The Engine calls it one request at a time, so it holds
// no lock.
type backend struct {
	fs   fsys.FS
	root string
	// temps numbers the temporary files of commits, so that two never share a name.
	temps uint64
	// durableDirs holds the directories whose entry in their parent has been synced since the
	// last Recover.
	durableDirs map[string]bool
	// logs holds, for each job, where its log chunks begin and how long they are.
	logs map[core.JobID][]chunk
}

// chunk is one append to a job's log.
type chunk struct{ offset, size uint64 }

var _ ledger.Backend = (*backend)(nil)

func newBackend(f fsys.FS, root string) *backend {
	return &backend{fs: f, root: root, durableDirs: map[string]bool{}, logs: map[core.JobID][]chunk{}}
}

// makeAll makes the directory name and the parents of it that are missing.
func makeAll(f fsys.FS, name string) error {
	_, err := f.ReadDir(name)
	switch {
	case err == nil:
		return nil
	case !errors.Is(err, fs.ErrNotExist):
		return err
	}
	parent := path.Dir(name)
	if parent == name {
		return err
	}
	if err := makeAll(f, parent); err != nil {
		return err
	}
	if err := f.Mkdir(name); err != nil && !errors.Is(err, fs.ErrExist) {
		return err
	}
	return f.SyncDir(parent)
}

// initialize makes the directory root a store when it is empty, and otherwise checks that it is
// one this program reads. The store exists once its format file does, and the file is written
// last, so a crash while making one leaves an empty directory that the next Open finishes.
func initialize(f fsys.FS, root string) error {
	name := path.Join(root, formatFile)
	data, err := f.ReadFile(name)
	if err == nil {
		var file rootFile
		return decode(name, data, &file)
	}
	if !errors.Is(err, fs.ErrNotExist) {
		return unavailable(root, err)
	}
	entries, err := f.ReadDir(root)
	if err != nil {
		return unavailable(root, err)
	}
	var strays []string
	for _, e := range entries {
		if !hidden(e.Name) && !(e.IsDir && slices.Contains(topDirs, e.Name)) {
			strays = append(strays, e.Name)
		}
	}
	if len(strays) > 0 {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
			"%s is not a vibesys-cli store: it has no %s file and it holds %s", root, formatFile, strings.Join(strays, ", "))}
	}
	for _, dir := range topDirs {
		if err := f.Mkdir(path.Join(root, dir)); err != nil && !errors.Is(err, fs.ErrExist) {
			return unavailable(root, err)
		}
	}
	if err := f.SyncDir(root); err != nil {
		return unavailable(root, err)
	}
	b := newBackend(f, root)
	if err := b.publish(name, rootFile{Format: formatVersion}); err != nil {
		return unavailable(root, err)
	}
	return nil
}
