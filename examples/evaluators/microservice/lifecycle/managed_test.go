package lifecycle

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"strconv"
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
		if err := os.WriteFile(pidFile, []byte(strconv.Itoa(os.Getpid())), 0o600); err != nil {
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
	time.Sleep(time.Minute)
	os.Exit(0)
}

func TestManagedCandidateKillsDetachedDescendant(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("detached descendant process-state assertion is Linux-specific")
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
		raw, readErr := os.ReadFile(pidFile)
		if readErr == nil {
			childPID, err = strconv.Atoi(string(raw))
			if err == nil {
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
	for processExists(childPID) && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if processExists(childPID) {
		_ = syscall.Kill(childPID, syscall.SIGKILL)
		t.Fatal(fmt.Sprintf("detached child %d survived", childPID))
	}
}
