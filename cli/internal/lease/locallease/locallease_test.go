//go:build unix

package locallease

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/leasetest"
)

// world is a directory of leases. Each daemon opens its own lock files, as a daemon of its own
// process would, and a crash closes them without the release's clearing of the holder's name.
func world(t leasetest.TB) leasetest.World {
	dir := Dir{Path: filepath.Join(t.TempDir(), "leases")}
	return leasetest.World{
		Daemon: func() lease.Leases { return dir },
		Crash:  func(l lease.Lease) { l.(*Lease).f.Close() },
	}
}

func TestTheLeasesOfThisMachineKeepTheContract(t *testing.T) {
	leasetest.Run(t, leasetest.Contract{New: world})
}

func TestTheDirectoryIsMadeForEveryUser(t *testing.T) {
	dir := Dir{Path: filepath.Join(t.TempDir(), "a", "leases")}
	l, err := dir.Acquire(context.Background(), "machine", core.LeaseHolder{User: "alice"})
	if err != nil {
		t.Fatal(err)
	}
	defer l.Release(context.Background())
	info, err := os.Stat(dir.Path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode()&os.ModeSticky == 0 || info.Mode().Perm() != 0o777 {
		t.Fatalf("the directory of leases has the mode %s, want sticky and writable by everyone", info.Mode())
	}
	file, err := os.Stat(dir.file("machine"))
	if err != nil {
		t.Fatal(err)
	}
	if file.Mode().Perm() != 0o666 {
		t.Fatalf("the lock file has the mode %s, want writable by everyone", file.Mode())
	}
}

func TestALinkIsNeverFollowed(t *testing.T) {
	dir := Dir{Path: filepath.Join(t.TempDir(), "leases")}
	if err := os.MkdirAll(dir.Path, 0o777); err != nil {
		t.Fatal(err)
	}
	victim := filepath.Join(t.TempDir(), "victim")
	if err := os.WriteFile(victim, []byte("keep me"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(victim, dir.file("machine")); err != nil {
		t.Fatal(err)
	}
	if l, err := dir.Acquire(context.Background(), "machine", core.LeaseHolder{User: "mallory"}); err == nil {
		l.Release(context.Background())
		t.Fatalf("a lease was taken through a link")
	}
	if b, _ := os.ReadFile(victim); string(b) != "keep me" {
		t.Fatalf("the file a link pointed at was written: %q", b)
	}
}

func TestABadKeyIsRefused(t *testing.T) {
	dir := Dir{Path: t.TempDir()}
	for _, k := range []lease.Key{"", "../etc", "A", "a/b", ".hidden"} {
		if l, err := dir.Acquire(context.Background(), k, core.LeaseHolder{}); err == nil {
			l.Release(context.Background())
			t.Errorf("the key %q was taken", k)
		}
	}
}

// The test binary is also the other daemon: started again with roleVar set, it takes the lease in
// the directory it is given, says so on stdout, and holds it until its stdin closes.
const (
	roleVar = "VIBESYS_LOCALLEASE_TEST_ROLE"
	dirVar  = "VIBESYS_LOCALLEASE_TEST_DIR"
)

func TestMain(m *testing.M) {
	if os.Getenv(roleVar) == "holder" {
		os.Exit(holdForTest())
	}
	os.Exit(m.Run())
}

func holdForTest() int {
	l, err := Dir{Path: os.Getenv(dirVar)}.Acquire(context.Background(), "machine", core.LeaseHolder{
		User: "other", Host: "host-b", PID: os.Getpid(), Job: "their-measure", Since: time.Unix(1_800_000_000, 0),
	})
	if err != nil {
		fmt.Println("refused:", err)
		return 1
	}
	fmt.Println("held")
	_, _ = bufio.NewReader(os.Stdin).ReadString('\n')
	if err := l.Release(context.Background()); err != nil {
		fmt.Println("release:", err)
		return 1
	}
	return 0
}

type holderProc struct {
	cmd   *exec.Cmd
	stdin interface{ Close() error }
}

// startHolder starts another process that takes the lease, and returns once it holds it.
func startHolder(t *testing.T, dir string) holderProc {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	t.Cleanup(cancel)
	cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^$")
	cmd.Env = append(os.Environ(), roleVar+"=holder", dirVar+"="+dir)
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
	t.Cleanup(func() { _ = cmd.Process.Kill(); _ = cmd.Wait() })
	line, err := bufio.NewReader(stdout).ReadString('\n')
	if strings.TrimSpace(line) != "held" {
		t.Fatalf("the other process did not take the lease: %q %v", line, err)
	}
	return holderProc{cmd: cmd, stdin: stdin}
}

// TestTwoProcessesTakeTurns has two real processes contend for one lease: the second is refused,
// told the first's name and pid, while the first holds it, and takes it once the first gives it
// back or is killed without a word.
func TestTwoProcessesTakeTurns(t *testing.T) {
	for _, end := range []string{"released", "killed"} {
		t.Run(end, func(t *testing.T) {
			dir := Dir{Path: filepath.Join(t.TempDir(), "leases")}
			other := startHolder(t, dir.Path)
			me := core.LeaseHolder{User: "me", Host: "host-a", PID: os.Getpid(), Job: "my-measure"}
			_, err := dir.Acquire(context.Background(), "machine", me)
			var held *lease.HeldError
			if !errors.As(err, &held) {
				t.Fatalf("the lease another process holds was not refused: %v", err)
			}
			if held.Holder.PID != other.cmd.Process.Pid || held.Holder.User != "other" || held.Holder.Host != "host-b" || held.Holder.Job != "their-measure" {
				t.Fatalf("the refusal names %+v, want the other process (pid %d)", held.Holder, other.cmd.Process.Pid)
			}
			switch end {
			case "released":
				other.stdin.Close()
				if err := other.cmd.Wait(); err != nil {
					t.Fatalf("the other process: %v", err)
				}
			case "killed":
				if err := other.cmd.Process.Signal(syscall.SIGKILL); err != nil {
					t.Fatal(err)
				}
				_ = other.cmd.Wait()
			}
			l, err := dir.Acquire(context.Background(), "machine", me)
			if err != nil {
				t.Fatalf("the lease was not given back when the other process was %s: %v", end, err)
			}
			defer l.Release(context.Background())
			// The other process may take it again only once this one gives it back.
			cmd := exec.Command(os.Args[0], "-test.run=^$")
			cmd.Env = append(os.Environ(), roleVar+"=holder", dirVar+"="+dir.Path)
			out, _ := cmd.Output()
			if !strings.HasPrefix(string(out), "refused:") || !strings.Contains(string(out), "pid "+strconv.Itoa(os.Getpid())) {
				t.Fatalf("another process was not refused the lease this one holds: %q", out)
			}
		})
	}
}
