package connect

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"testing/quick"
	"time"

	"golang.org/x/sys/unix"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

func shortRoot(t *testing.T) string {
	t.Helper()
	root, err := os.MkdirTemp("/tmp", "gw-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(root) })
	return root
}

// helperEnv says a test binary is the candidate host of TestDetachedStartup, and where it listens.
const helperEnv = "VIBESYS_CONNECT_TEST_SOCKET"

// This is the one real-process wiring test for a start: the candidate is detached, runs in its root
// with exactly the environment it was given, and reports on the descriptor it was told about.
func TestDetachedStartup(t *testing.T) {
	root := shortRoot(t)
	socket := filepath.Join(root, "host.sock")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	binary, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	process := Process{Binary: binary, Args: []string{"-test.run=^TestDetachedHelper$"}, Env: []string{helperEnv + "=" + socket, "ONLY=this"}, Dir: root, Log: filepath.Join(root, "host.log")}
	connection, err := Dial(ctx, socket, process, func() (bool, error) { return false, nil })
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	connection.SetDeadline(time.Now().Add(10 * time.Second))
	var pid, group, session int
	var dir, only string
	if _, err := fmt.Fscanf(connection, "%d %d %d %s %s\n", &pid, &group, &session, &dir, &only); err != nil {
		t.Fatal(err)
	}
	if pid != group || pid != session {
		t.Fatalf("child %d has process group %d and session %d; serve was not detached", pid, group, session)
	}
	if real, _ := filepath.EvalSymlinks(root); dir != real || only != "this" {
		t.Fatalf("child ran in %q with ONLY=%q; want the root %q and the given environment", dir, only, real)
	}
	if _, err := connection.Write([]byte{1}); err != nil {
		t.Fatal(err)
	}
	if _, err := io.ReadAll(connection); err != nil {
		t.Fatal(err)
	}
}

// The candidate of TestDetachedStartup: it serves one connection and reports on ReportFD.
func TestDetachedHelper(t *testing.T) {
	socket := os.Getenv(helperEnv)
	if socket == "" {
		return
	}
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	listener.(*net.UnixListener).SetDeadline(time.Now().Add(10 * time.Second))
	data, err := proto.Marshal(&pb.HostStartReport{Outcome: &pb.HostStartReport_Serving{Serving: &pb.Serving{}}})
	if err != nil {
		t.Fatal(err)
	}
	report := os.NewFile(ReportFD, "report")
	if _, err := report.Write(data); err != nil {
		t.Fatal(err)
	}
	report.Close()
	connection, err := listener.Accept()
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	connection.SetDeadline(time.Now().Add(10 * time.Second))
	pid := unix.Getpid()
	group, err := unix.Getpgid(pid)
	if err != nil {
		t.Fatal(err)
	}
	session, err := unix.Getsid(pid)
	if err != nil {
		t.Fatal(err)
	}
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := fmt.Fprintf(connection, "%d %d %d %s %s\n", pid, group, session, dir, os.Getenv("ONLY")); err != nil {
		t.Fatal(err)
	}
	var acknowledgement [1]byte
	if _, err := connection.Read(acknowledgement[:]); err != nil {
		t.Fatal(err)
	}
}

func TestAMissingExecutableFailsTheStart(t *testing.T) {
	root := shortRoot(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	process := Process{Binary: "/nonexistent/vibesys-host", Dir: root, Log: filepath.Join(root, "host.log")}
	if connection, err := Dial(ctx, filepath.Join(root, "host.sock"), process, func() (bool, error) { return false, nil }); err == nil {
		connection.Close()
		t.Fatal("missing executable accepted")
	}
}

// A candidate that ends without reporting fails with what this attempt wrote and nothing older,
// however much an earlier host left in the log, and the log keeps it all for a person to read.
func TestAFailedStartShowsOnlyWhatThatAttemptWrote(t *testing.T) {
	root := shortRoot(t)
	log := filepath.Join(root, "host.log")
	const old = "2026-11-03 a line of an older host\n"
	if err := os.WriteFile(log, []byte(old), 0o600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	process := Process{Binary: "/bin/sh", Args: []string{"-c", "echo this attempt >&2; exit 7"}, Dir: root, Log: log}
	_, err := process.Launch(ctx)
	var ended *EndedError
	if !errors.As(err, &ended) || ended.Stderr != "this attempt" || !strings.Contains(ended.Status, "7") {
		t.Fatalf("got %v, want only this attempt's words and its status", err)
	}
	kept, _ := os.ReadFile(log)
	if !strings.Contains(string(kept), old) || !strings.Contains(string(kept), "this attempt") {
		t.Fatalf("the log holds %q", kept)
	}
	// With nothing written by this attempt there is nothing to show, whatever is in the file.
	process.Args = []string{"-c", "exit 1"}
	_, err = process.Launch(ctx)
	if !errors.As(err, &ended) || ended.Stderr != "" || strings.Contains(err.Error(), "older host") {
		t.Fatalf("got %v, want no words at all", err)
	}
}

// A start that outlasts its bound ends the candidate: nothing the client started and then gave up on
// is left behind. A log past its limit is set aside first, not shown.
func TestAStartThatTimesOutLeavesNoProcess(t *testing.T) {
	root := shortRoot(t)
	log := filepath.Join(root, "host.log")
	if err := os.WriteFile(log, make([]byte, logRotation), 0o600); err != nil {
		t.Fatal(err)
	}
	pidFile := filepath.Join(root, "pid")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	process := Process{Binary: "/bin/sh", Args: []string{"-c", "echo $$ > " + pidFile + ".tmp; mv " + pidFile + ".tmp " + pidFile + "; exec sleep 600"}, Dir: root, Log: log}
	failed := make(chan error, 1)
	go func() {
		_, err := process.Launch(ctx)
		failed <- err
	}()
	guard := time.Now().Add(30 * time.Second)
	var pid int
	for {
		data, err := os.ReadFile(pidFile)
		if err == nil {
			pid, _ = strconv.Atoi(strings.TrimSpace(string(data)))
			break
		}
		if time.Now().After(guard) {
			t.Fatal("the candidate never ran")
		}
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	if err := <-failed; !errors.Is(err, context.Canceled) {
		t.Fatalf("got %v, want the context's error", err)
	}
	if err := syscall.Kill(pid, 0); !errors.Is(err, syscall.ESRCH) {
		t.Fatalf("candidate %d is still there: %v", pid, err)
	}
	if info, err := os.Stat(log + ".old"); err != nil || info.Size() != logRotation {
		t.Fatalf("the long log was not set aside: %v, %v", info, err)
	}
}

// fakeLauncher is the host process in memory. Each Launch plays the next step of a script: a step is
// what a candidate does, against a socket and a lock the test owns.
type fakeLauncher struct {
	steps    []func() (*pb.HostStartReport, error)
	launches int
}

func (f *fakeLauncher) Launch(context.Context) (*pb.HostStartReport, error) {
	if f.launches >= len(f.steps) {
		return nil, errors.New("the script has no more starts")
	}
	f.launches++
	return f.steps[f.launches-1]()
}

func serving() *pb.HostStartReport {
	return &pb.HostStartReport{Outcome: &pb.HostStartReport_Serving{Serving: &pb.Serving{}}}
}

func alreadyServed() *pb.HostStartReport {
	return &pb.HostStartReport{Outcome: &pb.HostStartReport_AlreadyServed{AlreadyServed: &pb.AlreadyServed{}}}
}

func refused(detail string) *pb.HostStartReport {
	return &pb.HostStartReport{Outcome: &pb.HostStartReport_Refused{Refused: &pb.Unserved{Reason: pb.UnservedReason_UNSERVED_REASON_STATE_REFUSED, Detail: detail}}}
}

// listenOn makes a host appear on socket.
func listenOn(t *testing.T, socket string) {
	t.Helper()
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
}

// Whatever the candidates report, in any order, a Dial ends in exactly one way: connected to a host
// that serves, or the refusal of the candidate that refused, after which it starts nothing more. It
// never starts more than one candidate per time the lock came free.
func TestDialEndsInOneWayForEveryReportSequence(t *testing.T) {
	if err := quick.Check(func(script []uint8) bool {
		socket := filepath.Join(shortRoot(t), "host.sock")
		if len(script) > 8 {
			script = script[:8]
		}
		fake := &fakeLauncher{}
		expectRefused, expectLaunches := false, 0
		for _, s := range script {
			expectLaunches++
			if s%3 == 0 {
				fake.steps = append(fake.steps, func() (*pb.HostStartReport, error) { return refused("nope"), nil })
				expectRefused = true
				break
			}
			if s%3 == 1 {
				fake.steps = append(fake.steps, func() (*pb.HostStartReport, error) {
					listenOn(t, socket)
					return serving(), nil
				})
				break
			}
			// A loser of the lock, whose winner went away with nobody serving.
			fake.steps = append(fake.steps, func() (*pb.HostStartReport, error) { return alreadyServed(), nil })
		}
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		connection, err := Dial(ctx, socket, fake, func() (bool, error) { return false, nil })
		if connection != nil {
			connection.Close()
		}
		var said *RefusedError
		switch {
		case fake.launches != expectLaunches && len(fake.steps) == expectLaunches:
			t.Logf("script %v: %d launches, want %d", script, fake.launches, expectLaunches)
			return false
		case expectRefused:
			if !errors.As(err, &said) || said.Why.GetDetail() != "nope" || connection != nil {
				t.Logf("script %v: %v", script, err)
				return false
			}
		case fake.launches == len(fake.steps) && err != nil && !strings.Contains(err.Error(), "no more starts"):
			t.Logf("script %v: %v", script, err)
			return false
		}
		return true
	}, nil); err != nil {
		t.Fatal(err)
	}
}

// A loser of the lock waits for the winner and starts nothing more, however long the winner takes.
func TestALoserOfTheLockWaitsForTheWinner(t *testing.T) {
	socket := filepath.Join(shortRoot(t), "host.sock")
	fake := &fakeLauncher{steps: []func() (*pb.HostStartReport, error){func() (*pb.HostStartReport, error) { return alreadyServed(), nil }}}
	looks := 0
	holding := func() (bool, error) {
		looks++
		if looks == 3 {
			listenOn(t, socket)
		}
		return true, nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	connection, err := Dial(ctx, socket, fake, holding)
	if err != nil {
		t.Fatal(err)
	}
	connection.Close()
	if fake.launches != 1 {
		t.Fatalf("%d candidates started for one winner", fake.launches)
	}
}

// A host that already serves is used, and nothing is started.
func TestAServingHostStartsNothing(t *testing.T) {
	socket := filepath.Join(shortRoot(t), "host.sock")
	listenOn(t, socket)
	fake := &fakeLauncher{}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	connection, err := Dial(ctx, socket, fake, func() (bool, error) { return false, nil })
	if err != nil || fake.launches != 0 {
		t.Fatalf("%v, %d candidates", err, fake.launches)
	}
	connection.Close()
}
