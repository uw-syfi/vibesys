// Package clustercli is the apptainer.Cluster of a real Slurm cluster, reached through a shell on
// its login node (ssh, in production): files are written on the file system the login node shares
// with the compute nodes, allocations are asked for with the site's submit command, and everything
// that runs on a node is a step of the allocation (srun --overlap) started from the login node.
//
// What it relies on of the cluster, each checked on one: sbatch --parsable prints the job's id; an
// srun step started with --jobid and --overlap from the login node runs on the job's node beside
// its other steps and propagates its command's exit code; a step reads the standard input srun
// was given; apptainer exec passes APPTAINERENV_ variables into a clean environment, binds read-only
// with :ro, mounts none of the site's bind paths with --no-mount bind-paths, and gives the node's
// AMD GPUs with --rocm. The cluster's apptainer only runs SIFs; none is built on it.
package clustercli

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"maps"
	"math"
	"os/exec"
	"path"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/slurm"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
)

// Bounds of the calls.
const (
	// CallTimeout bounds a call that should answer at once.
	CallTimeout = apptainer.CallTimeout
	// DefaultPoll is the pause between two reads of a cancelled job's state.
	DefaultPoll = 5 * time.Second
)

// DefaultProgram is the apptainer program on the nodes.
const DefaultProgram = "apptainer"

// Config configures New.
type Config struct {
	// Shell runs programs on the login node.
	Shell remote.Shell
	// Timers bounds the calls.
	Timers clock.Timers
	// Submit is the command that submits a batch job, and its arguments: sbatch, or the site's
	// command that wraps it. The allocation's options follow it.
	Submit []string
	// Program is the apptainer program on the nodes; empty is DefaultProgram.
	Program string
	// Runtime is what runs a box's processes on the nodes.
	Runtime apptainer.Runtime
	// Poll is the pause between two reads of a cancelled job's state; zero is DefaultPoll.
	Poll time.Duration
}

// Cluster is the cluster. Create it with New. It is safe for concurrent use.
type Cluster struct {
	cfg Config

	mu    sync.Mutex
	ports int // how many ports this process chose, which spreads its choices
}

var _ apptainer.Cluster = (*Cluster)(nil)

// New returns the cluster, or a core.FailureInvalidInput naming what is missing.
func New(cfg Config) (*Cluster, error) {
	switch {
	case cfg.Shell == nil:
		return nil, invalid("the config has no Shell")
	case cfg.Timers == nil:
		return nil, invalid("the config has no Timers")
	case len(cfg.Submit) == 0 || cfg.Submit[0] == "":
		return nil, invalid("the config has no submit command")
	}
	if cfg.Program == "" {
		cfg.Program = DefaultProgram
	}
	if cfg.Poll <= 0 {
		cfg.Poll = DefaultPoll
	}
	return &Cluster{cfg: cfg}, nil
}

func invalid(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: "apptainer: " + fmt.Sprintf(format, args...)}
}

func unavailable(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureUnavailable, Message: "apptainer: " + fmt.Sprintf(format, args...)}
}

// sh runs the script with /bin/sh on the login node, each attempt bounded by d, with args as $1
// and on. A call that does not reach the login node is made again as remote.Reconnect says, so a
// login node that refuses new connections for a minute or two fails no call.
func (c *Cluster) sh(ctx context.Context, d time.Duration, script string, args ...string) (string, error) {
	return c.within(ctx, d, append([]string{"/bin/sh", "-c", script, "vs-apptainer"}, args...))
}

// within runs argv on the login node as remote.Within does, saying so when it did not answer.
func (c *Cluster) within(ctx context.Context, d time.Duration, argv []string) (string, error) {
	out, err := remote.Within(ctx, c.cfg.Timers, c.cfg.Shell, d, argv, "")
	if na := (*remote.NoAnswer)(nil); errors.As(err, &na) {
		return "", unavailable("%s did not answer within %v", na.Where, na.Within)
	}
	return out, err
}

func checkPath(p string) error {
	if !path.IsAbs(p) || path.Clean(p) != p || strings.ContainsAny(p, "\n\x00") {
		return invalid("%q is not a clean absolute path", p)
	}
	return nil
}

// hasScript says whether a file is at $1.
const hasScript = `if [ -e "$1" ]; then echo yes; else echo no; fi`

// Has implements apptainer.Cluster.
func (c *Cluster) Has(ctx context.Context, p string) (bool, error) {
	if err := checkPath(p); err != nil {
		return false, err
	}
	out, err := c.sh(ctx, CallTimeout, hasScript, p)
	if err != nil {
		return false, err
	}
	switch strings.TrimSpace(out) {
	case "yes":
		return true, nil
	case "no":
		return false, nil
	}
	return false, unavailable("asking whether %s is there, %s answered %q", p, c.cfg.Shell.Where(), out)
}

// readScript prints a marker line, then the file at $1 when it is there.
const readScript = `if [ -f "$1" ]; then echo yes; exec cat "$1"; else echo no; fi`

// Read implements apptainer.Cluster.
func (c *Cluster) Read(ctx context.Context, p string) ([]byte, bool, error) {
	if err := checkPath(p); err != nil {
		return nil, false, err
	}
	out, err := c.sh(ctx, CallTimeout, readScript, p)
	if err != nil {
		return nil, false, err
	}
	first, rest, _ := strings.Cut(out, "\n")
	switch first {
	case "yes":
		return []byte(rest), true, nil
	case "no":
		return nil, false, nil
	}
	return nil, false, unavailable("reading %s, %s answered %q", p, c.cfg.Shell.Where(), first)
}

// uploadScript copies its input to a file of its own beside $1 and prints the file's name and size.
// A copy cut short by a hangup leaves no file; one whose input merely ended is checked by the
// caller, which knows how much it sent.
const uploadScript = `set -e
mkdir -p "$(dirname "$1")"
t="$1.tmp.$(hostname).$$"
trap 'rm -f "$t"; exit 1' HUP INT TERM
cat > "$t"
printf '%s %s\n' "$t" "$(wc -c < "$t" | tr -d ' ')"`

// commitScript renames $2 to $1 when it is $3 bytes long, and removes it otherwise.
const commitScript = `n=$(wc -c < "$2" | tr -d ' ')
if [ "$n" = "$3" ]; then exec mv -f "$2" "$1"; fi
rm -f "$2"
echo "the copy holds $n bytes of $3" >&2
exit 1`

// removeScript removes $1.
const removeScript = `rm -f "$1"`

// Publish implements apptainer.Cluster: the data goes to a file of this call's own beside the
// path, and is renamed to the path once its size is what was sent, so the path is never a part of
// a file, and publishers at once each rename a whole file of their own.
func (c *Cluster) Publish(ctx context.Context, p string, r io.Reader) error {
	if err := checkPath(p); err != nil {
		return err
	}
	count := &counting{r: r}
	cmd := c.cfg.Shell.Command(ctx, []string{"/bin/sh", "-c", uploadScript, "vs-apptainer", p})
	var out, errb lockedBuffer
	cmd.Stdin, cmd.Stdout, cmd.Stderr = count, &out, &errb
	err := cmd.Run()
	tmp, size, parsed := strings.Cut(strings.TrimSpace(out.String()), " ")
	parsed = parsed && strings.HasPrefix(tmp, p+".tmp.")
	if err != nil || count.err != nil || !parsed {
		if parsed {
			cleanup, stop := clock.Deadline(context.WithoutCancel(ctx), c.cfg.Timers, CallTimeout)
			_, _ = c.sh(cleanup, CallTimeout, removeScript, tmp)
			stop()
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if count.err != nil {
			return fmt.Errorf("copying to %s: %w", p, count.err)
		}
		failed := unavailable("copying to %s on %s failed: %v %s", p, c.cfg.Shell.Where(), err, clip(errb.String()))
		if exit := (*exec.ExitError)(nil); errors.As(err, &exit) && c.cfg.Shell.Lost(exit.ExitCode()) {
			return fmt.Errorf("%w (%w)", failed, apptainer.ErrConnectionLost)
		}
		return failed
	}
	if size != strconv.FormatInt(count.n, 10) {
		_, _ = c.sh(ctx, CallTimeout, removeScript, tmp)
		return unavailable("the copy to %s on %s holds %s bytes of the %d sent", p, c.cfg.Shell.Where(), size, count.n)
	}
	if _, err := c.sh(ctx, CallTimeout, commitScript, p, tmp, size); err != nil {
		return fmt.Errorf("publishing %s: %w", p, err)
	}
	return nil
}

// counting counts what is read through it, and keeps the reader's own failure apart from its end.
type counting struct {
	r   io.Reader
	n   int64
	err error
}

func (c *counting) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	c.n += int64(n)
	if err != nil && !errors.Is(err, io.EOF) {
		c.err = err
	}
	return n, err
}

// squashScript makes $1 a squashfs of the tar archive on its input with sqfstar, under a lock of
// the login node per path, unless it is there, and prints what made it. The squashfs is written to
// a file of its own beside $1 and renamed to $1 once ImageMarker in it holds $2: sqfstar takes a
// stream cut short between two entries for a whole one, and the marker is the stream's last entry.
const squashScript = `set -e
mkdir -p "$(dirname "$1")"
exec 9>"${TMPDIR:-/tmp}/vibesys-squash-$(id -u)-$(basename "$1").lock"
flock 9
by=$(sqfstar -version 2>&1 | head -n 1)
if [ -e "$1" ]; then printf '` + squashedMarker + ` %s\n' "$by"; exit 0; fi
t="$1.tmp.$(hostname).$$"
trap 'rm -f "$t"' EXIT
trap 'exit 1' HUP INT TERM
sqfstar -quiet -no-progress -all-root -comp zstd -Xcompression-level 3 -b 1M "$t" >&2
m=$(unsquashfs -cat "$t" ` + apptainer.ImageMarker + `)
if [ "$m" != "$2" ]; then echo "the squashfs's marker holds '$m', not '$2': its stream was cut short" >&2; exit 1; fi
mv -f "$t" "$1"
printf '` + squashedMarker + ` %s\n' "$by"`

// squashedMarker starts the line squashScript prints once the squashfs is there.
const squashedMarker = "vibesys-squashed"

// Squash implements apptainer.Cluster.
func (c *Cluster) Squash(ctx context.Context, p, content string, rootfs io.Reader) (string, error) {
	if err := checkPath(p); err != nil {
		return "", err
	}
	count := &counting{r: rootfs}
	cmd := c.cfg.Shell.Command(ctx, []string{"/bin/sh", "-c", squashScript, "vs-apptainer", p, content})
	var out, errb lockedBuffer
	cmd.Stdin, cmd.Stdout, cmd.Stderr = count, &out, &errb
	err := cmd.Run()
	by, made := strings.CutPrefix(strings.TrimSpace(out.String()), squashedMarker+" ")
	switch {
	case err == nil && made:
		// A squashfs already there is kept, and the stream is then not read to its end.
		return by, nil
	case ctx.Err() != nil:
		return "", ctx.Err()
	case count.err != nil:
		return "", fmt.Errorf("streaming to %s: %w", p, count.err)
	}
	failed := unavailable("making the squashfs %s on %s failed: %v %s", p, c.cfg.Shell.Where(), err, clip(errb.String()))
	if exit := (*exec.ExitError)(nil); errors.As(err, &exit) && c.cfg.Shell.Lost(exit.ExitCode()) {
		return "", fmt.Errorf("%w (%w)", failed, apptainer.ErrConnectionLost)
	}
	return "", failed
}

// Submit implements apptainer.Cluster. The job holds its node with a sleep until it is cancelled
// or its time limit; Slurm writes no file of it.
func (c *Cluster) Submit(ctx context.Context, a apptainer.Allocation) (string, error) {
	argv := append(slices.Clone(c.cfg.Submit), SubmitArgs(a)...)
	out, err := c.within(ctx, CallTimeout, argv)
	if err != nil {
		return "", fmt.Errorf("submitting the allocation %s: %w", a.Name, err)
	}
	return ParseJobID(out)
}

// SubmitArgs are the submit command's arguments of an allocation. A reusable one runs
// ReaperScript, which ends it once it has been idle for its Idle; any other sleeps until it is
// cancelled.
func SubmitArgs(a apptainer.Allocation) []string {
	minutes := int64((a.Limit + time.Minute - 1) / time.Minute)
	args := []string{
		"--parsable", "--job-name=" + a.Name, "--comment=" + a.Comment, "--no-requeue",
		"--time=" + strconv.FormatInt(minutes, 10), "--nodes=1", "--ntasks=1",
	}
	args = append(args, a.Options...)
	wrap := "exec sleep infinity"
	if a.Idle > 0 {
		wrap = ReaperScript(a.Idle, ReaperPoll)
	}
	return append(args, "--output=/dev/null", "--wrap="+wrap)
}

// ReaperPoll is how often a reusable allocation looks at its claim.
const ReaperPoll = 30 * time.Second

// poolDir is a reusable allocation's own directory on its node, which holds its claim: the lock
// of the claim (lock), the process that holds it (claim), and whether the allocation is ending
// (ended). It is a path every step and the batch script name alike.
const poolDir = `/tmp/vibesys-pool-$SLURM_JOB_ID`

// cleanFunction defines clean, which kills every process of a container in the job (one that
// carries EnvBox and is in the job's cgroup, or carries the job's id), waits for none to be left,
// up to a minute, and removes the containers' homes. It fails when some are left.
const cleanFunction = `clean() {
  i=0
  while :; do
    left=
    for f in $(grep -lz '^` + apptainer.EnvBox + `=' /proc/[0-9]*/environ 2>/dev/null); do
      p=${f#/proc/}; p=${p%/environ}
      if grep -q "/job_$SLURM_JOB_ID/" "/proc/$p/cgroup" 2>/dev/null || grep -qxz "SLURM_JOB_ID=$SLURM_JOB_ID" "/proc/$p/environ" 2>/dev/null; then
        kill -9 "$p" 2>/dev/null && left=1
      fi
    done
    [ -z "$left" ] && break
    i=$((i+1)); [ "$i" -ge 60 ] && return 1
    sleep 1
  done
  rm -rf "` + jobDir + `" "` + pyxisJobDir + `"
}`

// ReaperScript is the batch script of a reusable allocation: every poll, under the claim's lock,
// it counts the allocation busy while the process of its claim lives, clears what a dead claim
// left, and once no claim has lived for idle it marks the allocation ending, so nothing claims it,
// and exits, which ends the allocation.
func ReaperScript(idle, poll time.Duration) string {
	return cleanFunction + `
d="` + poolDir + `"
mkdir -p "$d"
idle=` + strconv.FormatInt(int64(idle/time.Second), 10) + `
last=$(date +%s)
exec 9>>"$d/lock"
while sleep ` + strconv.FormatFloat(poll.Seconds(), 'f', -1, 64) + `; do
  flock 9
  c=$(cat "$d/claim" 2>/dev/null)
  if [ -n "$c" ] && kill -0 "$c" 2>/dev/null; then
    last=$(date +%s)
  elif [ -n "$c" ]; then
    rm -f "$d/claim"; clean; last=$(date +%s)
  elif [ $(( $(date +%s) - last )) -ge "$idle" ]; then
    touch "$d/ended"; exit 0
  fi
  flock -u 9
done`
}

// ParseJobID reads the job's id from what sbatch --parsable printed: its last line, "id" or
// "id;cluster". A site's command may print more before it.
func ParseJobID(out string) (string, error) {
	lines := strings.Split(strings.TrimSpace(out), "\n")
	id, _, _ := strings.Cut(strings.TrimSpace(lines[len(lines)-1]), ";")
	if id == "" || strings.Trim(id, "0123456789") != "" {
		return "", unavailable("the submit command printed no job id: %q", clip(out))
	}
	return id, nil
}

// user is how a command names the shell's user.
const user = `"$(id -un)"`

// stateScript prints squeue's state, reason, estimated start and node of job $1, its exit status,
// and sacct's states.
const stateScript = `q=$(squeue -h -j "$1" -o '%T|%r|%S|%N' 2>&1); r=$?
printf 'squeue %s\n%s\n==sacct==\n' "$r" "$q"
sacct -n -X -P -j "$1" -o State 2>/dev/null || true`

// State implements apptainer.Cluster.
func (c *Cluster) State(ctx context.Context, job string) (apptainer.JobState, error) {
	if strings.Trim(job, "0123456789") != "" || job == "" {
		return apptainer.JobState{}, invalid("%q is not a job id", job)
	}
	out, err := c.sh(ctx, CallTimeout, stateScript, job)
	if err != nil {
		return apptainer.JobState{}, fmt.Errorf("reading the state of the job %s: %w", job, err)
	}
	return ParseState(out)
}

// ParseState reads stateScript's output. A job squeue lists is queued or running as it says, with
// its reason, Slurm's estimate of its start while it is queued (none for "N/A" or "Unknown") and
// its node while it runs, and any other state of it is ended; one it does not know is what sacct says, and ended when sacct
// knows nothing of it either. A squeue that failed for another reason than an unknown job is no
// answer.
func ParseState(out string) (apptainer.JobState, error) {
	head, sacct, _ := strings.Cut(out, "==sacct==\n")
	status, squeue, _ := strings.Cut(head, "\n")
	squeue = strings.TrimSpace(squeue)
	if strings.TrimSpace(status) != "squeue 0" && !strings.Contains(squeue, "Invalid job id") {
		return apptainer.JobState{}, unavailable("squeue failed: %s", clip(squeue))
	}
	if strings.TrimSpace(status) == "squeue 0" && squeue != "" {
		f := append(strings.Split(strings.Split(squeue, "\n")[0], "|"), "", "", "")
		name := strings.TrimSpace(f[0])
		st := apptainer.JobState{Phase: phaseOf(name), Name: name, Reason: strings.TrimSpace(f[1])}
		switch start := strings.TrimSpace(f[2]); {
		case st.Phase != apptainer.Queued, start == "N/A", strings.EqualFold(start, "Unknown"):
		default:
			st.Start = start
		}
		if st.Phase == apptainer.Running {
			st.Node = strings.TrimSpace(f[3])
		}
		return st, nil
	}
	for _, line := range strings.Split(sacct, "\n") {
		if f := strings.Fields(line); len(f) > 0 {
			// sacct writes "CANCELLED by 1002"; the first word is the state.
			name := strings.TrimRight(f[0], "+")
			return apptainer.JobState{Phase: recordedPhaseOf(name), Name: name}, nil
		}
	}
	return apptainer.JobState{Phase: apptainer.Ended}, nil
}

// definitelyEnded are the Slurm job states that end a job for good, as `man squeue` lists them.
// Every other state is a job that lives, and phaseOf never calls it ended: a state this package does
// not know, or one that only looks like an end (PREEMPTED while squeue still lists the job, which
// Slurm then requeues or is about to cancel), must not make a caller let go of a job that runs on.
var definitelyEnded = map[string]bool{
	"BOOT_FAIL": true, "CANCELLED": true, "COMPLETED": true, "DEADLINE": true, "FAILED": true,
	"NODE_FAIL": true, "OUT_OF_MEMORY": true, "REVOKED": true, "TIMEOUT": true,
}

// phaseOf is the phase of a job squeue lists in the state. A listed job is ended only in a state
// that ends a job for good; PREEMPTED, SUSPENDED, STOPPED, REQUEUED, RESIZING, CONFIGURING,
// COMPLETING, SIGNALING and the rest live (see recordedPhaseOf for a job squeue no longer lists).
// SUSPENDED and STOPPED jobs, and those that are closing (COMPLETING, STAGE_OUT), hold their node and
// take no new work: apptainer.Suspended.
func phaseOf(state string) apptainer.Phase {
	switch s := strings.ToUpper(strings.TrimSpace(state)); {
	case definitelyEnded[s]:
		return apptainer.Ended
	case s == "SUSPENDED" || s == "STOPPED" || s == "COMPLETING" || s == "STAGE_OUT":
		return apptainer.Suspended
	case s == "RUNNING" || s == "SIGNALING":
		return apptainer.Running
	}
	// PENDING, CONFIGURING, REQUEUED, REQUEUE_FED, REQUEUE_HOLD, RESIZING, RESV_DEL_HOLD, SPECIAL_EXIT,
	// PREEMPTED, and any state Slurm adds: it waits, and may run.
	return apptainer.Queued
}

// recordedPhaseOf is the phase of a job that squeue does not list, from the state sacct or scontrol
// recorded for it. Such a job that was preempted is gone: a preempted job that is requeued is
// listed again as pending, so a record that still says PREEMPTED is a job that will not come back.
func recordedPhaseOf(state string) apptainer.Phase {
	if strings.ToUpper(strings.TrimSpace(state)) == "PREEMPTED" {
		return apptainer.Ended
	}
	return phaseOf(state)
}

// Jobs implements apptainer.Cluster.
func (c *Cluster) Jobs(ctx context.Context, scope string) ([]apptainer.Job, error) {
	out, err := c.sh(ctx, CallTimeout, `squeue -h -u `+user+` -o '%j|%k|%i|%T|%L'`)
	if err != nil {
		return nil, fmt.Errorf("listing the Slurm jobs: %w", err)
	}
	return ParseJobs(out, scope), nil
}

// ParseJobs reads squeue's "name|comment|id|state|time left" lines and returns this package's jobs
// of the scope (apptainer.InScope).
func ParseJobs(out, scope string) []apptainer.Job {
	var jobs []apptainer.Job
	for _, line := range strings.Split(out, "\n") {
		f := strings.Split(strings.TrimSpace(line), "|")
		if len(f) < 2 || !strings.HasPrefix(f[0], apptainer.JobPrefix) {
			continue
		}
		comment := strings.TrimSpace(f[1])
		if _, ok := apptainer.InScope(comment, scope); !ok {
			continue
		}
		f = append(f, "", "", "")
		j := apptainer.Job{Name: f[0], Comment: comment, ID: strings.TrimSpace(f[2]), Phase: phaseOf(f[3])}
		if j.Phase == apptainer.Running {
			j.Left = ParseTimeLeft(f[4])
		}
		jobs = append(jobs, j)
	}
	return jobs
}

// ParseTimeLeft reads the time left as squeue writes it (%L): [days-][hours:]minutes:seconds,
// UNLIMITED, or what says it is not known (NOT_SET, INVALID), which is no time left.
func ParseTimeLeft(s string) time.Duration {
	d, _ := ParseDuration(s)
	return d
}

// ParseDuration reads a time as squeue writes it (%L, %l): [days-][hours:]minutes:seconds, or
// UNLIMITED (math.MaxInt64). It reports false for any other text, such as NOT_SET and INVALID, and
// then returns zero.
func ParseDuration(s string) (time.Duration, bool) {
	s = strings.TrimSpace(s)
	if s == "UNLIMITED" {
		return math.MaxInt64, true
	}
	var days int64
	if d, rest, ok := strings.Cut(s, "-"); ok {
		n, err := strconv.ParseInt(d, 10, 32)
		if err != nil || n < 0 {
			return 0, false
		}
		days, s = n, rest
	}
	parts := strings.Split(s, ":")
	if len(parts) > 3 || (days > 0 && len(parts) != 3) {
		return 0, false
	}
	var secs int64
	for _, p := range parts {
		n, err := strconv.ParseInt(p, 10, 32)
		if err != nil || n < 0 || len(p) == 0 {
			return 0, false
		}
		secs = secs*60 + n
	}
	return time.Duration(days*86400+secs) * time.Second, true
}

// Cancel implements apptainer.Cluster: Slurm's cancel by name, confirmed by squeue and then sacct.
func (c *Cluster) Cancel(ctx context.Context, name string) error {
	return slurm.CLI{Shell: c.cfg.Shell, Timers: c.cfg.Timers, Poll: c.cfg.Poll}.Cancel(ctx, name)
}

// Node implements apptainer.Cluster.
func (c *Cluster) Node(job string) apptainer.Node { return &node{c: c, job: job} }

// nextPort counts a choice of a port.
func (c *Cluster) nextPort() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.ports++
	return c.ports
}

// lockedBuffer keeps the first 64 KiB written to it.
type lockedBuffer struct {
	mu  sync.Mutex
	buf strings.Builder
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.buf.Len() < 64<<10 {
		b.buf.Write(p)
	}
	return len(p), nil
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

func clip(s string) string {
	s = strings.TrimSpace(s)
	const max = 600
	if len(s) > max {
		return "..." + strings.ToValidUTF8(s[len(s)-max:], "")
	}
	return s
}

// sections splits output by its marker lines, "==vibesys:NAME==", into what each section holds.
func sections(out string, names []string) map[string]string {
	got := map[string]string{}
	var cur string
	var b strings.Builder
	flush := func() {
		if cur != "" {
			got[cur] = b.String()
		}
		b.Reset()
	}
	sc := bufio.NewScanner(strings.NewReader(out))
	sc.Buffer(make([]byte, 0, 64<<10), 16<<20)
	for sc.Scan() {
		line := sc.Text()
		if name, ok := strings.CutPrefix(line, "==vibesys:"); ok && strings.HasSuffix(name, "==") {
			if n := strings.TrimSuffix(name, "=="); slices.Contains(names, n) {
				flush()
				cur = n
				continue
			}
		}
		if cur != "" {
			b.WriteString(line)
			b.WriteByte('\n')
		}
	}
	flush()
	return got
}

// readNodeScript runs each script of its arguments, given as name and script pairs, printing a
// marker line before each one's output.
const readNodeScript = `while [ "$#" -ge 2 ]; do
  printf '==vibesys:%s==\n' "$1"
  /bin/sh -c "$2" 2>/dev/null
  shift 2
done
exit 0`

// readArgs are readNodeScript's arguments of the scripts, by sorted name.
func readArgs(scripts map[string]string) ([]string, []string) {
	names := slices.Sorted(maps.Keys(scripts))
	var args []string
	for _, n := range names {
		args = append(args, n, scripts[n])
	}
	return names, args
}

// exitCode is the exit code of a command's error, or -1 when it did not exit.
func exitCode(err error) int {
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return exit.ExitCode()
	}
	return -1
}
