package docker

import "context"

// Dirs are the directories of one run: the checkout it runs in, the scratch directory the caller
// reads after it, and the directory its artifacts go to. Scratch and Artifacts may be empty.
type Dirs struct {
	Checkout, Scratch, Artifacts string
}

// Machine is the machine the engine runs containers on, as a run's files see it. A container's
// mounts are paths on that machine, so a run asks the machine for them.
//
// The paths of Dirs inside the container are always the caller's own, so that a path a command is
// given means the same inside: a machine only says where they come from.
type Machine interface {
	// Stage makes a run's directories visible to the engine and returns where they are on its
	// machine. Every directory it returns exists. The artifacts directory it returns is empty.
	Stage(ctx context.Context, d Dirs) (Dirs, error)

	// Collect brings back what the commands wrote that the caller reads: the staged scratch and
	// artifacts directories into the caller's. A run whose files were not brought back is a run
	// whose ending cannot be vouched for.
	Collect(ctx context.Context, local, staged Dirs) error

	// Replace makes the staged checkout a mirror of the caller's while the run's containers are
	// up, so what is not in the caller's is gone from it: a lab replaces its checkout in place.
	Replace(ctx context.Context, local, staged Dirs) error

	// Add copies the file at rel of the caller's artifacts directory to the staged one, replacing
	// what is there, while the run's containers are up: a lab takes files in this way. Nothing else
	// of the staged directory changes.
	Add(ctx context.Context, local, staged Dirs, rel string) error

	// Cache returns the directory on the machine that is kept for the key across runs, made if it
	// is missing.
	Cache(ctx context.Context, key string) (string, error)

	// User is the user and group the commands run as there, so the files they write are that
	// user's.
	User(ctx context.Context) (User, error)

	// Program makes a program of the caller's machine, given by its absolute path, runnable on the
	// engine's machine, and returns its path there. A program built for another platform than the
	// engine's machine's fails with core.FailureUnavailable naming both.
	Program(ctx context.Context, local string) (string, error)
}

// Shared is the Machine of an engine that runs on the caller's own machine with the caller's
// files: every directory is where the caller has it, nothing is copied, and there is no cache. It
// is what a Config with no Machine uses.
type Shared struct{ As User }

var _ Machine = Shared{}

// Stage implements Machine.
func (Shared) Stage(_ context.Context, d Dirs) (Dirs, error) { return d, nil }

// Collect implements Machine.
func (Shared) Collect(context.Context, Dirs, Dirs) error { return nil }

// Replace implements Machine: the commands see the caller's own directories.
func (Shared) Replace(context.Context, Dirs, Dirs) error { return nil }

// Add implements Machine: the commands see the caller's own directories.
func (Shared) Add(context.Context, Dirs, Dirs, string) error { return nil }

// Cache implements Machine. A shared machine has no cache.
func (Shared) Cache(context.Context, string) (string, error) { return "", nil }

// User implements Machine.
func (s Shared) User(context.Context) (User, error) { return s.As, nil }

// Program implements Machine: the engine's machine is the caller's.
func (Shared) Program(_ context.Context, local string) (string, error) { return local, nil }
