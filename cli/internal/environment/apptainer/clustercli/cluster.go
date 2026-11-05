// Package clustercli is the apptainer.Cluster of a real Slurm cluster, reached through a shell on
// its login node (ssh, in production): files are written on the file system the login node shares
// with the compute nodes, allocations are asked for with the site's submit command, and everything
// that runs on a node is a step of the allocation (srun --overlap) started from the login node.
//
// What it relies on of the cluster, each checked on one: sbatch --parsable prints the job's id; an
// srun step started with --jobid and --overlap from the login node runs on the job's node beside
// its other steps and propagates its command's exit code; a step reads the standard input srun
// was given; apptainer exec passes APPTAINERENV_ variables into a clean environment, binds read-only
// with :ro, and gives the node's AMD GPUs with --rocm; and apptainer build converts an uncompressed
// docker archive.
package clustercli

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"maps"
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
	// ConvertTimeout bounds the conversion of an image to a SIF on a node.
	ConvertTimeout = 30 * time.Minute
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

// sh runs the script with /bin/sh on the login node, bounded by d, with args as $1 and on.
func (c *Cluster) sh(ctx context.Context, d time.Duration, script string, args ...string) (string, error) {
	bounded, done := remote.Bounded(ctx, c.cfg.Timers, d)
	defer done()
	out, err := remote.Output(bounded, c.cfg.Shell, append([]string{"/bin/sh", "-c", script, "vs-apptainer"}, args...), nil)
	if err != nil && ctx.Err() == nil && bounded.Err() != nil {
		return "", unavailable("%s did not answer within %v", c.cfg.Shell.Where(), d)
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
		return unavailable("copying to %s on %s failed: %v %s", p, c.cfg.Shell.Where(), err, clip(errb.String()))
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

// Submit implements apptainer.Cluster. The job holds its node with a sleep until it is cancelled
// or its time limit; Slurm writes no file of it.
func (c *Cluster) Submit(ctx context.Context, a apptainer.Allocation) (string, error) {
	argv := append(slices.Clone(c.cfg.Submit), SubmitArgs(a)...)
	bounded, done := remote.Bounded(ctx, c.cfg.Timers, CallTimeout)
	defer done()
	out, err := remote.Output(bounded, c.cfg.Shell, argv, nil)
	if err != nil {
		return "", fmt.Errorf("submitting the allocation %s: %w", a.Name, err)
	}
	return ParseJobID(out)
}

// SubmitArgs are the submit command's arguments of an allocation.
func SubmitArgs(a apptainer.Allocation) []string {
	minutes := int64((a.Limit + time.Minute - 1) / time.Minute)
	args := []string{
		"--parsable", "--job-name=" + a.Name, "--comment=" + a.Comment, "--no-requeue",
		"--time=" + strconv.FormatInt(minutes, 10), "--nodes=1", "--ntasks=1",
	}
	args = append(args, a.Options...)
	return append(args, "--output=/dev/null", "--wrap=exec sleep infinity")
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

// stateScript prints squeue's state and reason of job $1, its exit status, and sacct's states.
const stateScript = `q=$(squeue -h -j "$1" -o '%T|%r' 2>&1); r=$?
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

// ParseState reads stateScript's output. A job squeue lists is queued or running as it says, and
// any other state of it is ended; one it does not know is what sacct says, and ended when sacct
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
		name, reason, _ := strings.Cut(strings.Split(squeue, "\n")[0], "|")
		return apptainer.JobState{Phase: phaseOf(name), Name: name, Reason: strings.TrimSpace(reason)}, nil
	}
	for _, line := range strings.Split(sacct, "\n") {
		if f := strings.Fields(line); len(f) > 0 {
			// sacct writes "CANCELLED by 1002"; the first word is the state.
			name := strings.TrimRight(f[0], "+")
			return apptainer.JobState{Phase: phaseOf(name), Name: name}, nil
		}
	}
	return apptainer.JobState{Phase: apptainer.Ended}, nil
}

func phaseOf(state string) apptainer.Phase {
	switch strings.ToUpper(strings.TrimSpace(state)) {
	case "PENDING", "CONFIGURING", "REQUEUED", "RESIZING":
		return apptainer.Queued
	case "RUNNING":
		return apptainer.Running
	}
	return apptainer.Ended
}

// Jobs implements apptainer.Cluster.
func (c *Cluster) Jobs(ctx context.Context, scope string) ([]string, error) {
	out, err := c.sh(ctx, CallTimeout, `squeue -h -u `+user+` -o '%j|%k'`)
	if err != nil {
		return nil, fmt.Errorf("listing the Slurm jobs: %w", err)
	}
	return ParseJobs(out, scope), nil
}

// ParseJobs reads squeue's "name|comment" lines and returns the names of this package's jobs whose
// comment is the scope.
func ParseJobs(out, scope string) []string {
	var names []string
	for _, line := range strings.Split(out, "\n") {
		name, comment, ok := strings.Cut(strings.TrimSpace(line), "|")
		if ok && strings.HasPrefix(name, apptainer.JobPrefix) && strings.TrimSpace(comment) == scope {
			names = append(names, name)
		}
	}
	return names
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
