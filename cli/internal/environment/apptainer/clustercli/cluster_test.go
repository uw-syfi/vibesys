package clustercli_test

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"testing/quick"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clustercli"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/propcheck"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
)

// fakeSrun runs a step here: it drops srun's options, takes the job's id from --jobid, and exits
// with the step's code, 128 plus the signal for one a signal ended, as srun does. With $SRUN_FAIL
// set it fails as srun does before a step starts, with that code. A step of a container
// (--container-image) is Pyxis's: its options go to pyxis.log, one step a line, and the command
// runs in --container-workdir with the environment srun was given, as Pyxis passes it in. The node's
// file system is the directory the file node_root beside it names, under which a step's mkdir
// makes its directories.
const fakeSrun = `#!/bin/sh
if [ -n "${SRUN_FAIL:-}" ]; then echo "srun: error: Unable to confirm allocation for job" >&2; exit "$SRUN_FAIL"; fi
NODE_ROOT=$(cat "$(dirname "$0")/node_root" 2>/dev/null)
image= pyxis=
while [ $# -gt 0 ]; do
  case "$1" in
    --jobid=*) SLURM_JOB_ID="${1#--jobid=}"; export SLURM_JOB_ID; shift ;;
    --container-image=*) image="${1#--container-image=}"; pyxis="$pyxis $1"; shift ;;
    --container-workdir=*) workdir="${1#--container-workdir=}"; pyxis="$pyxis $1"; shift ;;
    --container-*|--no-container-*) pyxis="$pyxis $1"; shift ;;
    --*) shift ;;
    *) break ;;
  esac
done
if [ "$1" = mkdir ] && [ -n "${NODE_ROOT:-}" ]; then exec mkdir -p "$NODE_ROOT$3"; fi
if [ -n "$image" ]; then
  echo "$pyxis" >> "$(dirname "$0")/pyxis.log"
  if [ -n "${workdir:-}" ]; then cd "$NODE_ROOT$workdir" || exit 1; fi
fi
"$@"
`

// fakeApptainer runs a container here: exec records its options and runs the command with the
// APPTAINERENV_ variables set.
const fakeApptainer = `#!/bin/sh
log="$(dirname "$0")/apptainer.log"
verb="$1"; shift
echo "$*" >> "$log"
while [ $# -gt 0 ]; do
  case "$1" in
    --home|--no-mount|--pwd|--bind) shift 2 ;;
    --*) shift ;;
    *) break ;;
  esac
done
shift
vars=$(env | sed -n 's/^APPTAINERENV_//p')
IFS='
'
set -f
exec env -i PATH="$PATH" $vars "$@"
`

// fakeScancel records what it cancels.
const fakeScancel = `#!/bin/sh
echo "$*" >> "$(dirname "$0")/scancel.log"
`

// shell runs programs here, finding the fakes first, and takes an exit code of 255 for the
// transport's when lost255 is set, as ssh's is, and what ssh says of it as ssh does. With exit set,
// every program reads its input and exits with that code instead, as a connection that drops does.
// While drops is above zero, each call takes one from it and, instead of its program, writes the
// lines dropped to standard error and exits with 255, as ssh does when it cannot connect, and so
// does each call while refusing, when set, says the login node refuses connections. calls counts
// the calls.
type shell struct {
	bin      string
	lost255  bool
	env      []string
	exit     string
	drops    *atomic.Int32
	dropped  string
	calls    *atomic.Int32
	refusing func() bool
}

// kexDropped is what ssh wrote when a login node closed its connection during the key exchange.
const kexDropped = "kex_exchange_identification: Connection closed by remote host\r\nConnection closed by UNKNOWN port 65535\r\n"

func (s shell) Command(ctx context.Context, argv []string) *exec.Cmd {
	s.calls.Add(1)
	if s.drops.Add(-1) >= 0 || (s.refusing != nil && s.refusing()) {
		return exec.CommandContext(ctx, "/bin/sh", "-c", `printf "$1" >&2; exit 255`, "vs-ssh", s.dropped)
	}
	if s.exit != "" {
		argv = []string{"/bin/sh", "-c", "cat > /dev/null; exit " + s.exit}
	}
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	if p, err := exec.LookPath(filepath.Join(s.bin, argv[0])); err == nil && !strings.Contains(argv[0], "/") {
		cmd = exec.CommandContext(ctx, p, argv[1:]...)
	}
	cmd.Env = append(append(os.Environ(), "PATH="+s.bin+":"+os.Getenv("PATH")), s.env...)
	return cmd
}

func (s shell) Lost(code int) bool { return s.lost255 && code == 255 }
func (s shell) Unreached(code int, stderr string) bool {
	return s.lost255 && (remote.SSH{}).Unreached(code, stderr)
}
func (shell) Where() string { return "host-a" }

func newShell(t *testing.T) shell {
	t.Helper()
	bin := t.TempDir()
	for name, text := range map[string]string{"srun": fakeSrun, "apptainer": fakeApptainer, "scancel": fakeScancel} {
		if err := os.WriteFile(filepath.Join(bin, name), []byte(text), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	return shell{bin: bin, drops: &atomic.Int32{}, dropped: kexDropped, calls: &atomic.Int32{}}
}

func newCluster(t *testing.T, sh shell) *clustercli.Cluster {
	t.Helper()
	c, err := clustercli.New(clustercli.Config{Shell: sh, Timers: envtest.NewTimers(), Submit: []string{"sbatch"}})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func ctx(t *testing.T) context.Context {
	c, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	t.Cleanup(cancel)
	return c
}

func entries(t *testing.T, dir string) []string {
	t.Helper()
	list, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, e := range list {
		names = append(names, e.Name())
	}
	return names
}

// A published file is there whole, and read back as it was.
func TestAPublishedFileIsReadBackWhole(t *testing.T) {
	c := newCluster(t, newShell(t))
	dir := t.TempDir()
	p := filepath.Join(dir, "images", "abc.size")
	if has, err := c.Has(ctx(t), p); err != nil || has {
		t.Fatalf("has %v, %v before the publish", has, err)
	}
	if _, ok, err := c.Read(ctx(t), p); err != nil || ok {
		t.Fatalf("read %v, %v before the publish", ok, err)
	}
	if err := c.Publish(ctx(t), p, strings.NewReader("12345\n")); err != nil {
		t.Fatal(err)
	}
	got, ok, err := c.Read(ctx(t), p)
	if err != nil || !ok || string(got) != "12345\n" {
		t.Fatalf("read %q, %v, %v", got, ok, err)
	}
	if has, err := c.Has(ctx(t), p); err != nil || !has {
		t.Fatalf("has %v, %v", has, err)
	}
	if names := entries(t, filepath.Join(dir, "images")); !slices.Equal(names, []string{"abc.size"}) {
		t.Errorf("files %v, want only the published one", names)
	}
}

// failing gives some bytes, then fails.
type failing struct{ n int }

func (f *failing) Read(p []byte) (int, error) {
	if f.n <= 0 {
		return 0, errors.New("docker save died")
	}
	n := min(len(p), f.n)
	for i := range n {
		p[i] = 'x'
	}
	f.n -= n
	return n, nil
}

// A copy whose data fails part way leaves the path as it was, and no part of the copy.
func TestACopyCutShortLeavesNothing(t *testing.T) {
	c := newCluster(t, newShell(t))
	dir := t.TempDir()
	p := filepath.Join(dir, "a.tar.gz")
	if err := os.WriteFile(p, []byte("old"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := c.Publish(ctx(t), p, &failing{n: 100_000}); err == nil {
		t.Fatal("a copy cut short was published")
	}
	if got, _ := os.ReadFile(p); string(got) != "old" {
		t.Errorf("the path holds %d bytes, want it as it was", len(got))
	}
	if names := entries(t, dir); !slices.Equal(names, []string{"a.tar.gz"}) {
		t.Errorf("files %v: a part of the copy was left", names)
	}
}

// A copy whose transport exits as ssh does when its connection drops is lost, and may be made
// again; one whose program failed, or whose 255 is not the transport's, is not.
func TestACopyWhoseConnectionDropsIsLost(t *testing.T) {
	for _, c := range []struct {
		exit    string
		lost255 bool
		lost    bool
	}{{"255", true, true}, {"255", false, false}, {"1", true, false}} {
		sh := newShell(t)
		sh.exit, sh.lost255 = c.exit, c.lost255
		err := newCluster(t, sh).Publish(ctx(t), "/shared/vibesys/images/x.sif", strings.NewReader("a SIF"))
		if err == nil || errors.Is(err, apptainer.ErrConnectionLost) != c.lost {
			t.Errorf("exit %s (lost255 %v): %v, want lost %v", c.exit, c.lost255, err, c.lost)
		}
	}
}

// Publishers of one path at once each copy a file of their own, and the path ends as one of them,
// whole.
func TestPublishersAtOnceLeaveOneWholeFile(t *testing.T) {
	c := newCluster(t, newShell(t))
	dir := t.TempDir()
	p := filepath.Join(dir, "images", "x.tar.gz")
	const n, size = 6, 256 << 10
	var wg sync.WaitGroup
	errs := make([]error, n)
	for i := range n {
		wg.Go(func() { errs[i] = c.Publish(ctx(t), p, bytes.NewReader(bytes.Repeat([]byte{byte('a' + i)}, size))) })
	}
	wg.Wait()
	for _, err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	got, err := os.ReadFile(p)
	if err != nil || len(got) != size || bytes.Count(got, got[:1]) != size {
		t.Fatalf("the path holds %d bytes, not one publisher's whole file (%v)", len(got), err)
	}
	if names := entries(t, filepath.Join(dir, "images")); !slices.Equal(names, []string{"x.tar.gz"}) {
		t.Errorf("files %v, want only the published one", names)
	}
}

// The allocation is a batch job of the submit command with the options that make it one node's,
// never requeued, its limit in minutes, sized as asked, holding the node with a sleep; its id is
// the last line the command printed.
func TestAnAllocationIsSubmittedAndItsIDRead(t *testing.T) {
	sh := newShell(t)
	calls := filepath.Join(sh.bin, "submit.log")
	script := "#!/bin/sh\nprintf '%s\\n' \"$@\" > " + calls + "\necho 'filtering the request' >&2\necho 'site notice'\necho '4242;cluster-a'\n"
	if err := os.WriteFile(filepath.Join(sh.bin, "submit-wrapper"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	c, err := clustercli.New(clustercli.Config{Shell: sh, Timers: envtest.NewTimers(), Submit: []string{"submit-wrapper", "--site"}})
	if err != nil {
		t.Fatal(err)
	}
	a := apptainer.Allocation{Name: "vibesys-apptainer-c", Comment: "d42", Limit: 61*time.Minute + time.Second, Options: []string{"--partition=part-a", "--account=acct-a"}}
	id, err := c.Submit(ctx(t), a)
	if err != nil || id != "4242" {
		t.Fatalf("id %q, %v", id, err)
	}
	data, _ := os.ReadFile(calls)
	got := strings.Split(strings.TrimSuffix(string(data), "\n"), "\n")
	want := []string{
		"--site", "--parsable", "--job-name=vibesys-apptainer-c", "--comment=d42", "--no-requeue", "--time=62", "--nodes=1", "--ntasks=1",
		"--partition=part-a", "--account=acct-a", "--output=/dev/null", "--wrap=exec sleep infinity",
	}
	if !slices.Equal(got, want) {
		t.Errorf("submitted with\n%q\nwant\n%q", got, want)
	}
}

func TestAJobIDIsTheLastLineSbatchPrinted(t *testing.T) {
	for out, want := range map[string]string{"17\n": "17", "notice\n17;c\n": "17", "": "", "Submitted batch job 17\n": "", "17x\n": ""} {
		got, err := clustercli.ParseJobID(out)
		if got != want || (err == nil) != (want != "") {
			t.Errorf("%q: %q, %v; want %q", out, got, err, want)
		}
	}
}

// A job's state is squeue's while it lists it, sacct's after, and ended when neither knows it; a
// squeue that failed otherwise is no answer.
func TestAJobsStateIsWhatSlurmSays(t *testing.T) {
	cases := []struct {
		out   string
		phase apptainer.Phase
		name  string
		err   bool
	}{
		{"squeue 0\nPENDING|Priority\n==sacct==\nPENDING\n", apptainer.Queued, "PENDING", false},
		{"squeue 0\nCONFIGURING|None\n==sacct==\n", apptainer.Queued, "CONFIGURING", false},
		{"squeue 0\nRUNNING|None\n==sacct==\nRUNNING\n", apptainer.Running, "RUNNING", false},
		{"squeue 0\nCOMPLETING|None\n==sacct==\nCANCELLED by 1002\n", apptainer.Suspended, "COMPLETING", false},
		{"squeue 0\n\n==sacct==\nCANCELLED by 1002\n", apptainer.Ended, "CANCELLED", false},
		{"squeue 1\nslurm_load_jobs error: Invalid job id specified\n==sacct==\nFAILED\n", apptainer.Ended, "FAILED", false},
		{"squeue 0\n\n==sacct==\n", apptainer.Ended, "", false},
		{"squeue 1\nslurm_load_jobs error: Unable to contact slurm controller\n==sacct==\n", 0, "", true},
	}
	for _, c := range cases {
		got, err := clustercli.ParseState(c.out)
		if (err != nil) != c.err || (err == nil && (got.Phase != c.phase || got.Name != c.name)) {
			t.Errorf("%q: %+v, %v", c.out, got, err)
		}
	}
	if got, _ := clustercli.ParseState("squeue 0\nPENDING|Priority\n==sacct==\n"); got.Reason != "Priority" {
		t.Errorf("reason %q", got.Reason)
	}
}

// A queued job's state carries Slurm's estimate of its start when it gives one, and a running
// job's its node; neither is made up when Slurm writes none.
func TestAQueuedJobsStateSaysWhenSlurmExpectsItToStart(t *testing.T) {
	for out, want := range map[string]apptainer.JobState{
		"squeue 0\nPENDING|Priority|2026-11-05T22:14:03|\n==sacct==\n":   {Phase: apptainer.Queued, Name: "PENDING", Reason: "Priority", Start: "2026-11-05T22:14:03"},
		"squeue 0\nPENDING|Resources|N/A|\n==sacct==\n":                  {Phase: apptainer.Queued, Name: "PENDING", Reason: "Resources"},
		"squeue 0\nPENDING|Priority|Unknown|\n==sacct==\n":               {Phase: apptainer.Queued, Name: "PENDING", Reason: "Priority"},
		"squeue 0\nRUNNING|None|2026-11-05T19:01:00|node-a\n==sacct==\n": {Phase: apptainer.Running, Name: "RUNNING", Reason: "None", Node: "node-a"},
	} {
		if got, err := clustercli.ParseState(out); err != nil || got != want {
			t.Errorf("%q: %+v, %v; want %+v", out, got, err, want)
		}
	}
}

// A job of the scope is one whose comment is the scope, or a run's scope under it; a scope that
// only starts with the scope's name is another.
func TestOnlyThisPackagesJobsOfTheScopeAreListed(t *testing.T) {
	out := "vibesys-apptainer-vibesys-cli-01|d1|11|RUNNING|1-02:03:04\nvibesys-apptainer-vibesys-cli-02|d2|12|RUNNING|5:00\n" +
		"other-job|d1|13|RUNNING|5:00\nvibesys-docker-vibesys-cli-03|d1|14|RUNNING|5:00\nvibesys-apptainer-pool-x|d1|15|PENDING|1-00:00:00\n" +
		"vibesys-apptainer-vibesys-cli-04|d1.r1|16|PENDING|5:00\nvibesys-apptainer-vibesys-cli-05|d10|17|PENDING|5:00\n" +
		"vibesys-apptainer-vibesys-cli-06|d1.|18|PENDING|5:00\n\n"
	want := []apptainer.Job{
		{ID: "11", Name: "vibesys-apptainer-vibesys-cli-01", Comment: "d1", Phase: apptainer.Running, Left: 26*time.Hour + 3*time.Minute + 4*time.Second},
		{ID: "15", Name: "vibesys-apptainer-pool-x", Comment: "d1", Phase: apptainer.Queued},
		{ID: "16", Name: "vibesys-apptainer-vibesys-cli-04", Comment: "d1.r1", Phase: apptainer.Queued},
	}
	if got := clustercli.ParseJobs(out, "d1"); !slices.Equal(got, want) {
		t.Errorf("%+v", got)
	}
}

// The time left is read in every form squeue writes it, and what is not a time is none left, so a
// job whose time left is not known is never taken to have enough.
func TestTheTimeLeftIsReadAsSqueueWritesIt(t *testing.T) {
	err := quick.Check(func(days, h, m, sec uint8) bool {
		d := time.Duration(days%7)*24*time.Hour + time.Duration(h%24)*time.Hour + time.Duration(m%60)*time.Minute + time.Duration(sec%60)*time.Second
		var text string
		switch {
		case d >= 24*time.Hour:
			text = fmt.Sprintf("%d-%02d:%02d:%02d", days%7, h%24, m%60, sec%60)
		case d >= time.Hour:
			text = fmt.Sprintf("%d:%02d:%02d", h%24, m%60, sec%60)
		default:
			text = fmt.Sprintf("%d:%02d", m%60, sec%60)
		}
		return clustercli.ParseTimeLeft(text) == d
	}, propcheck.Config(t, nil))
	if err != nil {
		t.Error(err)
	}
	for _, s := range []string{"", "INVALID", "NOT_SET", "1-2:3", "-1:00", "a:b", "1:2:3:4", "1-"} {
		if got := clustercli.ParseTimeLeft(s); got != 0 {
			t.Errorf("%q is %v left", s, got)
		}
	}
	if clustercli.ParseTimeLeft("UNLIMITED") < 1000*time.Hour {
		t.Error("UNLIMITED is not more than any limit")
	}
}

// A chosen port is never one a socket of the node has, and is in the range, whatever is taken.
func TestAChosenPortIsFree(t *testing.T) {
	r := rand.New(rand.NewPCG(1, 2))
	for range 200 {
		taken := map[int]bool{}
		for range r.IntN(3000) {
			taken[20000+r.IntN(12768)] = true
		}
		seed := fmt.Sprint(r.Int())
		p, err := clustercli.FreePort(taken, seed)
		if err != nil || taken[p] || p < 20000 || p > 32767 {
			t.Fatalf("port %d, %v", p, err)
		}
	}
	all := map[int]bool{}
	for p := 20000; p <= 32767; p++ {
		all[p] = true
	}
	if _, err := clustercli.FreePort(all, "s"); err == nil {
		t.Error("a port was chosen with every one taken")
	}
	tcp := "  sl  local_address rem_address   st\n   0: 00000000:4E21 00000000:0000 0A\n   1: 0100007F:7531 0100007F:9C40 01\n" +
		"  sl  local_address                         remote_address\n   0: 00000000000000000000000000000000:4E22 00000000000000000000000000000000:0000 0A\n"
	if got := clustercli.TakenPorts(tcp); len(got) != 3 || !got[20001] || !got[30001] || !got[20002] {
		t.Errorf("taken %v", got)
	}
}

func node(t *testing.T, sh shell) apptainer.Node {
	t.Helper()
	return newCluster(t, sh).Node("77")
}

// announced is a writer that closes up at its first write.
type announced struct {
	once sync.Once
	up   chan struct{}
}

// Write implements io.Writer.
func (a *announced) Write(p []byte) (int, error) {
	a.once.Do(func() { close(a.up) })
	return len(p), nil
}

func wait(t *testing.T, p environment.Process) environment.Exit {
	t.Helper()
	select {
	case e := <-p.Exited():
		return e
	case <-ctx(t).Done():
		t.Fatal("the process never ended")
	}
	return environment.Exit{}
}

// A box's process runs its command in the SIF with the box's variables, mounts, directory and
// GPUs, its standard error merged into its output, and ends with the command's code.
func TestABoxsProcessRunsTheCommandInTheContainer(t *testing.T) {
	sh := newShell(t)
	sh.env = []string{"ROCR_VISIBLE_DEVICES=0,1", "HIP_VISIBLE_DEVICES="}
	n := node(t, sh)
	sif := filepath.Join(t.TempDir(), "x.sif")
	box := apptainer.Box{
		Name: "vibesys-cli-0a", Image: sif, Dir: "/work", Env: map[string]string{"GREETING": "a, b"},
		Mounts: []docker.Mount{{Host: "/shared/run", Container: "/work"}, {Host: "/shared/model", Container: "/model", ReadOnly: true}},
		ROCm:   true,
	}
	var out bytes.Buffer
	p, err := n.Start(box, []string{"/bin/sh", "-c", `echo "$GREETING $VIBESYS_BOX $ROCR_VISIBLE_DEVICES ${HIP_VISIBLE_DEVICES-unset}"; echo oops >&2; exit 3`}, &out)
	if err != nil {
		t.Fatal(err)
	}
	if e := wait(t, p); e != (environment.Exit{Code: 3}) {
		t.Fatalf("exit %+v", e)
	}
	if got := out.String(); got != "a, b vibesys-cli-0a 0,1 unset\noops\n" {
		t.Errorf("output %q", got)
	}
	log, _ := os.ReadFile(filepath.Join(sh.bin, "apptainer.log"))
	for _, want := range []string{"--cleanenv", "--no-mount tmp,cwd,bind-paths --bind /etc/hosts:/etc/hosts:ro", "--pwd /work", "--bind /shared/run:/work ", "--bind /shared/model:/model:ro", "--rocm", sif + " /bin/sh"} {
		if !strings.Contains(string(log), want) {
			t.Errorf("apptainer exec %q lacks %q", log, want)
		}
	}
}

// A step that did not start its command (srun failed, or the transport's own exit) is lost, never
// the command's exit.
func TestAStepThatDidNotStartItsCommandIsLost(t *testing.T) {
	for _, c := range []struct {
		code    string
		lost255 bool
	}{{"1", false}, {"255", true}, {"255", false}} {
		sh := newShell(t)
		sh.lost255 = c.lost255
		sh.env = []string{"SRUN_FAIL=" + c.code}
		p, err := node(t, sh).Start(apptainer.Box{Name: "b", Image: "/shared/x.sif"}, []string{"true"}, io.Discard)
		if err != nil {
			t.Fatal(err)
		}
		if e := wait(t, p); !errors.Is(e.Err, docker.ErrLost) {
			t.Errorf("srun exit %s: %+v, want lost", c.code, e)
		}
	}
	// A command's own 255 through ssh is the transport's.
	sh := newShell(t)
	sh.lost255 = true
	p, _ := node(t, sh).Start(apptainer.Box{Name: "b", Image: "/shared/x.sif"}, []string{"/bin/sh", "-c", "exit 255"}, io.Discard)
	if e := wait(t, p); !errors.Is(e.Err, docker.ErrLost) {
		t.Errorf("%+v, want lost", e)
	}
}

// A signal reaches every process of the box, and no other box's.
func TestASignalReachesTheBoxsProcesses(t *testing.T) {
	sh := newShell(t)
	n := node(t, sh)
	name := fmt.Sprintf("vibesys-cli-sig%d", os.Getpid())
	// A process exists to be signalled only once it has run: a box's process says so, and the test
	// waits for that, since a signal sent before the process starts reaches nothing.
	start := func(box string) environment.Process {
		up := &announced{up: make(chan struct{})}
		p, err := n.Start(apptainer.Box{Name: box, Image: "/shared/x.sif"}, []string{"/bin/sh", "-c", "echo up; exec sleep 600"}, up)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(p.Kill)
		select {
		case <-up.up:
		case <-ctx(t).Done():
			t.Fatalf("the process of %s never started", box)
		}
		return p
	}
	a, other := start(name), start(name+"-other")
	if err := n.Signal(ctx(t), name, apptainer.SIGTERM); err != nil {
		t.Fatal(err)
	}
	if e := wait(t, a); e.Code != 128+15 {
		t.Errorf("exit %+v, want SIGTERM's", e)
	}
	select {
	case e := <-other.Exited():
		t.Errorf("another box's process ended: %+v", e)
	default:
	}
	if err := n.Signal(ctx(t), name+"-other", apptainer.SIGKILL); err != nil {
		t.Fatal(err)
	}
	if e := wait(t, other); e.Code != 128+9 {
		t.Errorf("exit %+v, want SIGKILL's", e)
	}
}

// The holder runs until it is closed, then removes the job's directory of the node and cancels the
// allocation.
func TestClosingTheHoldCancelsTheAllocation(t *testing.T) {
	sh := newShell(t)
	tmp := t.TempDir()
	sh.env = []string{"TMPDIR=" + tmp}
	if err := os.MkdirAll(filepath.Join(tmp, "vibesys-job-77", "home-b"), 0o755); err != nil {
		t.Fatal(err)
	}
	h, err := node(t, sh).Hold(ctx(t))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(sh.bin, "scancel.log")); err == nil {
		t.Fatal("cancelled while held")
	}
	if err := h.Close(); err != nil {
		t.Fatal(err)
	}
	log, _ := os.ReadFile(filepath.Join(sh.bin, "scancel.log"))
	if strings.TrimSpace(string(log)) != "77" {
		t.Errorf("scancel %q, want the job", log)
	}
	if names := entries(t, tmp); len(names) != 0 {
		t.Errorf("left on the node: %v", names)
	}
}

// A node is read script by script, each script's output by its name, and a script that fails says
// nothing.
func TestANodeIsReadScriptByScript(t *testing.T) {
	c := newCluster(t, newShell(t))
	got, err := c.Node("77").Read(ctx(t), map[string]string{"a": "echo one; echo ==driver==", "b": "echo two >&2; exit 4", "c": "printf 'three'"})
	if err != nil {
		t.Fatal(err)
	}
	if want := map[string]string{"a": "one\n==driver==\n", "b": "", "c": "three\n"}; fmt.Sprint(got) != fmt.Sprint(want) {
		t.Errorf("read %q, want %q", got, want)
	}
}
