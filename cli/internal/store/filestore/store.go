package filestore

import (
	"errors"
	"fmt"
	"io/fs"
	"path"
	"slices"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
	"github.com/uw-syfi/vibesys/cli/internal/store/ledger"
)

// Store is a store.Store on disk, safe for concurrent use within the process that opened it.
// Create it with Open or OpenTask.
type Store struct {
	*ledger.Engine
	guard *guard
}

// Option configures Open and OpenTask.
type Option func(*config)

type config struct {
	fs    fsys.FS
	rules store.Rules
	owner Owner
	// entry is set when the store is known to exist, and names what says so.
	entry string
	// lookOnly says the caller wants a store that is there, and nothing made.
	lookOnly bool
}

func optionsOf(opts []Option) config {
	c := config{fs: fsys.OS{}}
	for _, opt := range opts {
		opt(&c)
	}
	return c
}

// WithFS sets the filesystem the store works on. Tests give it a fault-injecting one. The
// default is the operating system's.
func WithFS(f fsys.FS) Option { return func(c *config) { c.fs = f } }

// WithRules sets the decisions of internal/core that depend on the task. Without them every saved
// result with a submission's key answers it, and a submission that leaves Samples at zero joins
// nothing.
func WithRules(rules store.Rules) Option { return func(c *config) { c.rules = rules } }

// WithOwner says who is opening the store. Without it the owner has no name, which is enough for
// a program that does not leave work behind it.
func WithOwner(owner Owner) Option { return func(c *config) { c.owner = owner } }

// Existing says the store was made, and entry is the file that says so. Open then fails with
// core.FailureUnavailable and makes nothing if the store is not there: a store that was made and
// is missing is most likely on a disk that is not mounted, and one made now would hide the real
// one and say none of its jobs exist.
func Existing(entry string) Option { return func(c *config) { c.entry = entry } }

// LookOnly says the caller wants the store if it is there. Open then fails with
// core.FailureNotFound, marked store.ErrNoStore, and makes nothing if it is not.
func LookOnly() Option { return func(c *config) { c.lookOnly = true } }

// Open opens the store in the directory root, creating the directory and its parents when they
// are not there. A directory that holds anything else, or records of a newer format, is refused
// with an error that names the file.
//
// The opener owns the store until it calls Close or dies. A store with another owner is refused
// with an error of kind core.FailureConflict that names the store and the owner; see the Owners
// section of the package documentation.
func Open(root string, opts ...Option) (*Store, error) {
	c := optionsOf(opts)
	root = path.Clean(root)
	var made []string
	switch {
	case c.entry != "" || c.lookOnly:
		if err := findStore(c, root); err != nil {
			return nil, err
		}
	default:
		if err := makeAll(c.fs, root, &made); err != nil {
			return nil, unavailable(root, err)
		}
	}
	if err := initialize(c.fs, root); err != nil {
		return nil, prerelease(err, root, c.entry)
	}
	b := newBackend(c.fs, root)
	g, err := claim(b, c.owner)
	if errors.Is(err, fsys.ErrNoLocks) {
		unmake(c.fs, root, made)
	}
	if err != nil {
		return nil, err
	}
	b.guard = g
	if err := migrate(c.fs, root); err != nil {
		_ = g.release()
		return nil, err
	}
	engine, err := ledger.NewEngine(b, c.rules)
	if err != nil {
		_ = g.release()
		return nil, prerelease(err, root, c.entry)
	}
	return &Store{Engine: engine, guard: g}, nil
}

// Close gives up the store. Every later call on it fails, and the next Open may have it.
func (s *Store) Close() error { return s.guard.release() }

// GivenUp returns the jobs the store had running when it was taken over after its owner record was
// removed, which the user does to give up the place that ran them, and has running still as far as
// the record says. A run of one found by this owner is no run of the place given up, and is taken
// back with Reclaim. See the Owners section of the package documentation.
func (s *Store) GivenUp() []core.JobID { return s.guard.givenUp() }

// Reclaim takes a job back from the ones given up, as a run of it was found by this owner. A job
// that is not given up costs nothing.
func (s *Store) Reclaim(job core.JobID) error { return s.guard.reclaim(job) }

// Owned fails, marked store.ErrAway, when the store is no longer the opener's to use: another owner
// took it, its record is gone, or its disk does not answer. A store that failed it is given up and
// opened again.
func (s *Store) Owned() error { return s.guard.check() }

// prerelease completes the refusal of a store of the prerelease encoding with what to remove: the
// store's directory and, when the index records the store, its entry, without which the daemon
// takes the missing directory for a disk that is not mounted. A daemon makes the store again on
// the next submission. Any other error is returned as it is.
func prerelease(err error, root, entry string) error {
	var f *core.Failure
	if !errors.Is(err, errPrerelease) || !errors.As(err, &f) {
		return err
	}
	fix := fmt.Sprintf("remove the directory %s", root)
	if entry != "" {
		fix += fmt.Sprintf(" and the index entry %s", entry)
	}
	return &core.Failure{Kind: f.Kind, Message: strings.TrimSuffix(f.Message, prereleaseFix) + fix +
		", and the next submission makes the store again; no other store is affected"}
}

// findStore fails unless the store that was made is there.
func findStore(c config, root string) error {
	_, err := c.fs.ReadFile(path.Join(root, formatFile))
	if errors.Is(err, fs.ErrNotExist) && c.entry == "" {
		return store.Mark(&core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("no store was made at %s", root)}, store.ErrNoStore)
	}
	if errors.Is(err, fs.ErrNotExist) {
		return store.Mark(&core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(
			"the store at %s is not there, and %s says it was made: its disk may not be mounted, and if it was deleted on purpose, run vibesys-cli daemon forget on its repository",
			root, c.entry)}, store.ErrAway, store.ErrGone)
	}
	if err != nil {
		return unavailable(root, err)
	}
	return nil
}

// OwnerRecord is the file that names the owner of the store of one task of a repository. A user
// removes it to give up the place that ran the store's running jobs: see the Owners section of the
// package documentation.
func OwnerRecord(repository, task string) string {
	return path.Join(taskDir(path.Clean(repository), task), ownerFile)
}

// OpenTask opens the store of one task of a repository. The repository must be there: a store
// makes its own directories, and never a repository.
func OpenTask(repository, task string, opts ...Option) (*Store, error) {
	if err := ledger.CheckName("task", task); err != nil {
		return nil, err
	}
	if _, err := optionsOf(opts).fs.ReadDir(repository); errors.Is(err, fs.ErrNotExist) {
		return nil, store.Mark(&core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("the repository %s is not there: %v", repository, err)}, store.ErrAway, store.ErrGone)
	} else if err != nil {
		return nil, store.Mark(&core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("the repository %s cannot be read: %v", repository, err)}, store.ErrAway)
	}
	s, err := Open(taskDir(repository, task), opts...)
	if err != nil {
		return nil, err
	}
	if err := ignoreState(optionsOf(opts).fs, path.Join(path.Clean(repository), stateDirName), task); err != nil {
		_ = s.Close()
		return nil, err
	}
	return s, nil
}

// ignoreState writes a .gitignore that ignores everything into the state directory of a
// repository, unless one is there, so that no store file reaches a commit made with git add -A. A
// state directory made before this is given one on its next open. A .gitignore the user wrote is
// left as it is. The temporary file is named for the task, whose store this opener owns, so two
// openers of different tasks never write the same one.
func ignoreState(f fsys.FS, dir, task string) error {
	name := path.Join(dir, gitignoreFile)
	_, err := f.ReadFile(name)
	switch {
	case err == nil:
		return nil
	case !errors.Is(err, fs.ErrNotExist):
		return unavailable(dir, err)
	}
	b := newBackend(f, dir)
	tmp := path.Join(dir, tempPrefix+gitignoreFile+"-"+task)
	if err := b.writeFile(tmp, []byte(gitignoreContent)); err != nil {
		_ = f.Remove(tmp)
		return unavailable(dir, err)
	}
	if err := f.Rename(tmp, name); err != nil {
		_ = f.Remove(tmp)
		return unavailable(dir, err)
	}
	if err := f.SyncDir(dir); err != nil {
		return unavailable(dir, err)
	}
	return nil
}

func unavailable(root string, err error) error {
	return store.Mark(&core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("the store at %s cannot be opened: %v", root, err)}, store.ErrAway)
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
	// guard fences every read of the disk and every commit. An index has none: it is shared by
	// whoever runs, and it is only added to.
	guard *guard
}

// chunk is one append to a job's log.
type chunk struct{ offset, size uint64 }

var _ ledger.Backend = (*backend)(nil)

func newBackend(f fsys.FS, root string) *backend {
	return &backend{fs: f, root: root, durableDirs: map[string]bool{}, logs: map[core.JobID][]chunk{}}
}

// makeAll makes the directory name and the parents of it that are missing.
func makeAll(f fsys.FS, name string, made *[]string) error {
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
	if err := makeAll(f, parent, made); err != nil {
		return err
	}
	if err := f.Mkdir(name); err != nil && !errors.Is(err, fs.ErrExist) {
		return err
	}
	*made = append(*made, name)
	return f.SyncDir(parent)
}

// unmake removes a store that was made just now and turned out to be of no use, and the directories
// that were made for it, so that a refusal leaves nothing behind. made is empty for a store that
// was there.
func unmake(f fsys.FS, root string, made []string) {
	if len(made) == 0 {
		return
	}
	for _, name := range append(slices.Clone(topDirs), formatFile, lockFile) {
		_ = f.Remove(path.Join(root, name))
	}
	for _, dir := range slices.Backward(made) {
		_ = f.Remove(dir)
	}
}

// migrate brings a store of an older version this program reads up to formatVersion, under the
// owner's lock. Versions 3 to 5 only added to version 2, so the migration makes the directories
// version 2 did not have and then writes the format file, which is its commit point: a crash before
// it leaves a store of the older version with an empty directory more, which the next Open migrates
// again. The keys of the results of version 4 and older are derived again whenever they are read
// (rekeyed), so no record is rewritten.
func migrate(f fsys.FS, root string) error {
	name := path.Join(root, formatFile)
	data, err := f.ReadFile(name)
	if err != nil {
		return unavailable(root, err)
	}
	var file cliv1.FormatFile
	if err := decode(name, data, &file); err != nil {
		return err
	}
	if file.GetFormat() == formatVersion {
		return nil
	}
	for _, dir := range topDirs {
		if err := f.Mkdir(path.Join(root, dir)); err != nil && !errors.Is(err, fs.ErrExist) {
			return unavailable(root, err)
		}
	}
	if err := f.SyncDir(root); err != nil {
		return unavailable(root, err)
	}
	if err := newBackend(f, root).publish(name, &cliv1.FormatFile{Format: formatVersion}); err != nil {
		return unavailable(root, err)
	}
	return nil
}

// initialize makes the directory root a store when it is empty, and otherwise checks that it is
// one this program reads. The store exists once its format file does, and the file is written
// last, so a crash while making one leaves an empty directory that the next Open finishes.
func initialize(f fsys.FS, root string) error {
	name := path.Join(root, formatFile)
	data, err := f.ReadFile(name)
	if err == nil {
		var file cliv1.FormatFile
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
	if err := b.publish(name, &cliv1.FormatFile{Format: formatVersion}); err != nil {
		return unavailable(root, err)
	}
	return nil
}
