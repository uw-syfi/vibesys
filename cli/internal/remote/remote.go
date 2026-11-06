// Package remote reaches a machine to run programs on and copy directories to and from: this
// machine, or another one over ssh and rsync. It knows nothing of Docker or Slurm; the packages
// that drive those run their programs through a Shell.
//
// Every call is bounded by the caller's context. An exit code that is the transport's own, such as
// ssh's 255 when the connection fails, is reported as such (Shell.Lost), so a caller never takes
// it for the program's: an exit status nobody saw is unknown, never a success.
package remote

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os/exec"
	"strings"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Shell runs programs on one machine.
type Shell interface {
	// Command returns the command that runs argv there. ctx ends the local process, as
	// exec.CommandContext does; a program on another machine learns of it when its connection
	// closes.
	Command(ctx context.Context, argv []string) *exec.Cmd
	// Lost reports whether an exit code is the transport's own, so how the program ended is not
	// known.
	Lost(code int) bool
	// Where names the machine in messages.
	Where() string
}

// Local is this machine.
type Local struct{}

var _ Shell = Local{}

// Command implements Shell.
func (Local) Command(ctx context.Context, argv []string) *exec.Cmd {
	return exec.CommandContext(ctx, argv[0], argv[1:]...)
}

// Lost implements Shell. A program run here is waited for directly, so every exit code is its own.
func (Local) Lost(int) bool { return false }

// Where implements Shell.
func (Local) Where() string { return "this machine" }

// SSH is another machine, reached with ssh as the user's own config says. Prompts are off, so a
// connection that would ask for a password fails instead of hanging.
type SSH struct {
	// Host is the ssh destination or alias.
	Host string
	// Program is ssh and any arguments of the operator's, before the options this adds.
	Program []string
}

var _ Shell = SSH{}

// sshLost is the exit code ssh reports for its own failures.
const sshLost = 255

// options are what every ssh call adds: no prompts, and a dead connection noticed in about three
// minutes.
var options = []string{"-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=6"}

// argv is the ssh command line without the remote command.
func (s SSH) argv() []string {
	out := append([]string(nil), s.Program...)
	return append(append(out, options...), s.Host)
}

// Command implements Shell. The remote shell parses the command line ssh sends, so argv is quoted
// for it word by word, and arrives as it was given.
func (s SSH) Command(ctx context.Context, argv []string) *exec.Cmd {
	a := append(s.argv(), "--", Quote(argv))
	return exec.CommandContext(ctx, a[0], a[1:]...)
}

// Lost implements Shell.
func (SSH) Lost(code int) bool { return code == sshLost }

// Where implements Shell.
func (s SSH) Where() string { return s.Host }

// Quote quotes argv for a POSIX shell, so that the shell splits it back into the same words.
func Quote(argv []string) string {
	words := make([]string, len(argv))
	for i, a := range argv {
		words[i] = quoteWord(a)
	}
	return strings.Join(words, " ")
}

func quoteWord(w string) string {
	if w != "" && strings.Trim(w, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-./=:,@%+") == "" {
		return w
	}
	return "'" + strings.ReplaceAll(w, "'", `'"'"'`) + "'"
}

// Output runs argv on the machine to its end and returns what it wrote to standard output. A
// program that exits non-zero is an error that carries the end of what it wrote to standard error;
// one the transport lost is unavailable.
func Output(ctx context.Context, sh Shell, argv []string, stdin io.Reader) (string, error) {
	cmd := sh.Command(ctx, argv)
	var out, errb bytes.Buffer
	cmd.Stdin, cmd.Stdout, cmd.Stderr = stdin, &out, &errb
	err := cmd.Run()
	if ctx.Err() != nil {
		return "", ctx.Err()
	}
	if err == nil {
		return out.String(), nil
	}
	var exit *exec.ExitError
	what := strings.Join(argv, " ")
	if len(what) > 80 {
		what = what[:80] + "..."
	}
	msg := clip(strings.TrimSpace(errb.String()))
	switch {
	case errors.As(err, &exit) && sh.Lost(exit.ExitCode()):
		return "", &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("cannot reach %s to run %s: %s", sh.Where(), what, msg)}
	case errors.As(err, &exit):
		return "", &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("%s on %s exited with %d: %s", what, sh.Where(), exit.ExitCode(), msg)}
	}
	return "", &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("cannot run %s on %s: %v", what, sh.Where(), err)}
}

func clip(s string) string {
	const max = 600
	if len(s) > max {
		return "..." + strings.ToValidUTF8(s[len(s)-max:], "")
	}
	return s
}

// Rsync copies directories, and files to the host, between this machine and an ssh host.
type Rsync struct {
	SSH SSH
	// Program is rsync and any arguments of the operator's.
	Program []string
}

// Push makes remote a copy of local. With mirror, files that local does not have are deleted.
func (r Rsync) Push(ctx context.Context, local, remote string, mirror bool) error {
	return r.copy(ctx, strings.TrimSuffix(local, "/")+"/", r.SSH.Host+":"+strings.TrimSuffix(remote, "/")+"/", mirror)
}

// PushFile copies the file local to the path remote, which is replaced if it exists.
func (r Rsync) PushFile(ctx context.Context, local, remote string) error {
	return r.copy(ctx, local, r.SSH.Host+":"+remote, false)
}

// Pull makes local a copy of remote. With mirror, files that remote does not have are deleted.
func (r Rsync) Pull(ctx context.Context, remote, local string, mirror bool) error {
	return r.copy(ctx, r.SSH.Host+":"+strings.TrimSuffix(remote, "/")+"/", strings.TrimSuffix(local, "/")+"/", mirror)
}

func (r Rsync) copy(ctx context.Context, from, to string, mirror bool) error {
	argv := append([]string(nil), r.Program...)
	argv = append(argv, "-a", "-e", Quote(r.SSH.argv()[:len(r.SSH.argv())-1]))
	if mirror {
		argv = append(argv, "--delete")
	}
	argv = append(argv, "--", from, to)
	_, err := Output(ctx, Local{}, argv, nil)
	return err
}

// Bounded returns ctx limited to d by the timers, and the function that releases it. A call that
// should answer at once is bounded so that a machine that does not answer cannot hold it forever.
func Bounded(ctx context.Context, timers clock.Timers, d time.Duration) (context.Context, func()) {
	fired, stop := timers.Timeout(ctx, d)
	bounded, cancel := context.WithCancel(ctx)
	var wg sync.WaitGroup
	wg.Go(func() {
		select {
		case <-fired:
			cancel()
		case <-bounded.Done():
		}
	})
	return bounded, func() {
		cancel()
		stop()
		wg.Wait()
	}
}

// ProcFS is the /proc of a machine as an fs.FS, for cpu.Proc: a file is read whole with cat when it
// is opened, each read bounded by Timeout.
type ProcFS struct {
	Shell   Shell
	Timers  clock.Timers
	Timeout time.Duration
}

var _ fs.FS = ProcFS{}

// Open implements fs.FS.
func (p ProcFS) Open(name string) (fs.File, error) {
	if !fs.ValidPath(name) || strings.Contains(name, "/") {
		return nil, &fs.PathError{Op: "open", Path: name, Err: fs.ErrInvalid}
	}
	ctx, done := Bounded(context.Background(), p.Timers, p.Timeout)
	defer done()
	text, err := Output(ctx, p.Shell, []string{"cat", "/proc/" + name}, nil)
	if err != nil {
		return nil, &fs.PathError{Op: "open", Path: name, Err: err}
	}
	return &file{name: name, Reader: bytes.NewReader([]byte(text)), size: int64(len(text))}, nil
}

// file is a file read whole, in memory.
type file struct {
	*bytes.Reader
	name string
	size int64
}

func (f *file) Stat() (fs.FileInfo, error) { return info{f.name, f.size}, nil }
func (f *file) Close() error               { return nil }

type info struct {
	name string
	size int64
}

func (i info) Name() string       { return i.name }
func (i info) Size() int64        { return i.size }
func (i info) Mode() fs.FileMode  { return 0o444 }
func (i info) ModTime() time.Time { return time.Time{} }
func (i info) IsDir() bool        { return false }
func (i info) Sys() any           { return nil }
