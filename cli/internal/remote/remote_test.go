package remote_test

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"math/rand/v2"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
)

// Whatever the words, a shell splits the quoted line back into them: the remote command is the one
// given, and no word is read as shell syntax.
func TestAQuotedCommandLineIsSplitBackIntoItsWords(t *testing.T) {
	rng := rand.New(rand.NewPCG(3, 4))
	alphabet := []rune("ab -_./='\"\\$`!*?;&|<>(){}[]~#\t\n%@:,+é")
	for i := range 200 {
		words := make([]string, 1+rng.IntN(5))
		for j := range words {
			var b strings.Builder
			for range rng.IntN(8) {
				b.WriteRune(alphabet[rng.IntN(len(alphabet))])
			}
			words[j] = b.String()
		}
		ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
		script := "set -- " + remote.Quote(words) + `; for w in "$@"; do printf '%s\0' "$w"; done`
		out, err := exec.CommandContext(ctx, "/bin/sh", "-c", script).Output()
		cancel()
		if err != nil {
			t.Fatalf("case %d %q: %v", i, words, err)
		}
		got := strings.Split(strings.TrimSuffix(string(out), "\x00"), "\x00")
		if strings.Join(got, "\x01") != strings.Join(words, "\x01") {
			t.Fatalf("case %d: %q came back as %q", i, words, got)
		}
	}
}

func TestOutputSaysWhoFailedAndTheTransportsCodeIsNotTheProgramsOwn(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	out, err := remote.Output(ctx, clock.System{}, remote.Local{}, []string{"/bin/sh", "-c", "echo hi"}, "")
	if err != nil || out != "hi\n" {
		t.Fatalf("%q, %v", out, err)
	}
	_, err = remote.Output(ctx, clock.System{}, remote.Local{}, []string{"/bin/sh", "-c", "echo broken >&2; exit 3"}, "")
	if err == nil || !strings.Contains(err.Error(), "exited with 3") || !strings.Contains(err.Error(), "broken") {
		t.Errorf("a failing program: %v", err)
	}
	if !(remote.SSH{}).Lost(255) || (remote.SSH{}).Lost(1) || (remote.Local{}).Lost(255) {
		t.Errorf("only ssh's own 255 is a lost exit")
	}
}

func TestSSHPutsItsOptionsBeforeTheHostAndTheCommandAfterIt(t *testing.T) {
	cmd := remote.SSH{Host: "host-a", Program: []string{"ssh", "-F", "/cfg"}}.Command(context.Background(), []string{"docker", "ps", "--format", "{{.Names}}"})
	args := cmd.Args
	if args[0] != "ssh" || args[1] != "-F" || !strings.Contains(strings.Join(args, " "), "BatchMode=yes") {
		t.Errorf("args %q", args)
	}
	if args[len(args)-3] != "host-a" || args[len(args)-2] != "--" || args[len(args)-1] != "docker ps --format '{{.Names}}'" {
		t.Errorf("the host and the command end the line: %q", args)
	}
}

// A file of the machine's /proc is read whole, and anything that is not one name in it is refused.
func TestProcFSReadsAFileOfProc(t *testing.T) {
	p := remote.ProcFS{Shell: remote.Local{}, Timers: clock.System{}, Timeout: time.Minute}
	data, err := fs.ReadFile(p, "loadavg")
	if err != nil || len(strings.Fields(string(data))) < 3 {
		t.Errorf("loadavg %q, %v", data, err)
	}
	for _, name := range []string{"../etc/passwd", "self/status", ""} {
		if _, err := p.Open(name); err == nil {
			t.Errorf("opened %q", name)
		}
	}
}

// ssh's last line when it could not connect, so the remote command did not run.
var connectFailures = []string{
	"kex_exchange_identification: Connection closed by remote host",
	"kex_exchange_identification: read: Connection reset by peer",
	"ssh_exchange_identification: Connection closed by remote host",
	"Connection closed by UNKNOWN port 65535",
	"Connection closed by 192.0.2.1 port 22",
	"Connection reset by 192.0.2.1 port 22",
	"Connection timed out during banner exchange",
	"ssh: connect to host host-a port 22: Connection refused",
	"ssh: connect to host host-a port 22: Connection timed out",
}

// ssh's last line when it lost a connection whose session was open, so the command may have run.
var sessionFailures = []string{
	"Connection to host-a closed by remote host.",
	"client_loop: send disconnect: Broken pipe",
	"client_loop: send disconnect: Connection reset by peer",
	"Timeout, server host-a not responding.",
	"Write failed: Broken pipe",
	"the program's own last words",
}

// ssh's call is unreached exactly when ssh exited with its own code and its last line says it could
// not connect, whatever ssh warned of before it, and however the line ends.
func TestSSHTakesOnlyAFailedConnectionForUnreached(t *testing.T) {
	rng := rand.New(rand.NewPCG(5, 6))
	before := []string{"", "Warning: Permanently added 'host-a' to the list of known hosts.\r\n", "kex_exchange_identification: Connection closed by remote host\r\n"}
	ends := []string{"", "\n", "\r\n", "\r\n\n"}
	codes := []int{255, 255, 0, 1, 2, 127, 254}
	for i := range 500 {
		connect := rng.IntN(2) == 0
		last := sessionFailures[rng.IntN(len(sessionFailures))]
		if connect {
			last = connectFailures[rng.IntN(len(connectFailures))]
		}
		stderr := before[rng.IntN(len(before))] + last + ends[rng.IntN(len(ends))]
		code := codes[rng.IntN(len(codes))]
		if got, want := (remote.SSH{}).Unreached(code, stderr), connect && code == 255; got != want {
			t.Fatalf("case %d: exit %d, stderr %q: unreached %v, want %v", i, code, stderr, got, want)
		}
		if (remote.SSH{}).Unreached(code, "") || (remote.Local{}).Unreached(code, stderr) {
			t.Fatalf("case %d: a call that said nothing, or a program run here, is unreached", i)
		}
	}
}

// A call that does not reach its machine is made again after waits that double from
// ReconnectWait up to ReconnectMaxWait, adding up to ReconnectWindow, up to ReconnectAttempts in
// all; one that reaches it, or fails otherwise, is made no more.
func TestReconnectWaitsLongerEachTimeAndStops(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	for unreached := range remote.ReconnectAttempts + 2 {
		timers := clock.NewManualTimers()
		calls := 0
		type answer struct {
			n   int
			err error
		}
		done := make(chan answer, 1)
		go func() {
			n, err := remote.Reconnect(ctx, timers, func() (int, bool, error) {
				calls++
				if calls <= unreached {
					return 0, true, errors.New("not reached")
				}
				return calls, false, nil
			})
			done <- answer{n, err}
		}()
		var waits []time.Duration
		for range min(unreached, remote.ReconnectAttempts-1) {
			select {
			case d := <-timers.Armed():
				waits = append(waits, d)
				timers.Fire()
			case <-ctx.Done():
				t.Fatal("hung")
			}
		}
		got := <-done
		if unreached < remote.ReconnectAttempts {
			if got.err != nil || got.n != unreached+1 {
				t.Errorf("%d unreached: %+v, want the call after them", unreached, got)
			}
		} else if got.err == nil || !strings.Contains(got.err.Error(), fmt.Sprintf("not reached in %d attempts", remote.ReconnectAttempts)) || calls != remote.ReconnectAttempts {
			t.Errorf("%d unreached: %+v after %d calls, want the last failure after %d", unreached, got, calls, remote.ReconnectAttempts)
		}
		var total time.Duration
		for i, d := range waits {
			if want := min(remote.ReconnectWait<<i, remote.ReconnectMaxWait); d != want {
				t.Errorf("%d unreached: wait %d is %v, want %v", unreached, i+1, d, want)
			}
			total += d
		}
		if unreached >= remote.ReconnectAttempts && total != remote.ReconnectWindow {
			t.Errorf("the waits add up to %v, want %v", total, remote.ReconnectWindow)
		}
	}
}

// The window outlasts a login node that refuses new connections for a couple of minutes.
func TestReconnectOutlastsALoginNodeThatThrottlesConnections(t *testing.T) {
	if remote.ReconnectWindow < 4*time.Minute {
		t.Errorf("the calls are made again for %v", remote.ReconnectWindow)
	}
}

// An attempt that does not answer within its bound is a NoAnswer, a failure of its own kind, and is
// not made again: it may have run. The bound is each attempt's, so the waits between attempts that
// did not reach the machine do not use it up.
func TestWithinBoundsEachAttempt(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	calls := 0
	sh := scripted{run: func() string {
		calls++
		if calls == 1 {
			return "echo 'kex_exchange_identification: Connection closed by remote host' >&2; exit 255"
		}
		return "exec sleep 600"
	}}
	timers := clock.NewManualTimers()
	done := make(chan error, 1)
	go func() {
		_, err := remote.Within(ctx, timers, sh, time.Minute, []string{"x"}, "")
		done <- err
	}()
	// The first attempt's bound, which does not fire; the wait after it; the second's bound.
	for i, want := range []time.Duration{time.Minute, remote.ReconnectWait, time.Minute} {
		select {
		case d := <-timers.Armed():
			if d != want {
				t.Fatalf("timeout %d is %v, want %v", i+1, d, want)
			}
		case <-ctx.Done():
			t.Fatal("hung")
		}
		if i > 0 {
			timers.Fire()
		}
	}
	err := <-done
	var na *remote.NoAnswer
	if !errors.As(err, &na) || na.Within != time.Minute || na.Where != "host-a" || calls != 2 {
		t.Fatalf("%v after %d calls, want no answer after the second", err, calls)
	}
	if f := new(core.Failure); !errors.As(err, &f) || f.Kind != core.FailureUnavailable || !strings.Contains(f.Message, "host-a did not answer within 1m0s") {
		t.Errorf("the failure %+v", f)
	}
}

// scripted is an ssh-like shell whose each call runs the shell script run returns, its 255 ssh's.
type scripted struct{ run func() string }

func (s scripted) Command(ctx context.Context, _ []string) *exec.Cmd {
	return exec.CommandContext(ctx, "/bin/sh", "-c", s.run())
}
func (scripted) Lost(code int) bool { return code == 255 }
func (scripted) Unreached(code int, stderr string) bool {
	return (remote.SSH{}).Unreached(code, stderr)
}
func (scripted) Where() string { return "host-a" }

// A copy whose ssh could not connect, which rsync reports with its own lines after ssh's, is made
// again, and one that failed otherwise is made once.
func TestARsyncCopyWhoseSSHCouldNotConnectIsMadeAgain(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	for _, c := range []struct {
		name  string
		fail  string
		calls int
		ok    bool
	}{
		{"could not connect", `echo 'kex_exchange_identification: Connection closed by remote host' >&2
echo 'rsync: connection unexpectedly closed (0 bytes received so far) [Receiver]' >&2
echo 'rsync error: unexplained error (code 255) at io.c(232) [Receiver=3.2.7]' >&2
exit 255`, 3, true},
		{"failed otherwise", `echo 'rsync: change_dir "/nowhere" failed: No such file or directory (2)' >&2; exit 23`, 1, false},
		{"lost in the session", `echo 'Connection to host-a closed by remote host.' >&2; exit 255`, 1, false},
	} {
		dir := t.TempDir()
		count := filepath.Join(dir, "calls")
		script := "#!/bin/sh\necho x >> " + count + "\nif [ $(wc -l < " + count + ") -le 2 ]; then\n" + c.fail + "\nfi\nexit 0\n"
		prog := filepath.Join(dir, "rsync")
		if err := os.WriteFile(prog, []byte(script), 0o755); err != nil {
			t.Fatal(err)
		}
		r := remote.Rsync{SSH: remote.SSH{Host: "host-a"}, Program: []string{prog}, Timers: clock.Immediate{Longest: remote.ReconnectMaxWait}}
		err := r.Pull(ctx, "/remote", filepath.Join(dir, "local"), true)
		data, _ := os.ReadFile(count)
		if calls := strings.Count(string(data), "x"); calls != c.calls || (err == nil) != c.ok {
			t.Errorf("%s: %v after %d copies, want %d", c.name, err, calls, c.calls)
		}
	}
}

// Every ssh call gives up on a connection that is not set up within a bound, so a login node that
// leaves it waiting is a call that did not reach it, made again, and not one that hangs.
func TestSSHGivesUpOnAConnectionNotSetUp(t *testing.T) {
	args := strings.Join(remote.SSH{Host: "host-a", Program: []string{"ssh"}}.Command(context.Background(), []string{"true"}).Args, " ")
	if !strings.Contains(args, "-o ConnectTimeout=") {
		t.Errorf("args %q", args)
	}
}

// A wait that the caller's context ends returns the last failure at once.
func TestReconnectStopsWhenTheCallerDoes(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	calls := 0
	_, err := remote.Reconnect(ctx, clock.NewManualTimers(), func() (int, bool, error) {
		calls++
		return 0, true, errors.New("not reached")
	})
	if err == nil || calls != 1 {
		t.Errorf("%v after %d calls, want the failure after one", err, calls)
	}
}
