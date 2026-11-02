package filestore

import (
	"bytes"
	"errors"
	"fmt"
	"io/fs"
	"path"
	"slices"
	"sync/atomic"

	"github.com/uw-syfi/vibesys/cli/internal/core"
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
// two owners have the same record, not even the same process opening it twice.
type ownerRec struct {
	Format     int    `json:"format"`
	Generation uint64 `json:"generation"`
	Host       string `json:"host"`
	Dir        string `json:"dir"`
	PID        int    `json:"pid"`
}

func (r ownerRec) String() string {
	return fmt.Sprintf("process %d on %s, runtime directory %s", r.PID, r.Host, r.Dir)
}

// guard is the ownership of an open store. See the Owners section of the package documentation.
type guard struct {
	fs     fsys.FS
	root   string
	lock   fsys.Lock
	mine   []byte
	closed atomic.Bool
}

// claim makes the caller the owner of the store, or says why it cannot be.
func claim(b *backend, owner Owner) (*guard, error) {
	lock, err := b.fs.Lock(path.Join(b.root, lockFile))
	switch {
	case errors.Is(err, fsys.ErrLocked):
		return nil, refused("the store at %s is in use by %s: a store has one daemon, so stop that one or use it", b.root, describeOwner(b))
	case errors.Is(err, fsys.ErrNoLocks):
		return nil, refused("the store at %s cannot be kept to one daemon: %v: move the state to a filesystem with working locks, or mount this one without local_lock or nolock", b.root, err)
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

// takeRecord writes the caller's record over the last owner's, unless that one's work is live.
func takeRecord(b *backend, owner Owner, lock fsys.Lock) (*guard, error) {
	name := path.Join(b.root, ownerFile)
	last, err := readOwner(b, name)
	if err != nil {
		return nil, err
	}
	if last != nil && (last.Host != owner.Host || last.Dir != owner.Dir) {
		running, err := b.hasRunningJob()
		if err != nil {
			return nil, err
		}
		if running {
			return nil, refused(
				"the store at %s has running jobs that belong to %s: start the daemon there, or, if that place is gone for good, remove %s and those jobs will be marked lost",
				b.root, last, name)
		}
	}
	rec := ownerRec{Format: formatVersion, Generation: 1, Host: owner.Host, Dir: owner.Dir, PID: owner.PID}
	if last != nil {
		rec.Generation = last.Generation + 1
	}
	data, err := encode(rec)
	if err != nil {
		return nil, err
	}
	if err := b.publishBytes(name, data); err != nil {
		return nil, unavailable(b.root, err)
	}
	return &guard{fs: b.fs, root: b.root, lock: lock, mine: data}, nil
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

// describeOwner names the owner in an error. A daemon that holds the lock may not have written its
// record yet, and a record that cannot be read has a message of its own at the next Open.
func describeOwner(b *backend) string {
	if rec, err := readOwner(b, path.Join(b.root, ownerFile)); err == nil && rec != nil {
		return rec.String()
	}
	return "another daemon"
}

func (b *backend) hasRunningJob() (bool, error) {
	jobs, err := b.loadJobs()
	if err != nil {
		return false, unavailable(b.root, err)
	}
	return slices.ContainsFunc(jobs, func(j core.Job) bool { return j.State == core.JobRunning }), nil
}

// check fails unless the store still belongs to this guard. A backend with no guard, which is an
// index or a store being made, passes.
func (g *guard) check() error {
	if g == nil {
		return nil
	}
	if g.closed.Load() {
		return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("the store at %s is closed", g.root)}
	}
	name := path.Join(g.root, ownerFile)
	data, err := g.fs.ReadFile(name)
	switch {
	case errors.Is(err, fs.ErrNotExist):
		return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(
			"the store at %s no longer has the owner record %s: this daemon stopped using it", g.root, name)}
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
	return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(
		"the store at %s now belongs to %s: this daemon stopped using it", g.root, who)}
}

// release gives up the store. It lets go of the lock and leaves the record, which is what the next
// owner needs to read.
func (g *guard) release() error {
	g.closed.Store(true)
	return g.lock.Release()
}

func refused(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf(format, args...)}
}
