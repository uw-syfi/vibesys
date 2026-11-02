package filestore

import (
	"bytes"
	"errors"
	"fmt"
	"io/fs"
	"path"
	"slices"
	"sync"
	"sync/atomic"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
)

// Owner is who opens a store. Host and Dir together are where the owner's work can be found after
// it is gone; PID only helps a person find a running owner.
type Owner struct {
	Host string
	// Dir is the owner's runtime directory, where its supervisors can be reached.
	Dir string
	PID int
}

// ownerRec is the encoding of .owner. Generation counts the owners the store has had, so that no
// two owners have the same record, not even the same process opening it twice. GivenUp is the
// running jobs whose runs were given up: see the Owners section of the package documentation.
type ownerRec struct {
	Format     int          `json:"format"`
	Generation uint64       `json:"generation"`
	Host       string       `json:"host"`
	Dir        string       `json:"dir"`
	PID        int          `json:"pid"`
	GivenUp    []core.JobID `json:"given_up,omitempty"`
}

func (r ownerRec) String() string {
	return fmt.Sprintf("process %d on %s, runtime directory %s", r.PID, r.Host, r.Dir)
}

// guard is the ownership of an open store. See the Owners section of the package documentation.
type guard struct {
	fs     fsys.FS
	root   string
	lock   fsys.Lock
	closed atomic.Bool

	// mu covers the record, which Reclaim writes again while other calls check it.
	mu   sync.Mutex
	rec  ownerRec
	mine []byte
}

// claim makes the caller the owner of the store, or says why it cannot be.
func claim(b *backend, owner Owner) (*guard, error) {
	lock, err := b.fs.Lock(path.Join(b.root, lockFile))
	switch {
	case errors.Is(err, fsys.ErrLocked):
		return nil, inUse(b, owner)
	case errors.Is(err, fsys.ErrNoLocks):
		return nil, store.Mark(refused(
			"the store at %s cannot be kept to one daemon, because file locks on this filesystem stop at this machine or do not work (%v). "+
				"Keep the repository on a local disk, or have the filesystem mounted with working locks, without nolock or local_lock",
			b.root, err), fsys.ErrNoLocks)
	case err != nil:
		return nil, unavailable(b.root, err)
	}
	g, err := takeRecord(b, owner, lock)
	if err != nil {
		_ = lock.Release()
		return nil, err
	}
	return g, nil
}

// takeRecord writes the caller's record over the last owner's, unless that one's work is live. A
// store with running jobs and no record was given up by the user, who removed the record: those
// jobs are given up, and stay so in every later record while they run.
func takeRecord(b *backend, owner Owner, lock fsys.Lock) (*guard, error) {
	name := path.Join(b.root, ownerFile)
	last, err := readOwner(b, name)
	if err != nil {
		return nil, err
	}
	elsewhere := last != nil && (last.Host != owner.Host || last.Dir != owner.Dir)
	var running []core.JobID
	// Only a record that is gone, names another place or gives jobs up needs the jobs read: the
	// engine reads them next, and says what is wrong with one.
	if last == nil || elsewhere || len(last.GivenUp) > 0 {
		if running, err = b.runningJobs(); err != nil {
			return nil, err
		}
	}
	if elsewhere && len(running) > 0 {
		return nil, store.Mark(refused(
			"the store at %s has running jobs that belong to %s: start the daemon there, or, if that place is gone for good, remove %s and those of them that no run can be found of here will be marked lost",
			b.root, last, name), store.ErrAway)
	}
	rec := ownerRec{Format: formatVersion, Generation: 1, Host: owner.Host, Dir: owner.Dir, PID: owner.PID, GivenUp: running}
	if last != nil {
		rec.Generation = last.Generation + 1
		rec.GivenUp = slices.DeleteFunc(running, func(id core.JobID) bool { return !slices.Contains(last.GivenUp, id) })
	}
	if len(rec.GivenUp) == 0 {
		rec.GivenUp = nil
	}
	g := &guard{fs: b.fs, root: b.root, lock: lock}
	if err := g.write(rec, b.publishBytes); err != nil {
		return nil, err
	}
	return g, nil
}

// write makes rec the record, as this guard's own.
func (g *guard) write(rec ownerRec, publish func(name string, data []byte) error) error {
	data, err := encode(rec)
	if err != nil {
		return err
	}
	if err := publish(path.Join(g.root, ownerFile), data); err != nil {
		return unavailable(g.root, err)
	}
	g.rec, g.mine = rec, data
	return nil
}

// givenUp returns the jobs the record has given up.
func (g *guard) givenUp() []core.JobID {
	g.mu.Lock()
	defer g.mu.Unlock()
	return slices.Clone(g.rec.GivenUp)
}

// reclaim takes a job out of the given-up ones, writing the record again if it was one.
func (g *guard) reclaim(job core.JobID) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	if !slices.Contains(g.rec.GivenUp, job) {
		return nil
	}
	if err := g.checkLocked(); err != nil {
		return err
	}
	rec := g.rec
	rec.GivenUp = slices.DeleteFunc(slices.Clone(rec.GivenUp), func(id core.JobID) bool { return id == job })
	if len(rec.GivenUp) == 0 {
		rec.GivenUp = nil
	}
	// The temporary file has a name no commit uses, and the lock on mu keeps a second one out.
	return g.write(rec, func(name string, data []byte) error {
		b := newBackend(g.fs, g.root)
		tmp := path.Join(g.root, tempPrefix+"owner")
		if err := b.writeFile(tmp, data); err != nil {
			_ = g.fs.Remove(tmp)
			return err
		}
		if err := g.fs.Rename(tmp, name); err != nil {
			_ = g.fs.Remove(tmp)
			return err
		}
		return g.fs.SyncDir(g.root)
	})
}

// readOwner reads the record, which is nil when there is none.
func readOwner(b *backend, name string) (*ownerRec, error) {
	data, err := b.fs.ReadFile(name)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, unavailable(b.root, err)
	}
	var rec ownerRec
	if err := decode(name, data, &rec); err != nil {
		return nil, err
	}
	return &rec, nil
}

// leaving is a refusal the caller may try again: see store.ErrOwnerLeaving.
type leaving struct{ *core.Failure }

func (l leaving) Unwrap() []error { return []error{l.Failure, store.ErrOwnerLeaving, store.ErrAway} }

// inUse refuses a store another hold has. A daemon that holds the lock may not have written its
// record yet, and a record that cannot be read has a message of its own at the next Open.
func inUse(b *backend, owner Owner) error {
	const message = "the store at %s is in use by %s: a store has one daemon, so stop that one or use it"
	rec, err := readOwner(b, path.Join(b.root, ownerFile))
	if err != nil || rec == nil {
		return store.Mark(refused(message, b.root, "another daemon"), store.ErrAway)
	}
	failure := refused(message, b.root, rec)
	if rec.Host == owner.Host && rec.Dir == owner.Dir {
		return leaving{failure}
	}
	return store.Mark(failure, store.ErrAway)
}

// runningJobs returns the jobs the store has running, in the order it lists them.
func (b *backend) runningJobs() ([]core.JobID, error) {
	jobs, err := b.loadJobs()
	if err != nil {
		return nil, unavailable(b.root, err)
	}
	var out []core.JobID
	for _, j := range jobs {
		if j.State == core.JobRunning {
			out = append(out, j.ID)
		}
	}
	return out, nil
}

// check fails unless the store still belongs to this guard. A backend with no guard, which is an
// index or a store being made, passes.
func (g *guard) check() error {
	if g == nil {
		return nil
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.checkLocked()
}

// checkLocked is check with mu held.
func (g *guard) checkLocked() error {
	if g.closed.Load() {
		return fenced("the store at %s is closed", g.root)
	}
	name := path.Join(g.root, ownerFile)
	data, err := g.fs.ReadFile(name)
	switch {
	case errors.Is(err, fs.ErrNotExist):
		return fenced("the store at %s no longer has the owner record %s: this daemon stopped using it", g.root, name)
	case err != nil:
		return unavailable(g.root, err)
	case bytes.Equal(data, g.mine):
		return nil
	}
	who := "another daemon"
	var rec ownerRec
	if decode(name, data, &rec) == nil {
		who = rec.String()
	}
	return fenced("the store at %s now belongs to %s: this daemon stopped using it", g.root, who)
}

// fenced is a store this owner may not touch now, which says nothing about the jobs in it.
func fenced(format string, args ...any) error {
	return store.Mark(&core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(format, args...)}, store.ErrAway)
}

// release gives up the store. It lets go of the lock and leaves the record, which is what the next
// owner needs to read.
func (g *guard) release() error {
	g.closed.Store(true)
	return g.lock.Release()
}

func refused(format string, args ...any) *core.Failure {
	return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf(format, args...)}
}
