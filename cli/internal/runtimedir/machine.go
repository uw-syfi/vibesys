package runtimedir

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// ClaimPoll is how long a claim waits between tries of a lock it finds held.
const ClaimPoll = 50 * time.Millisecond

// ResolveMachine is the user's machine directory: where the daemons of one user on this machine
// find each other. It comes from the user id and from what the machine is, never from a variable, so
// that every daemon of the user finds the same one whatever its environment says; a runtime
// directory, which XDG_RUNTIME_DIR moves, cannot be that place.
//
// It is /var/tmp/vibesys-cli-<uid> when /var/tmp is on local disk. On a machine whose /var/tmp is on
// a network file system, such as one whose root is, it is vibesys-cli-machine in /run/user/<uid>,
// the user's own memory-backed directory; a machine with neither has none, and a daemon there is
// refused. Whether /var/tmp is local does not change while the machine runs, so two daemons never
// find two directories.
func ResolveMachine(uid int, fsys FS) (string, error) {
	return resolveMachine(uid, fsys, "/var/tmp", "/run/user")
}

func resolveMachine(uid int, fsys FS, varTmp, runUser string) (string, error) {
	network, err := fsys.IsNetwork(varTmp)
	if err != nil {
		return "", fmt.Errorf("checking where %s is: %w", varTmp, err)
	}
	if !network {
		return filepath.Join(varTmp, fmt.Sprintf("vibesys-cli-%d", uid)), nil
	}
	run := filepath.Join(runUser, strconv.Itoa(uid))
	if info, err := os.Stat(run); err == nil && info.IsDir() {
		return filepath.Join(run, "vibesys-cli-machine"), nil
	}
	return "", &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(
		"%s is on a network file system and there is no %s, so this machine has no local directory where the daemons of "+
			"a user find each other; start the daemon from a login session, which has %s", varTmp, run, run)}
}

// Machine is an opened machine directory.
type Machine struct{ path string }

// OpenMachine makes the machine directory if it is missing and checks it as Open checks a runtime
// directory: a real directory, the user's, private to them and on local disk.
func OpenMachine(path string, uid int, fsys FS) (Machine, error) {
	if err := private(path, uid, fsys, "the machine directory", "it must be on local disk"); err != nil {
		return Machine{}, err
	}
	m := Machine{path: path}
	if err := os.MkdirAll(m.sandboxes(), 0o700); err != nil {
		return Machine{}, fmt.Errorf("creating the sandbox directory: %w", err)
	}
	return m, nil
}

// Path is the directory itself.
func (m Machine) Path() string { return m.path }

// Lock is the file whose lock the one daemon of the user on this machine holds, and which says who
// it is.
func (m Machine) Lock() string { return filepath.Join(m.path, "machine.lock") }

// sandboxes is the directory of the sandbox daemons' registration files.
func (m Machine) sandboxes() string { return filepath.Join(m.path, "sandboxes") }

// sandboxFile is the registration file of the sandbox daemon of a runtime directory.
func (m Machine) sandboxFile(runtimeDir string) string {
	sum := sha256.Sum256([]byte(runtimeDir))
	return filepath.Join(m.sandboxes(), hex.EncodeToString(sum[:8])+".lock")
}

// HeldError says another daemon of the user holds the machine. Holder is who it is, as its lock file
// says, and nil when the file does not say yet or cannot be read.
type HeldError struct {
	Path   string
	Holder *cliv1.MachineDaemon
}

func (e *HeldError) Error() string {
	if e.Holder == nil {
		return fmt.Sprintf("another vibesys-cli daemon of this user holds this machine (%s), and does not say yet which", e.Path)
	}
	return fmt.Sprintf("another vibesys-cli daemon of this user holds this machine: pid %d, runtime directory %s, version %s (%s); "+
		"one daemon serves a user on a machine, so use that one, or stop it first. A daemon for a test or a "+
		"second checkout is a sandbox daemon: set VIBESYS_CLI_SANDBOX=1 and an XDG_RUNTIME_DIR of its own",
		e.Holder.GetPid(), e.Holder.GetRuntimeDir(), e.Holder.GetVersion(), e.Path)
}

// Claim is a daemon's hold on the machine, or a sandbox daemon's registration. The operating
// system lets go of it when the process ends, however it ends.
type Claim struct {
	lock *Lock
	// remove is the registration file to remove on Release, empty for the machine lock, which
	// stays for the next daemon to take.
	remove string
}

// Release lets go of the claim.
func (c *Claim) Release() error {
	if c.remove != "" {
		// Removed while still held, so no lister takes a file that is going away for a dead one.
		if err := os.Remove(c.remove); err != nil && !errors.Is(err, os.ErrNotExist) {
			_ = c.lock.Unlock()
			return err
		}
	}
	return c.lock.Unlock()
}

// Claim takes the machine for self, the one daemon of the user on it, and writes who it is into the
// lock file. Holder and Sandboxes take a free lock for a moment to see that it is free, so a claim
// that finds the lock held tries again, every ClaimPoll by the timers, until ctx is done, and then
// fails with a *HeldError naming the holder.
func (m Machine) Claim(ctx context.Context, t clock.Timers, self *cliv1.MachineDaemon) (*Claim, error) {
	return claim(ctx, t, m.Lock(), self, "")
}

// RegisterSandbox records self, a sandbox daemon, for as long as it runs, so that Sandboxes lists it.
// A sandbox daemon holds no machine lock; its runtime directory is its own, so a second one there is
// refused with a *HeldError. It tries as Claim does.
func (m Machine) RegisterSandbox(ctx context.Context, t clock.Timers, self *cliv1.MachineDaemon) (*Claim, error) {
	if !self.GetSandbox() {
		return nil, fmt.Errorf("registering daemon %d as a sandbox: it is not one", self.GetPid())
	}
	path := m.sandboxFile(self.GetRuntimeDir())
	return claim(ctx, t, path, self, path)
}

func claim(ctx context.Context, t clock.Timers, path string, self *cliv1.MachineDaemon, remove string) (*Claim, error) {
	if err := protovalidate.Validate(self); err != nil {
		return nil, fmt.Errorf("the daemon to record: %w", err)
	}
	data, err := protojson.Marshal(self)
	if err != nil {
		return nil, err
	}
	for {
		lock, err := TryLock(path)
		switch {
		case err == nil:
			if err := lock.record(data); err != nil {
				_ = lock.Unlock()
				return nil, fmt.Errorf("recording the daemon in %s: %w", path, err)
			}
			return &Claim{lock: lock, remove: remove}, nil
		case !errors.Is(err, ErrHeld):
			return nil, err
		}
		if !clock.Sleep(ctx, t, ClaimPoll) {
			holder, _ := readHolder(path)
			return nil, &HeldError{Path: path, Holder: holder}
		}
	}
}

// Holder is the daemon that holds the machine, and nil when none does.
func (m Machine) Holder() (*cliv1.MachineDaemon, error) {
	return live(m.Lock())
}

// Sandboxes are the sandbox daemons of the user on this machine that run, by runtime directory. A
// registration whose daemon ended, however it ended, is not one: its lock is free.
func (m Machine) Sandboxes() ([]*cliv1.MachineDaemon, error) {
	entries, err := os.ReadDir(m.sandboxes())
	if err != nil {
		return nil, err
	}
	var out []*cliv1.MachineDaemon
	for _, e := range entries {
		if !strings.HasSuffix(e.Name(), ".lock") {
			continue
		}
		d, err := live(filepath.Join(m.sandboxes(), e.Name()))
		if err != nil {
			return nil, err
		}
		if d != nil {
			out = append(out, d)
		}
	}
	slices.SortFunc(out, func(a, b *cliv1.MachineDaemon) int { return strings.Compare(a.GetRuntimeDir(), b.GetRuntimeDir()) })
	return out, nil
}

// live is the daemon a lock file names while its lock is held, and nil when it is free. A holder
// whose record is not written yet, or not readable, is named by an empty record.
func live(path string) (*cliv1.MachineDaemon, error) {
	lock, err := TryLock(path)
	switch {
	case errors.Is(err, ErrHeld):
		d, err := readHolder(path)
		if err != nil {
			return &cliv1.MachineDaemon{}, nil
		}
		return d, nil
	case err != nil:
		return nil, err
	}
	return nil, lock.Unlock()
}

// readHolder reads the daemon a lock file records.
func readHolder(path string) (*cliv1.MachineDaemon, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	d := &cliv1.MachineDaemon{}
	if err := protojson.Unmarshal(data, d); err != nil {
		return nil, err
	}
	if err := protovalidate.Validate(d); err != nil {
		return nil, err
	}
	return proto.CloneOf(d), nil
}
