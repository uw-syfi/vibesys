// Package hostlease keeps the leases of a machine that daemons reach over ssh, such as a shared
// GPU host, as advisory locks (flock) on files of one directory there that every user shares. A
// lease is held by a shell on that machine for as long as its connection lives: the daemon gives it
// back by closing the connection's input, and when the daemon or the connection dies, the shell
// ends and the machine's kernel gives the lock back. A crashed holder never blocks anyone.
//
// The machine needs a POSIX sh, flock(1) of util-linux, dd and /proc: a Linux machine. The
// directory and the files are made as package locallease makes them, so the daemons of the
// machine itself and those that reach it over ssh take turns at the same leases.
package hostlease

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os/exec"
	"strings"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
)

// Host is the leases of a machine, kept in a directory there.
type Host struct {
	// Shell reaches the machine.
	Shell remote.Shell
	// Dir is the directory there. When it is empty, the directory is DirName in the first of
	// Parents there that is sticky and writable by every user and whose file system type (stat -f)
	// is none of NetworkTypes, as the daemons of that machine find theirs.
	Dir          string
	Parents      []string
	DirName      string
	NetworkTypes []uint32
}

var _ lease.Leases = Host{}

// Where implements lease.Leases.
func (h Host) Where() string {
	if h.Dir != "" {
		return h.Shell.Where() + " (" + h.Dir + ")"
	}
	return h.Shell.Where() + " (" + h.DirName + " in the first local one of " + strings.Join(h.Parents, ", ") + ")"
}

// resolve is the shell code that sets dir to the directory of leases when it is not given, or
// answers why there is none.
const resolve = `if [ -z "$dir" ]; then
  why=""
  for p in $parents; do
    if [ "$(stat -L -c %a "$p" 2>/dev/null)" != 1777 ]; then why="$why; $p is not there, or not sticky and writable by every user"; continue; fi
    t=$(stat -L -f -c %t "$p" 2>/dev/null) || { why="$why; $p cannot be checked"; continue; }
    case " $network " in *" $t "*) why="$why; $p is on a network file system"; continue;; esac
    dir="$p/$name"; break
  done
  [ -n "$dir" ] || { echo "error: no local directory of leases${why}"; exit 2; }
fi
`

// script takes the lock of a key in a directory and holds it while its input is open. It answers
// on its first line: "acquired", "held" followed by the holder's record, or "error: " and why.
//
// The lock file is opened read-only, which is all flock needs, and is made with noclobber when it
// is not there, so another user's file is never opened to be created. The holder's record is
// written through the open file (/proc/self/fd/9), so a link put in its place cannot turn the
// write elsewhere. The shell waits with its own read, not a child, so the lock dies with it.
const script = `set -u
dir=$1 key=$2 rec=$3 parents=$4 name=$5 network=$6
` + resolve + `f="$dir/$key.lock"
if [ ! -d "$dir" ]; then
  if mkdir -p "$dir" 2>/dev/null; then chmod 1777 "$dir" 2>/dev/null; fi
fi
[ -d "$dir" ] || { echo "error: cannot make the directory $dir"; exit 2; }
if [ ! -e "$f" ] && [ ! -L "$f" ]; then (umask 0; set -C; : >"$f") 2>/dev/null; fi
if [ -L "$f" ] || [ ! -f "$f" ]; then echo "error: $f is not a regular file"; exit 2; fi
exec 9<"$f"
if ! flock -n 9; then echo held; head -c 4096 <&9; exit 3; fi
printf '%s\n' "$rec" | dd of=/proc/self/fd/9 conv=nocreat 2>/dev/null
echo acquired
while read -r _; do :; done
: | dd of=/proc/self/fd/9 conv=nocreat 2>/dev/null
exit 0
`

// Acquire implements lease.Leases. The shell that holds the lease outlives ctx, which bounds only
// its answer.
func (h Host) Acquire(ctx context.Context, key lease.Key, holder core.LeaseHolder) (lease.Lease, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if _, err := lease.ParseKey(string(key)); err != nil {
		return nil, err
	}
	rec, err := lease.EncodeRecord(holder)
	if err != nil {
		return nil, err
	}
	// The lease lives past this call, so its process is not bound to ctx: Release and Lost end it.
	cmd := h.Shell.Command(context.Background(), []string{"sh", "-c", script, "sh", h.Dir, string(key), string(rec), strings.Join(h.Parents, " "), h.DirName, h.networkTypes()})
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		return nil, unavailable("starting the lease %s on %s: %v", key, h.Where(), err)
	}
	l := &Lease{key: key, cmd: cmd, stdin: stdin, lost: make(chan struct{}), exited: make(chan struct{})}
	answer := make(chan string, 1)
	out := bufio.NewReader(stdout)
	go func() {
		line, _ := out.ReadString('\n')
		answer <- strings.TrimSpace(line)
	}()
	var first string
	select {
	case first = <-answer:
	case <-ctx.Done():
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		return nil, ctx.Err()
	}
	switch {
	case first == "acquired":
		go l.watch(stdout)
		return l, nil
	case first == "held":
		rest, _ := io.ReadAll(io.LimitReader(out, 4096))
		_ = cmd.Wait()
		who, err := lease.DecodeRecord(rest)
		if err != nil {
			who = lease.Anonymous()
		}
		return nil, &lease.HeldError{Key: key, Where: h.Where(), Holder: who}
	}
	waitErr := cmd.Wait()
	why := strings.TrimPrefix(first, "error: ")
	if why == "" {
		why = strings.TrimSpace(stderr.String())
	}
	var exit *exec.ExitError
	if errors.As(waitErr, &exit) && h.Shell.Lost(exit.ExitCode()) {
		return nil, unavailable("%s did not answer for the lease %s: %s", h.Shell.Where(), key, why)
	}
	return nil, unavailable("taking the lease %s on %s: %s", key, h.Where(), why)
}

// networkTypes are the file system types stat -f prints, in hexadecimal, separated by spaces.
func (h Host) networkTypes() string {
	var out []string
	for _, t := range h.NetworkTypes {
		out = append(out, fmt.Sprintf("%x", t))
	}
	return strings.Join(out, " ")
}

// Lease is a lease of a machine: the shell there that holds its lock.
type Lease struct {
	key      lease.Key
	cmd      *exec.Cmd
	stdin    io.WriteCloser
	mu       sync.Mutex
	released bool
	lost     chan struct{}
	exited   chan struct{}
	err      error
}

var _ lease.Lease = (*Lease)(nil)

// watch waits for the shell to end, and says the lease was lost if nobody released it.
func (l *Lease) watch(stdout io.Reader) {
	_, _ = io.Copy(io.Discard, stdout)
	l.err = l.cmd.Wait()
	l.mu.Lock()
	released := l.released
	l.mu.Unlock()
	if !released {
		close(l.lost)
	}
	close(l.exited)
}

// Release implements lease.Lease. It closes the shell's input and waits, bounded by ctx, for the
// shell to end, which gives back the lock; past ctx it ends the shell.
func (l *Lease) Release(ctx context.Context) error {
	l.mu.Lock()
	if l.released {
		l.mu.Unlock()
		return nil
	}
	l.released = true
	l.mu.Unlock()
	_ = l.stdin.Close()
	select {
	case <-l.exited:
		return nil
	case <-ctx.Done():
		_ = l.cmd.Process.Kill()
		<-l.exited
		return nil
	}
}

// Lost implements lease.Lease. It is closed when the shell that holds the lock ends without a
// Release, such as when its connection drops.
func (l *Lease) Lost() <-chan struct{} { return l.lost }

// Key implements lease.Lease.
func (l *Lease) Key() lease.Key { return l.key }

// Kill ends the shell that holds the lease without a word, as the death of its daemon or of its
// connection does, and returns once it has.
func (l *Lease) Kill() {
	_ = l.cmd.Process.Kill()
	<-l.exited
}

func unavailable(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureUnavailable, Message: "lease: " + fmt.Sprintf(format, args...)}
}
