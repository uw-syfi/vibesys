package remote_test

import (
	"context"
	"errors"
	"io/fs"
	"math/rand/v2"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
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
// ReconnectWait, up to ReconnectAttempts in all; one that reaches it, or fails otherwise, is made
// no more.
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
		} else if got.err == nil || !strings.Contains(got.err.Error(), "not reached in 5 attempts") || calls != remote.ReconnectAttempts {
			t.Errorf("%d unreached: %+v after %d calls, want the last failure after %d", unreached, got, calls, remote.ReconnectAttempts)
		}
		for i, d := range waits {
			if want := remote.ReconnectWait << i; d != want {
				t.Errorf("%d unreached: wait %d is %v, want %v", unreached, i+1, d, want)
			}
		}
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
