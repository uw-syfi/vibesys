package lifecycle

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestManagedCandidateHelper(t *testing.T) {
	if os.Getenv("GO_MANAGED_CANDIDATE_HELPER") != "1" {
		return
	}
	separator := 0
	for index, argument := range os.Args {
		if argument == "--" {
			separator = index
			break
		}
	}
	if separator == 0 || len(os.Args) < separator+3 {
		os.Exit(2)
	}
	mode := os.Args[separator+1]
	pidFile := os.Args[separator+2]
	if mode == "child" {
		if err := os.WriteFile(pidFile, []byte("ready"), 0o600); err != nil {
			os.Exit(3)
		}
		time.Sleep(time.Minute)
		os.Exit(0)
	}
	if mode != "launcher" {
		os.Exit(4)
	}
	child := exec.Command(
		os.Args[0], "-test.run=TestManagedCandidateHelper", "--", "child", pidFile,
	)
	child.Env = os.Environ()
	child.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := child.Start(); err != nil {
		os.Exit(5)
	}
	os.Exit(0)
}

func TestManagedCandidateKillsImmediatelyDetachedDescendant(t *testing.T) {
	requireContainment := os.Getenv("VIBESYS_REQUIRE_MANAGED_CANDIDATE_TESTS") == "1"
	bwrap, err := exec.LookPath("bwrap")
	if err != nil {
		if requireContainment {
			t.Fatalf("bubblewrap is required in this environment: %v", err)
		}
		t.Skip("bubblewrap is unavailable")
	}
	if err := probePIDNamespace(bwrap); err != nil {
		if requireContainment {
			t.Fatalf("PID namespaces are required in this environment: %v", err)
		}
		t.Skipf("PID namespaces are unavailable: %v", err)
	}
	t.Setenv("GO_MANAGED_CANDIDATE_HELPER", "1")
	pidFile := t.TempDir() + "/child.pid"
	managed, err := NewManagedCandidate(
		[]string{
			os.Args[0], "-test.run=TestManagedCandidateHelper", "--", "launcher", pidFile,
		},
		t.TempDir(),
		map[string]string{"VIBESYS_STATE_DIR": t.TempDir()},
	)
	if err != nil {
		t.Fatal(err)
	}
	defer managed.Close(context.Background())
	if err := managed.Prepare(context.Background()); err != nil {
		t.Fatal(err)
	}
	var childPID int
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if _, readErr := os.ReadFile(pidFile); readErr == nil {
			childPID = helperHostPID(pidFile)
			if childPID > 0 {
				break
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	if childPID == 0 {
		t.Fatalf("detached child did not start; log=%s", managed.LogTail(4000))
	}
	if err := managed.Stop(context.Background(), true); err != nil {
		t.Fatalf("stop managed candidate: %v; log=%s", err, managed.LogTail(4000))
	}
	deadline = time.Now().Add(2 * time.Second)
	for pidExists(childPID) && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if pidExists(childPID) {
		_ = syscall.Kill(childPID, syscall.SIGKILL)
		t.Fatalf("immediately detached child %d survived", childPID)
	}
}

func TestManagedCandidateFailsClosedWithoutPIDNamespaceLauncher(t *testing.T) {
	t.Setenv("PATH", t.TempDir())
	if _, err := NewManagedCandidate([]string{os.Args[0]}, t.TempDir(), nil); err == nil ||
		!strings.Contains(err.Error(), "requires bubblewrap") {
		t.Fatalf("containment error=%v", err)
	}
}

func pidExists(pid int) bool {
	err := syscall.Kill(pid, 0)
	return err == nil || !errors.Is(err, syscall.ESRCH)
}

func helperHostPID(pidFile string) int {
	output, err := exec.Command("ps", "-e", "-o", "pid=,args=").Output()
	if err != nil {
		return 0
	}
	for _, line := range strings.Split(string(output), "\n") {
		if !strings.Contains(line, "-- child "+pidFile) {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) == 0 {
			continue
		}
		pid, parseErr := strconv.Atoi(fields[0])
		if parseErr == nil {
			return pid
		}
	}
	return 0
}
