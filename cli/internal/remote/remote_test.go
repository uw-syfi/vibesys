package remote_test

import (
	"context"
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
