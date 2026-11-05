package clustercli

import (
	"bufio"
	"context"
	"fmt"
	"hash/fnv"
	"io"
	"maps"
	"os/exec"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
)

// node is a running job's node, reached with steps of the job.
type node struct {
	c   *Cluster
	job string
}

// step is the argv that runs argv as a step of the job, on its node, beside its other steps.
func (n *node) step(argv ...string) []string {
	return append([]string{"srun", "--jobid=" + n.job, "--overlap", "--nodes=1", "--ntasks=1", "--quiet"}, argv...)
}

// run runs the script on the node, bounded by d, and returns what it printed.
func (n *node) run(ctx context.Context, d time.Duration, script string, args ...string) (string, error) {
	return n.c.sh(ctx, d, `exec "$@"`, n.step(append([]string{"/bin/sh", "-c", script, "vs-node"}, args...)...)...)
}

// holdScript says it runs, then holds the allocation until its input ends, which is when the caller
// closes it or dies, or until it is told to stop; either way it cancels the allocation, which ends
// every step of it, after removing the job's own directory of the node.
const holdScript = `end() { rm -rf "` + jobDir + `"; scancel "$SLURM_JOB_ID"; exit 0; }
trap end HUP INT TERM
echo vibesys-held
cat >/dev/null
end`

// heldMarker is the line holdScript prints once it runs.
const heldMarker = "vibesys-held"

// Hold implements apptainer.Node.
func (n *node) Hold(ctx context.Context) (io.Closer, error) {
	cmd := n.c.cfg.Shell.Command(context.Background(), n.step("/bin/sh", "-c", holdScript, "vs-hold"))
	in, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	out, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	var errb lockedBuffer
	cmd.Stderr = &errb
	if err := cmd.Start(); err != nil {
		return nil, unavailable("cannot hold the allocation %s: %v", n.job, err)
	}
	h := &hold{n: n, cmd: cmd, in: in, done: make(chan struct{})}
	held := make(chan bool, 1)
	h.wg.Go(func() {
		r := bufio.NewReader(out)
		line, _ := r.ReadString('\n')
		held <- strings.TrimSpace(line) == heldMarker
		_, _ = io.Copy(io.Discard, r)
	})
	h.wg.Go(func() {
		h.waitErr = cmd.Wait()
		close(h.done)
	})
	limit, stop := n.c.cfg.Timers.Timeout(ctx, CallTimeout)
	defer stop()
	select {
	case ok := <-held:
		if ok {
			return h, nil
		}
		_ = h.Close()
		return nil, unavailable("the step that holds the allocation %s did not start: %s", n.job, clip(errb.String()))
	case <-limit:
		_ = h.Close()
		return nil, unavailable("the step that holds the allocation %s did not start within %v", n.job, CallTimeout)
	case <-ctx.Done():
		_ = h.Close()
		return nil, ctx.Err()
	}
}

// hold is the step that holds an allocation.
type hold struct {
	n       *node
	cmd     *exec.Cmd
	in      io.WriteCloser
	done    chan struct{}
	waitErr error
	wg      sync.WaitGroup
	once    sync.Once
}

// Close ends the holder's input, which makes it cancel the allocation, and waits for it, up to a
// bound after which its client is killed: the allocation is then cancelled by the caller's Remove.
func (h *hold) Close() error {
	h.once.Do(func() {
		_ = h.in.Close()
		limit, stop := h.n.c.cfg.Timers.Timeout(context.Background(), CallTimeout)
		select {
		case <-h.done:
		case <-limit:
			if h.cmd.Process != nil {
				_ = h.cmd.Process.Kill()
			}
		}
		stop()
		h.wg.Wait()
	})
	return nil
}

// Read implements apptainer.Node.
func (n *node) Read(ctx context.Context, scripts map[string]string) (map[string]string, error) {
	names, args := readArgs(scripts)
	out, err := n.run(ctx, CallTimeout, readNodeScript, args...)
	if err != nil {
		return nil, fmt.Errorf("reading the node of the job %s: %w", n.job, err)
	}
	got := sections(out, names)
	for _, name := range names {
		if _, ok := got[name]; !ok {
			return nil, unavailable("the node of the job %s printed nothing of the script %q", n.job, name)
		}
	}
	return got, nil
}

// portsScript prints the node's TCP sockets, whose local ports are taken.
const portsScript = `cat /proc/net/tcp /proc/net/tcp6 2>/dev/null; exit 0`

// Port implements apptainer.Node: a port no socket of the node has, chosen among the unprivileged
// ports above the ephemeral range's usual start by the job and how many this process chose, so
// two choices of one run differ.
func (n *node) Port(ctx context.Context) (int, error) {
	out, err := n.run(ctx, CallTimeout, portsScript)
	if err != nil {
		return 0, fmt.Errorf("reading the ports of the node of the job %s: %w", n.job, err)
	}
	return FreePort(TakenPorts(out), n.job+"/"+strconv.Itoa(n.c.nextPort()))
}

// The ports FreePort chooses among.
const (
	firstPort = 20000
	lastPort  = 32767
)

// TakenPorts reads the local ports of /proc/net/tcp's and tcp6's lines.
func TakenPorts(out string) map[int]bool {
	taken := map[int]bool{}
	for _, line := range strings.Split(out, "\n") {
		f := strings.Fields(line)
		if len(f) < 2 || !strings.HasSuffix(f[0], ":") {
			continue
		}
		i := strings.LastIndexByte(f[1], ':')
		if i < 0 {
			continue
		}
		if p, err := strconv.ParseUint(f[1][i+1:], 16, 16); err == nil {
			taken[int(p)] = true
		}
	}
	return taken
}

// FreePort returns the first port from the seed's place in the range that is not taken.
func FreePort(taken map[int]bool, seed string) (int, error) {
	h := fnv.New32a()
	_, _ = h.Write([]byte(seed))
	span := lastPort - firstPort + 1
	start := int(h.Sum32() % uint32(span))
	for i := range span {
		if p := firstPort + (start+i)%span; !taken[p] {
			return p, nil
		}
	}
	return 0, unavailable("every port from %d to %d of the node is taken", firstPort, lastPort)
}

// startMarker is what a box's process writes to its standard error once it runs in the container,
// before the command: a step that did not write it never started the command.
const startMarker = "vibesys-started"

// stepScript starts a box's process in the step: it makes the box's home, passes the GPUs Slurm
// assigned the job when the box gets the GPUs ($2 is 1), and runs the apptainer program ($3) with
// the rest, with the home first. The home is under the job's own directory of the node, which the
// holder removes when the allocation ends.
var stepScript = `h="` + jobDir + `/home-$1"
mkdir -p "$h" || exit 255
if [ "$2" = 1 ]; then
  for v in ` + strings.Join(apptainer.AssignedGPUVariables, " ") + `; do
    eval "x=\${$v-}"
    if [ -n "$x" ]; then export "APPTAINERENV_$v=$x"; fi
  done
fi
p="$3"; shift 3
exec "$p" exec --home "$h:/tmp" "$@"`

// jobDir is the job's own directory on its node, in a step's shell.
const jobDir = `${TMPDIR:-/tmp}/vibesys-job-$SLURM_JOB_ID`

// wrapper runs the command in the container, after saying so, with its standard error merged into
// its standard output.
const wrapper = `printf '%s\n' ` + startMarker + ` >&2; exec "$@" 2>&1`

// StepArgv is what runs argv in a container of the box, as a step: env sets the box's variables
// for the container (APPTAINERENV_), stepScript makes its home and passes the GPUs, and apptainer
// exec runs the wrapper in the image with a clean environment, the box's mounts, no other mount of
// the node's but its home at /tmp, and the box's directory as its working directory. The site's
// bind paths are not mounted (--no-mount bind-paths): a site binds its shared file system into
// every container, read-write, which would let a container write the images, a sandbox's tree
// among them, and every other run's directories. Of the bind paths apptainer itself sets, the
// node's /etc/hosts is bound back, read-only, since an image's own is empty and a server and its
// client find each other at localhost. The image itself is read-only, a sandbox too, since
// apptainer mounts one read-only without --writable.
func (n *node) StepArgv(box apptainer.Box, argv []string) ([]string, error) {
	if box.Name == "" || strings.ContainsAny(box.Name, "/\n\x00 ") {
		return nil, invalid("%q is not a box name", box.Name)
	}
	if err := checkPath(box.Image); err != nil {
		return nil, err
	}
	env := []string{"env"}
	vars := maps.Clone(box.Env)
	if vars == nil {
		vars = map[string]string{}
	}
	vars[apptainer.EnvBox] = box.Name
	for _, k := range slices.Sorted(maps.Keys(vars)) {
		env = append(env, "APPTAINERENV_"+k+"="+vars[k])
	}
	gpus := "0"
	if box.ROCm {
		gpus = "1"
	}
	args := []string{"/bin/sh", "-c", stepScript, "vs-step", box.Name, gpus, n.c.cfg.Program, "--cleanenv", "--no-mount", "tmp,cwd,bind-paths", "--bind", "/etc/hosts:/etc/hosts:ro"}
	if box.Dir != "" {
		args = append(args, "--pwd", box.Dir)
	}
	for _, m := range box.Mounts {
		if strings.ContainsAny(m.Host+m.Container, ",:") {
			return nil, invalid("the mount %s of %s has a comma or a colon, which apptainer's --bind cannot hold", m.Host, m.Container)
		}
		bind := m.Host + ":" + m.Container
		if m.ReadOnly {
			bind += ":ro"
		}
		args = append(args, "--bind", bind)
	}
	if box.ROCm {
		args = append(args, "--rocm")
	}
	args = append(args, box.Image, "/bin/sh", "-c", wrapper, "vs-exec")
	return n.step(append(append(env, args...), argv...)...), nil
}

// Start implements apptainer.Node.
func (n *node) Start(box apptainer.Box, argv []string, sink io.Writer) (environment.Process, error) {
	full, err := n.StepArgv(box, argv)
	if err != nil {
		return nil, err
	}
	cmd := n.c.cfg.Shell.Command(context.Background(), full)
	var stderr lockedBuffer
	cmd.Stdout, cmd.Stderr = sink, &stderr
	if err := cmd.Start(); err != nil {
		return nil, unavailable("cannot reach %s to start a step: %v", n.c.cfg.Shell.Where(), err)
	}
	p := &process{n: n, box: box.Name, cmd: cmd, exited: make(chan environment.Exit, 1)}
	p.wg.Go(func() { p.exited <- n.finish(box.Name, cmd.Wait(), stderr.String()) })
	return p, nil
}

// finish says how a box's process ended, from how its client did.
func (n *node) finish(box string, waitErr error, stderr string) environment.Exit {
	code := 0
	if waitErr != nil {
		code = exitCode(waitErr)
	}
	switch {
	case code < 0:
		// The client was killed, or never ran: what its command went on to do is not known.
		return docker.Lost(fmt.Sprintf("the client following a command of %s ended: %v", box, waitErr))
	case n.c.cfg.Shell.Lost(code):
		return docker.Lost(fmt.Sprintf("the connection to %s was lost while it ran a command of %s: %s", n.c.cfg.Shell.Where(), box, clip(stderr)))
	case !started(stderr):
		// srun or apptainer failed before the command: their exit code is not the command's.
		return docker.Lost(fmt.Sprintf("the step of %s in the job %s did not start its command (exit %d): %s", box, n.job, code, clip(stderr)))
	}
	return environment.Exit{Code: code}
}

// started reports whether the wrapper's marker is a line of stderr.
func started(stderr string) bool {
	return slices.Contains(strings.Split(stderr, "\n"), startMarker)
}

// signalScript sends the signal $2 to every process of the node whose environment names the box
// $1, read with one grep over every environment whose entries end with NUL. Another user's process
// cannot be read, which is not an error. After KILL it waits, a second at a time, for none of them
// to be left, and exits 3 when some still are after killWait seconds: a process that has ended
// and is not yet reaped has an empty environment.
const signalScript = `match() { grep -lxz -- "` + apptainer.EnvBox + `=$1" /proc/[0-9]*/environ 2>/dev/null; }
for f in $(match "$1"); do
  p=${f#/proc/}
  kill -s "$2" "${p%/environ}" 2>/dev/null
done
[ "$2" = KILL ] || exit 0
i=0
while [ -n "$(match "$1")" ]; do
  i=$((i + 1))
  if [ "$i" -gt ` + killWait + ` ]; then echo "processes of the box $1 are left after SIGKILL" >&2; exit 3; fi
  sleep 1
done
exit 0`

// killWait is how many seconds signalScript waits for the processes it killed to end, well within
// CallTimeout.
const killWait = "60"

// Signal implements apptainer.Node.
func (n *node) Signal(ctx context.Context, box string, sig apptainer.Signal) error {
	if sig != apptainer.SIGTERM && sig != apptainer.SIGKILL {
		return invalid("%q is not a signal a box is sent", sig)
	}
	if _, err := n.run(ctx, CallTimeout, signalScript, box, string(sig)); err != nil {
		return fmt.Errorf("signalling %s on the node of the job %s: %w", box, n.job, err)
	}
	return nil
}

// process is a box's process: the client of its step.
type process struct {
	n      *node
	box    string
	cmd    *exec.Cmd
	exited chan environment.Exit
	wg     sync.WaitGroup
}

// Exited implements environment.Process.
func (p *process) Exited() <-chan environment.Exit { return p.exited }

// Terminate implements environment.Process: SIGTERM to every process of the box.
func (p *process) Terminate() {
	ctx, stop := clock.Deadline(context.Background(), p.n.c.cfg.Timers, CallTimeout)
	defer stop()
	_ = p.n.Signal(ctx, p.box, apptainer.SIGTERM)
}

// Kill implements environment.Process: it ends the step's client at once. The command ends no
// later than its box is killed or its allocation ends.
func (p *process) Kill() {
	if p.cmd.Process != nil {
		_ = p.cmd.Process.Kill()
	}
}
