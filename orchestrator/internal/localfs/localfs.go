// Package localfs keeps the orchestrator's state off network filesystems.
//
// SQLite's write-ahead log needs shared memory and file locks that NFS, SMB and their kin do not
// keep honest between processes, and the result is a database that is quietly corrupt. So the state
// directory is checked before the database opens anything: Resolve follows every symlink, asks the
// filesystem what the real location is, and returns a Dir only for a local one. Dir is the proof,
// and the store opens nothing without it.
//
// The check fails closed: only filesystem types known to be local pass. A network type is refused
// and named, and so is a type nobody listed, with its statfs number, because a guess of "local" is
// the one answer that cannot be taken back.
//
// There is no flag to override a refusal. The error says what to do: point
// the state directory of the host's configuration at a local path.
//
// Detection sits behind Detector, so every filesystem type is tested with a Fake, and one test
// checks the real thing where the host has both kinds of disk.
//
// The detection tables here duplicate what the vibesys-cli runtime directory code does for its own
// state: this module may not import the cli, so a later change can move both into one shared
// library.
package localfs

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/uw-syfi/vibesys/orchestrator/privatesocket"
)

// Remedy says how an operator chooses a state directory.
const Remedy = "choose one on a local disk with `vibesys-client host configure --state-directory PATH`"

// Filesystem is what a path lives on.
type Filesystem struct {
	// Type is the filesystem's own name for itself: "nfs", "smbfs", "ext4".
	Type string
	// Class says whether Type is known to be local, known to be a network type, or unknown. Only
	// Local passes.
	Class Class
}

// Detector says what filesystem a path is on. The path exists.
type Detector interface {
	Detect(path string) (Filesystem, error)
}

// Dir is a directory proven to be on a local filesystem, symlinks resolved. Only Resolve and
// StateDir make one.
type Dir struct {
	path string
	// chosen is true when the host's configuration named the directory, false when it follows from
	// the root. A chosen directory is the operator's, and its sockets stay inside it or are refused;
	// a default one is the host's to arrange.
	chosen bool
}

// Path is the resolved directory. It may not exist yet.
func (d Dir) Path() string { return d.path }

// RefusedError says a state directory is not on a filesystem known to be local: it is a network
// type, or a type this build does not know.
type RefusedError struct {
	// Path is what the caller asked for, and Resolved is where it really is.
	Path, Resolved string
	Filesystem     Filesystem
	// Default says the path is the default, not one the caller chose.
	Default bool
}

func (e *RefusedError) Error() string {
	which := "state directory"
	if e.Default {
		which = "default state directory"
	}
	where := e.Path
	if e.Resolved != e.Path {
		where = fmt.Sprintf("%s (resolved to %s)", e.Path, e.Resolved)
	}
	what := fmt.Sprintf("on a network filesystem (%s), where SQLite cannot lock safely", e.Filesystem.Type)
	if e.Filesystem.Class != Network {
		what = fmt.Sprintf("on a filesystem this build does not know to be local (%s), and SQLite corrupts quietly on one that is not", e.Filesystem.Type)
	}
	return fmt.Sprintf("the %s %s is %s; %s", which, where, what, Remedy)
}

// Resolve proves a directory local. The directory need not exist: the nearest ancestor that does
// says where it would be made. A path that cannot be examined is refused like a network one,
// because the one answer that must never be a guess is "local".
func Resolve(path string, d Detector) (Dir, error) {
	return resolve(path, d, false)
}

func resolve(path string, d Detector, isDefault bool) (Dir, error) {
	if path == "" {
		return Dir{}, errors.New("state directory is empty")
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return Dir{}, fmt.Errorf("state directory %s: %w", path, err)
	}
	resolved, existing, err := evalExisting(abs)
	if err != nil {
		return Dir{}, fmt.Errorf("state directory %s: %w", path, err)
	}
	fs, err := d.Detect(existing)
	if err != nil {
		return Dir{}, fmt.Errorf("state directory %s: cannot tell what filesystem %s is on (%w); %s",
			path, existing, err, Remedy)
	}
	if fs.Class != Local {
		return Dir{}, &RefusedError{Path: abs, Resolved: resolved, Filesystem: fs, Default: isDefault}
	}
	return Dir{path: resolved, chosen: !isDefault}, nil
}

// evalExisting resolves symlinks in the nearest ancestor of path that exists, and returns the full
// path under it along with that ancestor.
func evalExisting(path string) (resolved, existing string, err error) {
	var rest []string
	cur := path
	for {
		real, err := filepath.EvalSymlinks(cur)
		if err == nil {
			parts := append([]string{real}, rest...)
			return filepath.Join(parts...), real, nil
		}
		if !errors.Is(err, os.ErrNotExist) {
			return "", "", err
		}
		parent := filepath.Dir(cur)
		if parent == cur {
			return "", "", err
		}
		rest = append([]string{filepath.Base(cur)}, rest...)
		cur = parent
	}
}

// StateDir proves local the state directory: chosen when the host's configuration names one, and
// otherwise fallback, the place that follows from the host's root. A fallback on a network filesystem
// is refused, never replaced by another place: a daemon whose state moved without its operator knowing
// would start empty and look like it lost everything.
func StateDir(chosen, fallback string, d Detector) (Dir, error) {
	if chosen != "" {
		if !filepath.IsAbs(chosen) {
			return Dir{}, fmt.Errorf("the state directory %q must be an absolute path; %s", chosen, Remedy)
		}
		return resolve(chosen, d, false)
	}
	if !filepath.IsAbs(fallback) {
		return Dir{}, fmt.Errorf("no home directory to put the default state directory under; %s", Remedy)
	}
	return resolve(fallback, d, true)
}

// agentSocketFile is the agent-only socket's name in a state directory. sessionsName is the
// directory of the sessions' holders, whose sockets are below it as <agent>/<n>/sock; the agent
// directory is AgentDirectoryBytes long and n is a number.
const (
	agentSocketFile = "agent.sock"
	sessionsName    = "sessions"
	// AgentDirectoryBytes is the length of an agent's directory name below Sessions.
	AgentDirectoryBytes = 16
	sessionSocketFile   = "sock"
	// longestSessionNumber is the widest session number a socket is checked for, in a directory the
	// operator chose.
	longestSessionNumber = "9999"
)

// Sessions is the directory below the state directory where each session's holder keeps its files.
func (d Dir) Sessions() string { return filepath.Join(d.path, sessionsName) }

// Place is where the socket whose natural path inside the state directory is natural goes. In a
// default state directory a path that does not fit a socket address goes to the short private place
// derived from it (privatesocket.Short), so a long home directory is not something a user has to
// know about. In a chosen one it stays: the operator put the directory there, and SocketPath
// refuses it when it does not fit.
func (d Dir) Place(natural string) string {
	if d.chosen {
		return natural
	}
	return privatesocket.Place(natural)
}

// SocketPath is Place, or an error that says what to do when the place is still too long for a
// socket address.
func (d Dir) SocketPath(natural string) (string, error) {
	path := d.Place(natural)
	if !privatesocket.Fits(path) {
		return "", fmt.Errorf("the socket path %s is longer than %d bytes, which a Unix socket cannot hold; %s", path, privatesocket.MaxPath, Remedy)
	}
	return path, nil
}

// AgentSocket is the agent-only socket of a state directory.
func (d Dir) AgentSocket() (string, error) {
	return d.SocketPath(filepath.Join(d.path, agentSocketFile))
}

// CheckSessionSockets says whether the sockets of sessions would find a place in this state
// directory, as AgentSocket does for the agent socket. Only a chosen directory can fail.
func (d Dir) CheckSessionSockets() error {
	_, err := d.SocketPath(filepath.Join(d.Sessions(), strings.Repeat("a", AgentDirectoryBytes), longestSessionNumber, sessionSocketFile))
	return err
}

// DefaultPath is where the state goes when nothing says otherwise: $XDG_STATE_HOME/vibesys/orchestrator
// when that is an absolute path, else ~/.local/state/vibesys/orchestrator. It is the same on every
// platform, so a documented path is true everywhere.
func DefaultPath(getenv func(string) string, home string) string {
	base := getenv("XDG_STATE_HOME")
	if !filepath.IsAbs(base) {
		base = filepath.Join(home, ".local", "state")
	}
	return filepath.Join(base, "vibesys", "orchestrator")
}
