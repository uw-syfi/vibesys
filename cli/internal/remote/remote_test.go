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
	out, err := remote.Output(ctx, remote.Local{}, []string{"/bin/sh", "-c", "echo hi"}, nil)
	if err != nil || out != "hi\n" {
		t.Fatalf("%q, %v", out, err)
	}
	_, err = remote.Output(ctx, remote.Local{}, []string{"/bin/sh", "-c", "echo broken >&2; exit 3"}, nil)
	if err == nil || !strings.Contains(err.Error(), "exited with 3") || !strings.Contains(err.Error(), "broken") {
		t.Errorf("a failing program: %v", err)
	}
	if !(remote.SSH{}).Lost(255) || (remote.SSH{}).Lost(1) || (remote.Local{}).Lost(255) {
		t.Errorf("only ssh's own 255 is a lost exit")
	}
}

// lossy is this machine, through a transport whose own exit code is 255, as ssh's is.
type lossy struct{ remote.Local }

func (lossy) Lost(code int) bool { return code == 255 }

// A program whose end the transport lost is an unavailable failure that wraps ErrLost; a program
// that failed by itself, whatever its code, does not.
func TestAProgramTheTransportLostWrapsErrLost(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	for _, c := range []struct {
		sh   remote.Shell
		code string
		lost bool
	}{{lossy{}, "255", true}, {lossy{}, "1", false}, {remote.Local{}, "255", false}} {
		_, err := remote.Output(ctx, c.sh, []string{"/bin/sh", "-c", "echo 'Connection reset by peer' >&2; exit " + c.code}, nil)
		var f *core.Failure
		if err == nil || errors.Is(err, remote.ErrLost) != c.lost || !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
			t.Errorf("exit %s through %T: %v, want an unavailable failure, lost %v", c.code, c.sh, err, c.lost)
		}
		if c.lost && !strings.Contains(err.Error(), "cannot reach") {
			t.Errorf("exit %s: %v does not say the machine could not be reached", c.code, err)
		}
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
