package clustercli_test

import (
	"bytes"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clustercli"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
)

// poolJob is a job id of this test's own, whose directory of the pool on this machine, the fake
// node's, is removed when the test ends.
func poolJob(t *testing.T) string {
	t.Helper()
	job := strconv.Itoa(800000 + os.Getpid()%100000)
	t.Cleanup(func() { _ = os.RemoveAll("/tmp/vibesys-pool-" + job) })
	return job
}

// until waits, bounded only against a hang, for the condition.
func until(t *testing.T, what string, cond func() bool) {
	t.Helper()
	for !cond() {
		if !clock.Sleep(ctx(t), clock.System{}, 10*time.Millisecond) {
			t.Fatalf("never: %s", what)
		}
	}
}

// A claim of a reusable allocation is its alone: another fails with ErrTaken while it lives, and
// once it is released, which kills every process of the allocation's containers first, the
// allocation can be claimed again. An allocation that is ending is claimed by none.
func TestAClaimIsTheAllocationsAlone(t *testing.T) {
	sh := newShell(t)
	nodeRoot := t.TempDir()
	if err := os.WriteFile(filepath.Join(sh.bin, "node_root"), []byte(nodeRoot), 0o644); err != nil {
		t.Fatal(err)
	}
	c, err := clustercli.New(clustercli.Config{Shell: sh, Timers: envtest.NewTimers(), Submit: []string{"sbatch"}, Runtime: apptainer.RuntimePyxis})
	if err != nil {
		t.Fatal(err)
	}
	job := poolJob(t)
	n := c.Node(job)
	first, err := n.Claim(ctx(t))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := n.Claim(ctx(t)); !errors.Is(err, apptainer.ErrTaken) {
		t.Fatalf("a second claim: %v, want taken", err)
	}
	up := &announced{up: make(chan struct{})}
	p, err := n.Start(apptainer.Box{Name: "b", Image: "/shared/x.sqsh"}, []string{"/bin/sh", "-c", "echo up; exec sleep 600"}, up)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(p.Kill)
	select {
	case <-up.up:
	case <-ctx(t).Done():
		t.Fatal("the box's process never started")
	}
	if err := first.Close(); err != nil {
		t.Fatalf("release: %v", err)
	}
	if e := wait(t, p); e.Err == nil && e.Code == 0 {
		t.Errorf("the box's process outlived the release: %+v", e)
	}
	again, err := n.Claim(ctx(t))
	if err != nil {
		t.Fatalf("a claim after the release: %v", err)
	}
	if err := again.Close(); err != nil || again.Close() != nil {
		t.Errorf("a second release: %v", err)
	}
	if err := os.WriteFile("/tmp/vibesys-pool-"+job+"/ended", nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := n.Claim(ctx(t)); !errors.Is(err, apptainer.ErrTaken) {
		t.Errorf("a claim of an ending allocation: %v, want taken", err)
	}
}

// fakeSleep counts a poll, then pauses briefly; fakeDate says how many polls there were as the
// seconds since the epoch, so the reaper's idle time is counted in polls, whatever time passes.
const (
	fakeSleep = "#!/bin/sh\necho . >> \"$(dirname \"$0\")/ticks\"\nexec /bin/sleep 0.01\n"
	fakeDate  = "#!/bin/sh\nwc -l < \"$(dirname \"$0\")/ticks\" | tr -d ' '\n"
)

// A reusable allocation's batch script keeps it while its claim's process lives, however long;
// clears what a dead claim left; and ends once no claim has lived for its idle time, marking it
// ending first so that nothing claims it.
func TestAReusableAllocationEndsOnceIdle(t *testing.T) {
	bin := t.TempDir()
	for name, text := range map[string]string{"sleep": fakeSleep, "date": fakeDate, "ticks": ""} {
		if err := os.WriteFile(filepath.Join(bin, name), []byte(text), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	job := poolJob(t)
	dir := "/tmp/vibesys-pool-" + job
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	holder := exec.Command("/bin/sleep", "600")
	box := exec.Command("/bin/sleep", "600")
	box.Env = []string{apptainer.EnvBox + "=b", "SLURM_JOB_ID=" + job}
	for _, p := range []*exec.Cmd{holder, box} {
		if err := p.Start(); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = p.Process.Kill() })
	}
	boxEnded := make(chan struct{})
	go func() { _ = box.Wait(); close(boxEnded) }()
	if err := os.WriteFile(filepath.Join(dir, "claim"), []byte(strconv.Itoa(holder.Process.Pid)), 0o644); err != nil {
		t.Fatal(err)
	}
	const idleTicks = 5
	reaper := exec.CommandContext(ctx(t), "/bin/sh", "-c", clustercli.ReaperScript(idleTicks*time.Second, 10*time.Millisecond))
	reaper.Env = append(os.Environ(), "PATH="+bin+":"+os.Getenv("PATH"), "SLURM_JOB_ID="+job)
	var out bytes.Buffer
	reaper.Stdout, reaper.Stderr = &out, &out
	if err := reaper.Start(); err != nil {
		t.Fatal(err)
	}
	ended := make(chan error, 1)
	go func() { ended <- reaper.Wait() }()
	ticks := func() int {
		data, _ := os.ReadFile(filepath.Join(bin, "ticks"))
		return strings.Count(string(data), "\n")
	}
	until(t, "the reaper looked at a live claim many times", func() bool { return ticks() >= 4*idleTicks })
	select {
	case err := <-ended:
		t.Fatalf("the reaper ended a claimed allocation: %v %s", err, out.String())
	case <-boxEnded:
		t.Fatal("the reaper killed a live claim's container")
	default:
	}
	_ = holder.Process.Kill()
	_ = holder.Wait()
	select {
	case err := <-ended:
		if err != nil {
			t.Fatalf("the reaper: %v %s", err, out.String())
		}
	case <-ctx(t).Done():
		t.Fatal("the reaper never ended an idle allocation")
	}
	select {
	case <-boxEnded:
	default:
		t.Error("what the dead claim left still runs")
	}
	if _, err := os.Stat(filepath.Join(dir, "ended")); err != nil {
		t.Errorf("the allocation was not marked ending: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "claim")); err == nil {
		t.Error("the dead claim is still recorded")
	}
}

// A reusable allocation's batch script is the reaper, with its idle time; any other sleeps.
func TestAReusableAllocationIsSubmittedWithItsReaper(t *testing.T) {
	a := apptainer.Allocation{Name: "n", Comment: "c", Limit: time.Hour, Idle: 90 * time.Second}
	args := clustercli.SubmitArgs(a)
	if wrap := args[len(args)-1]; !strings.HasPrefix(wrap, "--wrap=") || !strings.Contains(wrap, "idle=90\n") {
		t.Errorf("wrap %q", wrap)
	}
	a.Idle = 0
	if args := clustercli.SubmitArgs(a); args[len(args)-1] != "--wrap=exec sleep infinity" {
		t.Errorf("wrap %q", args[len(args)-1])
	}
}
