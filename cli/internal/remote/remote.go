// Package remote reaches a machine to run programs on and copy directories to and from: this
// machine, or another one over ssh and rsync. It knows nothing of Docker or Slurm; the packages
// that drive those run their programs through a Shell.
//
// Every call is bounded by the caller's context. An exit code that is the transport's own, such as
// ssh's 255 when the connection fails, is reported as such (Shell.Lost), so a caller never takes
// it for the program's: an exit status nobody saw is unknown, never a success.
//
// A call whose transport failed before its program started (Shell.Unreached), such as an ssh
// connection a login node closed during its key exchange, never ran, so it is made again, as
// Reconnect says, for a few minutes before it is reported: long enough for a login node that
// throttles new connections (sshd's MaxStartups) to take them again.
package remote

import (
	"bytes"
	"context"
	"errors"
	"fmt"
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
	// Unreached reports whether a call that exited with code, having written stderr, failed before
	// its program started: the program did not run, so the call is safe to make again whatever it
	// does. A call that may have reached its program is never unreached.
	Unreached(code int, stderr string) bool
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

// Unreached implements Shell. A program run here is started directly, so it is never unreached.
func (Local) Unreached(int, string) bool { return false }

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

// options are what every ssh call adds: no prompts, a connection that is not set up within 30
// seconds given up on, and a dead connection noticed in about three minutes. A login node whose
// sshd throttles new connections, or whose listen queue is full, can leave a connection waiting
// for its banner, or its TCP handshake unanswered, for minutes; with ConnectTimeout ssh then says
// so ("Connection timed out during banner exchange", "ssh: connect to host ...: Connection timed
// out"), which is a call that did not reach the machine, made again as Reconnect says. An
// operator's own -o ConnectTimeout in Program comes first, and ssh keeps the first value.
var options = []string{"-o", "BatchMode=yes", "-o", "ConnectTimeout=30", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=6"}

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

// unconnected are the starts of the line ssh ends with when it could not connect: it failed to
// reach the host, or the host closed or reset the connection during the key exchange or the
// authentication, which is what a login node at its limit of unauthenticated connections does.
// ssh says these before it opens a session, so the remote command did not run. A connection lost
// after that ends in another line ("Connection to host-a closed by remote host.", "client_loop:
// ...", "Timeout, server host-a not responding."), and the command may have run.
var unconnected = []string{
	"kex_exchange_identification: ",
	"ssh_exchange_identification: ",
	"Connection closed by ",
	"Connection reset by ",
	"Connection timed out during banner exchange",
	"ssh: connect to host ",
}

// Unreached implements Shell: ssh exited with its own code, and the last line it wrote says it
// could not connect.
func (SSH) Unreached(code int, stderr string) bool {
	if code != sshLost {
		return false
	}
	lines := strings.Split(strings.TrimSpace(stderr), "\n")
	last := strings.TrimSpace(lines[len(lines)-1])
	for _, start := range unconnected {
		if strings.HasPrefix(last, start) {
			return true
		}
	}
	return false
}

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

// The policy of Reconnect.
const (
	// ReconnectAttempts is how many times in all a call that does not reach its machine is made.
	ReconnectAttempts = 14
	// ReconnectWait is the wait before the second attempt; each later wait is twice the one before,
	// up to ReconnectMaxWait, so the waits are 1s, 2s, 4s, 8s, 16s and then 30s eight times:
	// ReconnectWindow in all, which outlasts a login node that refuses new connections for a
	// minute or two (sshd's MaxStartups), as a busy one does.
	ReconnectWait = time.Second
	// ReconnectMaxWait is the longest wait between two attempts.
	ReconnectMaxWait = 30 * time.Second
	// ReconnectWindow is how long the waits between the attempts add up to.
	ReconnectWindow = 4*time.Minute + 31*time.Second
)

// Reconnect calls try until it returns a call that reached the machine, and returns that call's
// result: try reports with unreached that its call failed before its program started. Such a call
// is made again after a wait, ReconnectWait and then twice the one before each time up to
// ReconnectMaxWait, up to ReconnectAttempts in all; the last unreached call's result is returned,
// its error saying how many were made. A wait that ctx ends returns the last result at once.
func Reconnect[T any](ctx context.Context, timers clock.Timers, try func() (result T, unreached bool, err error)) (T, error) {
	wait := ReconnectWait
	for attempt := 1; ; attempt++ {
		res, unreached, err := try()
		if !unreached {
			return res, err
		}
		if attempt == ReconnectAttempts {
			return res, fmt.Errorf("%w (not reached in %d attempts over %v)", err, attempt, ReconnectWindow)
		}
		if !clock.Sleep(ctx, timers, wait) {
			return res, err
		}
		wait = min(2*wait, ReconnectMaxWait)
	}
}

// Unreached says whether a call of sh that ended with err, from exec.Cmd's Run or Wait, having
// written stderr, failed before its program started, as Shell.Unreached says.
func Unreached(sh Shell, err error, stderr string) bool {
	var exit *exec.ExitError
	return errors.As(err, &exit) && sh.Unreached(exit.ExitCode(), stderr)
}

// Output runs argv on the machine to its end and returns what it wrote to standard output, with
// stdin as its input. A program that exits non-zero is an error that carries the end of what it
// wrote to standard error; one the transport lost is unavailable. A call that does not reach the
// machine is made again as Reconnect says, waiting on the timers.
func Output(ctx context.Context, timers clock.Timers, sh Shell, argv []string, stdin string) (string, error) {
	return Reconnect(ctx, timers, func() (string, bool, error) { return output(ctx, sh, argv, stdin) })
}

// Within is Output with each attempt bounded by d, on the timers: a call that should answer at once
// is bounded so that a machine that does not answer cannot hold it forever, and the bound is each
// attempt's, so the waits of Reconnect between attempts that did not reach the machine never use
// it up. An attempt that d ends returns a *NoAnswer, and is not made again: it may have run.
func Within(ctx context.Context, timers clock.Timers, sh Shell, d time.Duration, argv []string, stdin string) (string, error) {
	return Reconnect(ctx, timers, func() (string, bool, error) {
		bounded, done := Bounded(ctx, timers, d)
		defer done()
		out, unreached, err := output(bounded, sh, argv, stdin)
		if err != nil && ctx.Err() == nil && bounded.Err() != nil {
			return "", false, &NoAnswer{Where: sh.Where(), Within: d}
		}
		return out, unreached, err
	})
}

// NoAnswer is the failure of a call whose machine did not answer within its bound. It is a
// *core.Failure of kind FailureUnavailable, as errors.As finds it.
type NoAnswer struct {
	// Where names the machine.
	Where string
	// Within is the bound.
	Within time.Duration
}

// Error implements error.
func (n *NoAnswer) Error() string { return n.failure().Error() }

// Unwrap makes the failure what errors.As finds.
func (n *NoAnswer) Unwrap() error { return n.failure() }

func (n *NoAnswer) failure() *core.Failure {
	return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("%s did not answer within %v", n.Where, n.Within)}
}

// output makes one attempt of Output.
func output(ctx context.Context, sh Shell, argv []string, stdin string) (string, bool, error) {
	return run(ctx, sh, argv, stdin, func(err error, stderr string) bool { return Unreached(sh, err, stderr) })
}

// run runs argv on the machine once, as Output says, and reports with unreached whether it did not
// reach it.
func run(ctx context.Context, sh Shell, argv []string, stdin string, unreached func(err error, stderr string) bool) (string, bool, error) {
	cmd := sh.Command(ctx, argv)
	var out, errb bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errb
	if stdin != "" {
		cmd.Stdin = strings.NewReader(stdin)
	}
	err := cmd.Run()
	if ctx.Err() != nil {
		return "", false, ctx.Err()
	}
	if err == nil {
		return out.String(), false, nil
	}
	what := strings.Join(argv, " ")
	if len(what) > 80 {
		what = what[:80] + "..."
	}
	msg := clip(strings.TrimSpace(errb.String()))
	var exit *exec.ExitError
	switch {
	case errors.As(err, &exit) && sh.Lost(exit.ExitCode()):
		return "", unreached(err, errb.String()), &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("cannot reach %s to run %s: %s", sh.Where(), what, msg)}
	case errors.As(err, &exit):
		return "", unreached(err, errb.String()), &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("%s on %s exited with %d: %s", what, sh.Where(), exit.ExitCode(), msg)}
	}
	return "", false, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("cannot run %s on %s: %v", what, sh.Where(), err)}
}

func clip(s string) string {
	const max = 600
	if len(s) > max {
		return "..." + strings.ToValidUTF8(s[len(s)-max:], "")
	}
	return s
}

// Rsync copies directories, and files to the host, between this machine and an ssh host. A copy
// whose ssh could not connect is made again as Reconnect says: a copy makes its destination what
// its source is, so making it again is safe however much of it the first made.
type Rsync struct {
	SSH SSH
	// Program is rsync and any arguments of the operator's.
	Program []string
	// Timers makes the waits between the copies of one that did not connect.
	Timers clock.Timers
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
	_, err := Reconnect(ctx, r.Timers, func() (string, bool, error) { return run(ctx, Local{}, argv, "", rsyncUnreached) })
	return err
}

// rsyncUnreached reports whether rsync failed because its ssh could not connect: rsync exits with
// 255 when its remote shell did, and writes its own lines after ssh's, so any line of stderr may be
// the one ssh ended with.
func rsyncUnreached(err error, stderr string) bool {
	var exit *exec.ExitError
	if !errors.As(err, &exit) || exit.ExitCode() != sshLost {
		return false
	}
	for _, line := range strings.Split(stderr, "\n") {
		if (SSH{}).Unreached(sshLost, line) {
			return true
		}
	}
	return false
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
	text, err := Output(ctx, p.Timers, p.Shell, []string{"cat", "/proc/" + name}, "")
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
