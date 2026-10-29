package hostcommand_test

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
)

func TestProgram(t *testing.T) {
	args := os.Args
	for i, arg := range args {
		if arg != "--host-command-program" {
			continue
		}
		mode := args[i+1]
		switch mode {
		case "echo":
			data, _ := io.ReadAll(os.Stdin)
			fmt.Fprint(os.Stdout, string(data)+os.Getenv("VIBESYS_STATE_HOME"))
		case "fail":
			fmt.Fprint(os.Stderr, "invalid configuration")
			os.Exit(7)
		case "large":
			fmt.Fprint(os.Stdout, strings.Repeat("x", hostcommand.MaxOutput+1))
		case "stderr":
			fmt.Fprint(os.Stderr, strings.Repeat("x", hostcommand.MaxStderr+10))
		case "frame":
			writeTestFrame(os.Stdout, []byte("hi"))
		case "group":
			cmd := exec.Command(os.Args[0], "-test.run=^TestProgram$", "--", "--host-command-program", "pause")
			if err := cmd.Start(); err != nil {
				fmt.Fprintln(os.Stderr, err)
				os.Exit(3)
			}
			writeTestFrame(os.Stdout, []byte(strconv.Itoa(cmd.Process.Pid)))
			waitForTerm()
			_ = cmd.Wait()
		case "pause":
			waitForTerm()
		}
		os.Exit(0)
	}
}

// Every implementation runs the same contract, including failures and limits.
func TestRunnerContract(t *testing.T) {
	binary, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	for _, implementation := range []string{"exec", "fake"} {
		t.Run(implementation, func(t *testing.T) {
			var runner hostcommand.Runner = hostcommand.ExecRunner{}
			if implementation == "fake" {
				runner = hostcommand.FakeRunner{Program: func(ctx context.Context, c hostcommand.Command) (hostcommand.Result, error) {
					if c.Executable == "/missing/vibesys" {
						return hostcommand.Result{ExitCode: -1}, os.ErrNotExist
					}
					switch c.Args[len(c.Args)-1] {
					case "echo":
						return hostcommand.Result{Output: append(append([]byte{}, c.Input...), []byte(c.StateRoot)...)}, nil
					case "fail":
						return hostcommand.Result{ExitCode: 7, Stderr: "invalid configuration"}, errors.New("exit status 7")
					case "large":
						return hostcommand.Result{Output: []byte(strings.Repeat("x", hostcommand.MaxOutput+1))}, nil
					case "stderr":
						return hostcommand.Result{Stderr: strings.Repeat("x", hostcommand.MaxStderr+10)}, nil
					default:
						panic("unknown program")
					}
				}}
			}
			run := func(ctx context.Context, mode string) (hostcommand.Result, error) {
				return runner.Run(ctx, hostcommand.Command{Executable: binary, Args: []string{"-test.run=^TestProgram$", "--", "--host-command-program", mode}, Input: []byte{0, 255, 10}, StateRoot: "/state"})
			}
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			result, err := run(ctx, "echo")
			if err != nil || string(result.Output) != string([]byte{0, 255, 10})+"/state" || result.ExitCode != 0 {
				t.Fatalf("echo: %+v %v", result, err)
			}
			result, err = run(ctx, "fail")
			if err == nil || result.ExitCode != 7 || result.Stderr != "invalid configuration" {
				t.Fatalf("failure: %+v %v", result, err)
			}
			result, err = run(ctx, "large")
			if err == nil || len(result.Output) > hostcommand.MaxOutput {
				t.Fatalf("stdout limit: %d %v", len(result.Output), err)
			}
			result, err = run(ctx, "stderr")
			if err != nil || len(result.Stderr) != hostcommand.MaxStderr {
				t.Fatalf("stderr limit: %d %v", len(result.Stderr), err)
			}
			_, err = runner.Run(ctx, hostcommand.Command{Executable: "/missing/vibesys"})
			if err == nil {
				t.Fatal("missing executable succeeded")
			}
			cancelled, stop := context.WithCancel(ctx)
			stop()
			_, err = run(cancelled, "echo")
			if !errors.Is(err, context.Canceled) {
				t.Fatalf("cancelled: %v", err)
			}
			expired, finish := context.WithDeadline(ctx, time.Time{})
			defer finish()
			_, err = run(expired, "echo")
			if !errors.Is(err, context.DeadlineExceeded) {
				t.Fatalf("deadline: %v", err)
			}
		})
	}
}

func TestStreamDeliversOnePayload(t *testing.T) {
	binary, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	for _, implementation := range []string{"exec", "fake"} {
		t.Run(implementation, func(t *testing.T) {
			var runner hostcommand.Runner = hostcommand.ExecRunner{}
			command := hostcommand.Command{
				Executable: binary,
				Args:       []string{"-test.run=^TestProgram$", "--", "--host-command-program", "frame"},
				StateRoot:  t.TempDir(),
			}
			if implementation == "fake" {
				runner = hostcommand.FakeRunner{Program: func(context.Context, hostcommand.Command) (hostcommand.Result, error) {
					return hostcommand.Result{Output: []byte("hi")}, nil
				}}
			}
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			var got []byte
			err := runner.Stream(ctx, command, func(frame []byte) error {
				got = append(got, frame...)
				return nil
			})
			if err != nil || string(got) != "hi" {
				t.Fatalf("stream: %q %v", got, err)
			}
		})
	}
}

func TestStreamCancelReapsTheProcessGroup(t *testing.T) {
	binary, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	var pid int
	err = hostcommand.ExecRunner{}.Stream(ctx, hostcommand.Command{
		Executable: binary,
		Args:       []string{"-test.run=^TestProgram$", "--", "--host-command-program", "group"},
		StateRoot:  t.TempDir(),
	}, func(frame []byte) error {
		parsed, parseErr := strconv.Atoi(string(frame))
		if parseErr != nil {
			return parseErr
		}
		pid = parsed
		cancel()
		return nil
	})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("cancel: %v", err)
	}
	if pid == 0 {
		t.Fatal("child pid was not reported")
	}
	// Stream waits for its direct child, but the kernel may still be reaping
	// descendants after the process-group signal. Observe their disappearance;
	// the deadline only guards a hang, rather than synchronizing cancellation.
	waitUntilGone(t, pid)
}

// A killed orphan stays a zombie until PID 1 reaps it, and a PID 1 that does
// not reap leaves it so; kill(pid, 0) still succeeds on a zombie.
func TestWaitUntilGoneTreatsAnUnreapedZombieAsDead(t *testing.T) {
	if _, err := os.Stat("/proc/self/stat"); err != nil {
		t.Skip("zombie state is read from /proc")
	}
	binary, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(binary, "-test.run=^TestProgram$", "--", "--host-command-program", "fail")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = cmd.Wait() }()
	waitUntilGone(t, cmd.Process.Pid)
}

func TestZombieState(t *testing.T) {
	for stat, want := range map[string]bool{
		"12 (prog) Z 1 12 12 0 -1 4228": true,
		"12 (prog) X 1 12 12 0 -1 4228": true,
		"12 (prog) S 1 12 12 0 -1 4228": false,
		"12 (prog) R 1 12 12 0 -1 4228": false,
		"12 (a) Z (b) S 1 12 12 0":      false,
		"12 (a b) Z 1 12 12 0":          true,
		"12 (a) Z) S 1 12 12 0":         false,
		"":                              false,
		"12 (prog":                      false,
	} {
		if got := zombieState(stat); got != want {
			t.Errorf("zombieState(%q) = %v, want %v", stat, got, want)
		}
	}
}

// waitUntilGone polls until pid is dead or a zombie. The deadline only guards
// a hang.
func waitUntilGone(t *testing.T, pid int) {
	t.Helper()
	guard := time.NewTimer(10 * time.Second)
	defer guard.Stop()
	poll := time.NewTicker(10 * time.Millisecond)
	defer poll.Stop()
	for {
		gone, err := processGone(pid)
		if err != nil {
			t.Fatalf("check descendant %d: %v", pid, err)
		}
		if gone {
			return
		}
		select {
		case <-poll.C:
		case <-guard.C:
			t.Fatalf("descendant %d is still running after cancellation", pid)
		}
	}
}

func processGone(pid int) (bool, error) {
	err := syscall.Kill(pid, 0)
	if errors.Is(err, syscall.ESRCH) {
		return true, nil
	}
	if err != nil {
		return false, err
	}
	// Without /proc (macOS) launchd reaps promptly, so ESRCH is enough; a read
	// error on Linux means the process exited between the two checks.
	stat, readErr := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	return readErr == nil && zombieState(string(stat)), nil
}

// zombieState reports state Z (or X) in a /proc/<pid>/stat line. The command
// name in parentheses may itself contain spaces and parentheses, so the state
// is the first field after the last closing parenthesis.
func zombieState(stat string) bool {
	end := strings.LastIndexByte(stat, ')')
	if end < 0 {
		return false
	}
	fields := strings.Fields(stat[end+1:])
	return len(fields) > 0 && (fields[0] == "Z" || fields[0] == "X")
}

func writeTestFrame(writer io.Writer, payload []byte) {
	var header [4]byte
	binary.BigEndian.PutUint32(header[:], uint32(len(payload)))
	_, _ = writer.Write(header[:])
	_, _ = writer.Write(payload)
}

func waitForTerm() {
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGTERM)
	<-signals
}
