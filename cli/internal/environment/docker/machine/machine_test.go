package machine_test

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/machine"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
)

// fakeSSH stands in for ssh: it drops ssh's options and the host and runs the command here, so a
// test reaches "another machine" that is this one, through the same command lines.
const fakeSSH = `#!/bin/sh
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o|-F|-i|-p) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
shift
[ "$1" = "--" ] && shift
exec /bin/sh -c "$*"
`

func remoteMachine(t *testing.T) (*machine.Remote, string) {
	t.Helper()
	if _, err := exec.LookPath("rsync"); err != nil {
		t.Skip("rsync is not installed")
	}
	dir := t.TempDir()
	program := filepath.Join(dir, "ssh")
	if err := os.WriteFile(program, []byte(fakeSSH), 0o755); err != nil {
		t.Fatal(err)
	}
	root := filepath.Join(dir, "remote-root")
	ssh := remote.SSH{Host: "host-a", Program: []string{program}}
	return &machine.Remote{Shell: ssh, Rsync: remote.Rsync{SSH: ssh, Program: []string{"rsync"}}, Root: root, Scope: "d1", Timers: clock.System{}}, dir
}

func write(t *testing.T, file, text string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(file), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(file, []byte(text), 0o644); err != nil {
		t.Fatal(err)
	}
}

func read(t *testing.T, file string) string {
	t.Helper()
	data, err := os.ReadFile(file)
	if err != nil {
		return "<" + err.Error() + ">"
	}
	return string(data)
}

func dirsIn(t *testing.T, root, job string) docker.Dirs {
	d := docker.Dirs{
		Checkout: filepath.Join(root, job, "checkout"), Scratch: filepath.Join(root, job, "scratch"),
		Artifacts: filepath.Join(root, job, "artifacts", "01-check"),
	}
	for _, p := range []string{d.Checkout, d.Scratch, d.Artifacts} {
		if err := os.MkdirAll(p, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	return d
}

// A run's directories are copied there before it and what it wrote is copied back after it; a job's
// later runs find what its earlier ones built, and the next job's first run removes the earlier
// job's copies.
func TestARemoteRunHasCopiesOfItsDirectoriesAndItsOutputComesBack(t *testing.T) {
	m, dir := remoteMachine(t)
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	local := filepath.Join(dir, "local")
	job := dirsIn(t, local, "job1")
	write(t, filepath.Join(job.Checkout, "src.c"), "int main;")
	write(t, filepath.Join(job.Scratch, "stale"), "x")

	staged, err := m.Stage(ctx, job)
	if err != nil {
		t.Fatal(err)
	}
	if read(t, filepath.Join(staged.Checkout, "src.c")) != "int main;" || read(t, filepath.Join(staged.Scratch, "stale")) != "x" {
		t.Fatalf("the staged copies do not hold the caller's files")
	}
	// The command builds in the checkout, writes its result and leaves an artifact.
	write(t, filepath.Join(staged.Checkout, "build", "server"), "binary")
	write(t, filepath.Join(staged.Scratch, "sample-0.out"), "{}")
	write(t, filepath.Join(staged.Artifacts, "server.log"), "ready")
	if err := m.Collect(ctx, job, staged); err != nil {
		t.Fatal(err)
	}
	if read(t, filepath.Join(job.Scratch, "sample-0.out")) != "{}" || read(t, filepath.Join(job.Artifacts, "server.log")) != "ready" {
		t.Errorf("the result or the artifact did not come back")
	}
	if _, err := os.Stat(filepath.Join(job.Checkout, "build")); err == nil {
		t.Errorf("the staged checkout's build came back into the caller's checkout")
	}

	// The job's next run keeps the build, and gets an empty artifacts directory of its own.
	job.Artifacts = filepath.Join(local, "job1", "artifacts", "02-benchmark")
	again, err := m.Stage(ctx, job)
	if err != nil {
		t.Fatal(err)
	}
	if again.Checkout != staged.Checkout || read(t, filepath.Join(again.Checkout, "build", "server")) != "binary" {
		t.Errorf("the job's second run lost the build of its first")
	}
	if entries, _ := os.ReadDir(again.Artifacts); again.Artifacts == staged.Artifacts || len(entries) != 0 {
		t.Errorf("the second run's artifacts directory %s is not a new empty one", again.Artifacts)
	}

	// The next job removes the first one's copies.
	next := dirsIn(t, local, "job2")
	if _, err := m.Stage(ctx, next); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(staged.Checkout); !os.IsNotExist(err) {
		t.Errorf("the earlier job's staged checkout is still there (%v)", err)
	}
}

// Where a scope's jobs run at once, each run's checkouts are its own: a run removes its own run's
// earlier checkouts and what the scope's dead runs left, an earlier binary's checkout among them,
// and never what a live run staged.
func TestRunsOfOneScopeAtOnceKeepTheirOwnStagedDirectories(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	table := dockerfake.NewRunTable()
	first, dir := remoteMachine(t)
	first.Run, first.Runs = "run-a", table.Process()
	// another is a process of the run of the scope: a machine of its own on the same remote root.
	another := func(run string) *machine.Remote {
		return &machine.Remote{Shell: first.Shell, Rsync: first.Rsync, Root: first.Root, Scope: first.Scope, Timers: first.Timers, Run: run, Runs: table.Process()}
	}
	second := another("run-b")
	local := filepath.Join(dir, "local")
	stage := func(m *machine.Remote, job string) docker.Dirs {
		t.Helper()
		d := dirsIn(t, local, job)
		write(t, filepath.Join(d.Checkout, "src.c"), job)
		staged, err := m.Stage(ctx, d)
		if err != nil {
			t.Fatal(err)
		}
		return staged
	}
	exists := func(p string) bool {
		_, err := os.Stat(p)
		return err == nil
	}
	earlierBinary := filepath.Join(first.Root, "staged", first.Scope, "0123456789abcdef", "checkout")
	write(t, filepath.Join(earlierBinary, "src.c"), "old")

	a := stage(first, "job-a")
	b := stage(second, "job-b")
	if !exists(a.Checkout) || !exists(b.Checkout) || a.Checkout == b.Checkout {
		t.Fatalf("the live runs' checkouts %s and %s are not both there", a.Checkout, b.Checkout)
	}
	if exists(earlierBinary) {
		t.Error("the checkout an earlier binary left is still there")
	}
	// The first run's process dies; a later run of the scope removes what it left, and keeps the
	// second's.
	first.Runs.(*dockerfake.Runs).Die()
	c := stage(another("run-c"), "job-c")
	if exists(a.Checkout) || !exists(b.Checkout) || !exists(c.Checkout) {
		t.Errorf("after the first run died: its checkout there %v, the live ones' %v and %v; want only the live ones'",
			exists(a.Checkout), exists(b.Checkout), exists(c.Checkout))
	}
	if table.Live("run-a") {
		t.Error("the dead run is still held after its directories were removed")
	}
}

func TestARemoteMachinesCacheIsKeptAndItsUserIsTheSSHUsers(t *testing.T) {
	m, _ := remoteMachine(t)
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	dir, err := m.Cache(ctx, "task-0123")
	if err != nil {
		t.Fatal(err)
	}
	write(t, filepath.Join(dir, "ccache", "x"), "kept")
	if again, err := m.Cache(ctx, "task-0123"); err != nil || again != dir || read(t, filepath.Join(dir, "ccache", "x")) != "kept" {
		t.Errorf("the cache %s was not kept (%v)", again, err)
	}
	u, err := m.User(ctx)
	if err != nil || u.UID != os.Getuid() || u.GID != os.Getgid() {
		t.Errorf("user %+v, %v; want %d:%d", u, err, os.Getuid(), os.Getgid())
	}
}

func TestALocalMachineSharesTheCallersDirectories(t *testing.T) {
	root := t.TempDir()
	l := machine.Local{CacheRoot: root, As: docker.User{UID: 1, GID: 2}}
	d := docker.Dirs{Checkout: "/c", Scratch: "/s", Artifacts: "/a"}
	if got, err := l.Stage(context.Background(), d); err != nil || got != d {
		t.Errorf("stage %+v, %v", got, err)
	}
	if dir, err := l.Cache(context.Background(), "k"); err != nil || dir != filepath.Join(root, "k") {
		t.Errorf("cache %s, %v", dir, err)
	} else if st, err := os.Stat(dir); err != nil || !st.IsDir() {
		t.Errorf("the cache directory was not made")
	}
}

// A program is placed there by its content, runnable, and placed again only when its content
// changes. Regression: it was copied as a directory, which rsync refuses for a file.
func TestARemoteMachinePlacesAProgramOnceByItsContent(t *testing.T) {
	m, dir := remoteMachine(t)
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	local := filepath.Join(dir, "local", "probe-ready")
	write(t, local, "#!/bin/sh\necho v1\n")
	first, err := m.Program(ctx, local)
	if err != nil {
		t.Fatal(err)
	}
	if out, err := exec.CommandContext(ctx, first).Output(); err != nil || string(out) != "v1\n" {
		t.Fatalf("the placed program %s ran with %q, %v", first, out, err)
	}
	if again, err := m.Program(ctx, local); err != nil || again != first {
		t.Errorf("the same program was placed at %s (%v), want %s", again, err, first)
	}
	write(t, local, "#!/bin/sh\necho v2\n")
	second, err := m.Program(ctx, local)
	if err != nil || second == first || filepath.Base(second) != "probe-ready" {
		t.Fatalf("the changed program was placed at %s (%v), after %s", second, err, first)
	}
	if out, err := exec.CommandContext(ctx, second).Output(); err != nil || string(out) != "v2\n" {
		t.Errorf("the changed program ran with %q, %v", out, err)
	}
}
