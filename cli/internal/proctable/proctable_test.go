package proctable_test

import (
	"bufio"
	"bytes"
	"context"
	"encoding/binary"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"testing/quick"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/proctable"
)

// hangBound only guards against a process the kernel will not end. A passing run never gets near it.
const hangBound = time.Minute

// probeVar is the variable the probes carry. No test of this package runs with it set.
const probeVar = "VIBESYS_PROCTABLE_PROBE"

// helperVar makes the test binary a helper process instead of running tests, as the standard
// library's own tests of os/exec do.
const helperVar = "VIBESYS_PROCTABLE_HELPER"

// TestHelperProcess is the program the tests start, not a test. As a helper it waits for its input
// to close, and as "parent" first runs cat, a program the system protects on macOS, and prints cat's
// pid. Run as a test it does nothing. The test binary is the helper because a system program such as
// cat shows no environment on macOS, and the processes a test machine starts are the tool itself.
func TestHelperProcess(t *testing.T) {
	role := os.Getenv(helperVar)
	if role == "" {
		return
	}
	if role == "parent" {
		child := exec.Command("cat")
		stdin, err := child.StdinPipe()
		if err != nil {
			os.Exit(2)
		}
		if err := child.Start(); err != nil {
			os.Exit(2)
		}
		defer func() { _ = stdin.Close(); _ = child.Wait() }()
		_, _ = os.Stdout.WriteString(strconv.Itoa(child.Process.Pid) + "\n")
	}
	_, _ = io.Copy(io.Discard, os.Stdin)
}

// encodeProcArgs2 writes the layout of macOS's sysctl kern.procargs2: the argument count, the
// executable's path and padding, the arguments, the environment, an empty string, and the strings
// the kernel adds after the environment.
func encodeProcArgs2(execPath string, padding int, args, environ, apple []string, trailing int) []byte {
	var b bytes.Buffer
	_ = binary.Write(&b, binary.NativeEndian, int32(len(args)))
	b.WriteString(execPath)
	b.Write(make([]byte, 1+padding))
	for _, s := range args {
		b.WriteString(s)
		b.WriteByte(0)
	}
	for _, s := range environ {
		b.WriteString(s)
		b.WriteByte(0)
	}
	b.WriteByte(0)
	for _, s := range apple {
		b.WriteString(s)
		b.WriteByte(0)
	}
	b.Write(make([]byte, trailing))
	return b.Bytes()
}

// text makes a generated string one the layout can hold: no NUL, and not empty.
func text(s string) string {
	s = strings.ReplaceAll(s, "\x00", "")
	if s == "" {
		return "x"
	}
	return s
}

func texts(in []string) []string {
	out := make([]string, len(in))
	for i, s := range in {
		out[i] = text(s)
	}
	return out
}

// Whatever the executable, the padding, the arguments and the environment, and whatever follows the
// environment, the arguments and the environment come back as written.
func TestProcArgs2ReadsBackWhatTheKernelLayoutHolds(t *testing.T) {
	err := quick.Check(func(execPath string, padding uint8, args, environ, apple []string, laterEmpty []bool, trailing uint8) bool {
		args, environ, apple = texts(args), texts(environ), texts(apple)
		// An empty argument after the first is legal. An empty first one cannot be told from padding.
		for i := 1; i < len(args) && i <= len(laterEmpty); i++ {
			if laterEmpty[i-1] {
				args[i] = ""
			}
		}
		raw := encodeProcArgs2(text(execPath), int(padding%16), args, environ, apple, int(trailing%16))
		gotArgs, gotEnviron, err := proctable.ParseProcArgs2(raw)
		return err == nil && slices.Equal(gotArgs, args) && slices.Equal(gotEnviron, environ)
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
}

// A program the system protects has its arguments and no environment, which is what the macOS kernel
// returns for sh and cat: the layout ends where the arguments do.
func TestProcArgs2OfAProtectedProgramHasNoEnvironment(t *testing.T) {
	err := quick.Check(func(execPath string, padding uint8, args []string) bool {
		args = texts(args)
		var b bytes.Buffer
		_ = binary.Write(&b, binary.NativeEndian, int32(len(args)))
		b.WriteString(text(execPath))
		b.Write(make([]byte, 1+int(padding%16)))
		for _, s := range args {
			b.WriteString(s)
			b.WriteByte(0)
		}
		gotArgs, gotEnviron, err := proctable.ParseProcArgs2(b.Bytes())
		return err == nil && slices.Equal(gotArgs, args) && len(gotEnviron) == 0
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
}

// A layout cut short anywhere before the end of its arguments is an error, never a shorter list of
// arguments: a reader that took what it could read for all of it would find fewer processes than
// there are.
func TestProcArgs2CutShortInTheArgumentsIsAnError(t *testing.T) {
	err := quick.Check(func(execPath string, args, environ []string, cut uint16) bool {
		args = texts(args)
		if len(args) == 0 {
			args = []string{"a"}
		}
		raw := encodeProcArgs2(text(execPath), 0, args, texts(environ), nil, 0)
		end := 4 + len(text(execPath)) + 1
		for _, s := range args {
			end += len(s) + 1
		}
		_, _, err := proctable.ParseProcArgs2(raw[:int(cut)%end])
		return err != nil
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
}

func TestProcArgs2RefusesWhatIsNotItsLayout(t *testing.T) {
	negative := binary.NativeEndian.AppendUint32(nil, uint32(1<<32-1))
	for name, raw := range map[string][]byte{
		"nothing":            nil,
		"a short count":      {1, 0},
		"a negative count":   append(negative, "/bin/x\x00x\x00"...),
		"no executable path": binary.NativeEndian.AppendUint32(nil, 1),
	} {
		if args, environ, err := proctable.ParseProcArgs2(raw); err == nil {
			t.Errorf("%s: read arguments %q and environment %q", name, args, environ)
		}
	}
}

func FuzzProcArgs2(f *testing.F) {
	f.Add(encodeProcArgs2("/bin/x", 3, []string{"x", "-y"}, []string{"A=b", "C="}, []string{"executable_path=/bin/x"}, 5))
	f.Add([]byte{})
	f.Add([]byte{255, 255, 255, 255, 0})
	f.Fuzz(func(t *testing.T, raw []byte) {
		args, _, err := proctable.ParseProcArgs2(raw)
		if err == nil && len(args) > len(raw) {
			t.Fatalf("read %d arguments from %d bytes", len(args), len(raw))
		}
	})
}

// helper runs the helper process with the variables added to this test's environment, and ends it
// when the test does. Its input is the pipe the test closes to let it finish.
func helper(t *testing.T, role string, env ...string) (*exec.Cmd, *bufio.Reader) {
	t.Helper()
	cmd := exec.Command(os.Args[0], "-test.run=^TestHelperProcess$", "-test.count=1")
	cmd.Env = append(append(os.Environ(), helperVar+"="+role), env...)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = stdin.Close()
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	})
	return cmd, bufio.NewReader(stdout)
}

// untilNotRunning returns once the process has ended, which the kernel does after the signal that
// ends it is sent, not as it is sent. The bound only guards against a process that never ends.
func untilNotRunning(t *testing.T, pid int) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), hangBound)
	defer cancel()
	for {
		running, err := proctable.Running(pid)
		if err != nil {
			t.Fatal(err)
		}
		if !running {
			return
		}
		if !clock.Sleep(ctx, clock.System{}, 5*time.Millisecond) {
			t.Fatalf("process %d is still running", pid)
		}
	}
}

// The processes of a directory are the ones whose variable is the directory or a path inside it:
// not one that is set to something else, to a sibling whose name starts with the directory's, to the
// directory's parent, to a path that leaves it, or not set. This is what keeps a test's cleanup to
// the test's own processes when other tests run beside it in sibling directories.
func TestRealProcessesWithTheVariableUnderADirectoryAreFound(t *testing.T) {
	root := t.TempDir()
	var want []int
	for _, value := range []string{root, root + "/rt", root + "/rt/vibesys-cli/deeper", root + "/./rt//x/"} {
		cmd, _ := helper(t, "probe", probeVar+"="+value)
		want = append(want, cmd.Process.Pid)
	}
	for _, value := range []string{root + "2", root + "-sibling/rt", filepath.Dir(root), root + "/../elsewhere", ""} {
		helper(t, "probe", probeVar+"="+value)
	}
	helper(t, "probe")
	slices.Sort(want)

	got, err := proctable.WithEnvUnder(probeVar, root)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(got, want) {
		t.Fatalf("WithEnvUnder(%q, %q) = %v, want %v", probeVar, root, got, want)
	}
}

// A program the system hides the environment of is found as the child of a process that carries the
// variable. On macOS cat is such a program, and on Linux it carries the variable itself, so the
// child is listed on either, and the process that started it too.
func TestRealAChildOfAProcessOfTheDirectoryIsFound(t *testing.T) {
	root := t.TempDir()
	parent, stdout := helper(t, "parent", probeVar+"="+root)
	line, err := stdout.ReadString('\n')
	if err != nil {
		t.Fatalf("the parent did not say its child's pid: %v", err)
	}
	child, err := strconv.Atoi(strings.TrimSpace(line))
	if err != nil {
		t.Fatalf("the parent said %q: %v", line, err)
	}
	want := []int{parent.Process.Pid, child}
	slices.Sort(want)

	got, err := proctable.WithEnvUnder(probeVar, root)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(got, want) {
		t.Fatalf("WithEnvUnder = %v, want the parent and its child %v", got, want)
	}
}

// A process that has ended is not listed or running, whether it waits to be collected or has been,
// and a stopped one is both: stopping a process does not end it.
func TestRealAProcessIsRunningUntilItEndsAndAZombieHasEnded(t *testing.T) {
	root := t.TempDir()
	cmd, _ := helper(t, "probe", probeVar+"="+root)
	pid := cmd.Process.Pid
	assertRunning := func(when string, want bool) {
		t.Helper()
		got, err := proctable.Running(pid)
		if err != nil || got != want {
			t.Fatalf("Running(%d) %s = %v, %v; want %v", pid, when, got, err, want)
		}
	}
	assertListed := func(when string, want bool) {
		t.Helper()
		pids, err := proctable.WithEnvUnder(probeVar, root)
		if err != nil || slices.Contains(pids, pid) != want {
			t.Fatalf("WithEnvUnder %s = %v, %v; want %d listed: %v", when, pids, err, pid, want)
		}
	}
	assertRunning("when it started", true)
	assertListed("when it started", true)

	if err := cmd.Process.Signal(syscall.SIGSTOP); err != nil {
		t.Fatal(err)
	}
	assertRunning("while stopped", true)
	assertListed("while stopped", true)
	if err := cmd.Process.Signal(syscall.SIGCONT); err != nil {
		t.Fatal(err)
	}

	// Killed and not yet collected, it is a zombie.
	if err := cmd.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	untilNotRunning(t, pid)
	assertListed("as a zombie", false)

	if err := cmd.Wait(); err == nil {
		t.Fatal("a killed process waited for without an error")
	}
	assertRunning("once collected", false)
	assertListed("once collected", false)
}

func TestRealTheCallersOwnProcessIsRunningAndNeverListed(t *testing.T) {
	if got, err := proctable.Running(os.Getpid()); err != nil || !got {
		t.Fatalf("Running(own pid) = %v, %v", got, err)
	}
	// PATH is set in nearly every process, so this lists nearly everything the user runs, which
	// reads the whole table, and the caller would be among them if it were not left out.
	pids, err := proctable.WithEnvUnder("PATH", "/")
	if err != nil {
		t.Fatal(err)
	}
	if slices.Contains(pids, os.Getpid()) {
		t.Fatalf("WithEnvUnder listed the caller %d among %v", os.Getpid(), pids)
	}
}

// What cannot be answered is an error, never an empty answer.
func TestACallThatAsksForNothingSensibleIsAnError(t *testing.T) {
	for _, pid := range []int{0, -1} {
		if got, err := proctable.Running(pid); err == nil || got {
			t.Errorf("Running(%d) = %v, %v", pid, got, err)
		}
	}
	for _, c := range []struct{ name, dir string }{{"", "/a"}, {"A=B", "/a"}, {"A\x00B", "/a"}, {probeVar, ""}} {
		if pids, err := proctable.WithEnvUnder(c.name, c.dir); err == nil || len(pids) != 0 {
			t.Errorf("WithEnvUnder(%q, %q) = %v, %v", c.name, c.dir, pids, err)
		}
	}
}
