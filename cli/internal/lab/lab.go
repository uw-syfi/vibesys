// Package lab is a running lab as its supervisor holds it: the containers its environment handed
// over (environment.Bench), the processes it started in them, and the calls that reach it.
//
// A Lab is made parked, when its job's supervisor starts; Hold is what the environment calls once
// the containers are up, and it returns when the lab ends; Close says it ended. Calls (Call) wait
// until it is ready, and are refused once it ends. Nothing a lab runs is a result: it reports
// output, exits, files and its state, and never a measurement.
package lab

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"maps"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/hostfs"
	"github.com/uw-syfi/vibesys/cli/internal/owned"
)

// Chunk is the most bytes one reply of a pull carries.
const Chunk = 512 << 10

// Config is what a Lab needs. Every field is required.
type Config struct {
	Clock clock.Clock
	// Log is the job's output. Detached processes write there, each line under its handle, and the
	// lab says there what each call changed.
	Log io.Writer
	// Files is the file system of the daemon's machine, where Artifacts is.
	Files hostfs.FS
	// Artifacts is the lab's artifacts directory on the daemon's machine.
	Artifacts string
	// Commit is the commit the checkout is at when the lab starts.
	Commit core.Commit
	// Time is how long the lab lasts once it is ready.
	Time time.Duration
	// Switch replaces the checkout, in place, with a clean checkout of the commit.
	Switch func(ctx context.Context, commit core.Commit) error
}

// Lab is one lab. Create it with New. All its methods are safe for concurrent use.
type Lab struct {
	cfg   Config
	group owned.Group
	log   *lockedWriter

	ready    chan struct{} // closed when it is ready, or ended without being ready
	endAsked chan struct{} // closed by the first LabEndCall
	endOnce  sync.Once
	starting sync.WaitGroup // processes being started, which an ending waits for

	mu     sync.Mutex
	phase  core.LabPhase
	bench  environment.Bench
	until  time.Time
	commit core.Commit
	procs  []*process
	// busy is set while the checkout is replaced, which no process may start during.
	busy bool
}

// process is a process the lab started.
type process struct {
	handle    core.LabHandle
	room      core.LabRoom
	argv      []string
	startedAt time.Time
	p         environment.Process
	done      chan struct{}
	end       *core.LabExit // set once, before done closes
	// out flushes what is left of a detached process's last line.
	out *prefixWriter
	// pid is the process's id in its room, once its wrapper said it (Bench.Shell).
	pid *pidSniffer
}

// New returns a lab that is starting.
func New(cfg Config) *Lab {
	return &Lab{
		cfg: cfg, log: &lockedWriter{w: cfg.Log}, ready: make(chan struct{}), endAsked: make(chan struct{}),
		phase: core.LabStarting, commit: cfg.Commit,
	}
}

// Hold implements environment.HoldLab: the lab is ready until its time is up, it is asked to end,
// or the bench is cancelled. Then every process is asked to terminate, given the bench's grace, and
// killed, and Hold returns once every one of them has ended.
func (l *Lab) Hold(b environment.Bench) core.ExecOutcome {
	l.mu.Lock()
	if l.phase != core.LabStarting {
		l.mu.Unlock()
		return core.ExecOutcome{Kind: core.ExecCancelled}
	}
	l.bench, l.phase, l.until = b, core.LabReady, l.cfg.Clock.Now().Add(l.cfg.Time)
	status := l.statusLocked()
	l.mu.Unlock()
	close(l.ready)
	l.logf("ready: %s, port %d, until %s", roomList(status.Rooms), status.Port, status.Until.UTC().Format(time.RFC3339))

	var out core.ExecOutcome
	select {
	case <-b.Limit:
		l.logf("its time is up")
		out = core.ExecOutcome{Kind: core.ExecExited}
	case <-l.endAsked:
		l.logf("ended")
		out = core.ExecOutcome{Kind: core.ExecExited}
	case <-b.Cancelled:
		l.logf("cancelled")
		out = core.ExecOutcome{Kind: core.ExecCancelled}
	}
	l.mu.Lock()
	l.phase = core.LabEnded
	l.mu.Unlock()
	l.starting.Wait()
	l.stopAll(b)
	l.group.Wait()
	return out
}

// Close says the lab ended, whether or not it was ever ready, and waits for what it started. Its
// supervisor calls it once the environment returned.
func (l *Lab) Close() {
	l.mu.Lock()
	wasStarting := l.phase == core.LabStarting
	l.phase = core.LabEnded
	l.mu.Unlock()
	if wasStarting {
		close(l.ready)
	}
	l.group.Wait()
}

// stopAll asks every process that runs to terminate, waits for them for the grace, and kills
// what is left.
func (l *Lab) stopAll(b environment.Bench) {
	l.mu.Lock()
	procs := slices.Clone(l.procs)
	l.mu.Unlock()
	var running []*process
	for _, p := range procs {
		select {
		case <-p.done:
		default:
			running = append(running, p)
		}
	}
	if len(running) == 0 {
		return
	}
	for _, p := range running {
		p.p.Terminate()
	}
	if b.Grace != nil {
		fired, stop := b.Grace()
	wait:
		for _, p := range running {
			select {
			case <-p.done:
			case <-fired:
				break wait
			}
		}
		stop()
	}
	for _, p := range running {
		p.p.Kill()
	}
	for _, p := range running {
		<-p.done
	}
}

// Call makes one call on the lab and passes its replies to emit, the answer last. It waits while
// the lab is starting. It fails with core.FailureConflict once the lab ended, and with what the
// call ran into otherwise; a failure of emit stops it.
func (l *Lab) Call(ctx context.Context, call core.LabCall, emit func(core.LabReply) error) error {
	if status, ok := call.(core.LabStatusCall); ok {
		if status.Wait {
			select {
			case <-l.ready:
			case <-ctx.Done():
				return ctx.Err()
			}
		}
		return emit(l.Status())
	}
	if _, end := call.(core.LabEndCall); end {
		l.endOnce.Do(func() { close(l.endAsked) })
		return emit(l.Status())
	}
	select {
	case <-l.ready:
	case <-ctx.Done():
		return ctx.Err()
	}
	switch c := call.(type) {
	case core.LabExec:
		return l.exec(ctx, c, emit)
	case core.LabKillCall:
		return l.kill(c, emit)
	case core.LabCheckoutCall:
		return l.checkout(ctx, c, emit)
	case core.LabPull:
		return l.pull(ctx, c, emit)
	case core.LabPush:
		return l.push(ctx, c, emit)
	case core.LabStatusCall, core.LabEndCall:
	}
	return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("a lab takes no %T", call)}
}

// Status is where the lab is now.
func (l *Lab) Status() core.LabStatus {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.statusLocked()
}

func (l *Lab) statusLocked() core.LabStatus {
	s := core.LabStatus{Phase: l.phase, Commit: l.commit, Until: l.until, Port: l.bench.Port, Artifacts: l.bench.Artifacts}
	s.Rooms = slices.Sorted(maps.Keys(l.bench.Rooms))
	for _, p := range l.procs {
		s.Processes = append(s.Processes, p.state())
	}
	return s
}

func (p *process) state() core.LabProcess {
	out := core.LabProcess{Handle: p.handle, Room: p.room, Argv: slices.Clone(p.argv), StartedAt: p.startedAt}
	select {
	case <-p.done:
		end := *p.end
		out.End = &end
	default:
	}
	return out
}

// ended is the failure of a call on a lab that ended.
func ended() error {
	return &core.Failure{Kind: core.FailureConflict, Message: "the lab has ended; start another one with lab start"}
}

// start starts a process in a room and keeps it, with what follows its end.
func (l *Lab) start(c core.LabExec, sink io.Writer) (*process, error) {
	l.mu.Lock()
	if l.phase != core.LabReady {
		l.mu.Unlock()
		return nil, ended()
	}
	if l.busy {
		l.mu.Unlock()
		return nil, &core.Failure{Kind: core.FailureConflict, Message: "the lab's checkout is being replaced; run it once lab checkout has answered"}
	}
	room, ok := l.bench.Rooms[c.Room]
	if !ok {
		rooms := roomList(slices.Sorted(maps.Keys(l.bench.Rooms)))
		l.mu.Unlock()
		return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
			"this lab has no %s container (it has %s): a task of one image has only the candidate's", c.Room, rooms)}
	}
	p := &process{
		handle: core.LabHandleOf(len(l.procs) + 1), room: c.Room, argv: slices.Clone(c.Argv),
		startedAt: l.cfg.Clock.Now(), done: make(chan struct{}),
	}
	l.procs = append(l.procs, p)
	l.starting.Add(1)
	l.mu.Unlock()
	defer l.starting.Done()

	if c.Detach {
		p.out = &prefixWriter{w: l.log, prefix: "[" + string(p.handle) + "] "}
		sink = p.out
	}
	argv := withEnv(c.Argv, c.Env)
	if l.bench.Shell {
		p.pid = &pidSniffer{w: sink}
		sink, argv = p.pid, append([]string{"/bin/sh", "-c", pidWrapper, "vs-lab"}, argv...)
	}
	proc, err := room.Start(argv, sink)
	if err != nil {
		p.end = &core.LabExit{Lost: fmt.Sprintf("it did not start: %v", err)}
		close(p.done)
		return nil, err
	}
	p.p = proc
	l.group.Go(func() {
		st := <-proc.Exited()
		end := &core.LabExit{Code: st.Code}
		switch {
		case st.Err != nil:
			end = &core.LabExit{Lost: st.Err.Error()}
		case st.Kind != "" && st.Kind != core.ExecExited:
			end = &core.LabExit{Lost: fmt.Sprintf("it ended as %s", st.Kind)}
		}
		if p.out != nil {
			p.out.flush()
		}
		p.end = end
		close(p.done)
		l.logf("%s %s", p.handle, describeExit(*end))
	})
	return p, nil
}

// withEnv puts the variables in front of the command, through env, so every room sets them the
// same way.
func withEnv(argv []string, vars map[string]string) []string {
	if len(vars) == 0 {
		return slices.Clone(argv)
	}
	out := []string{"env"}
	for _, k := range slices.Sorted(maps.Keys(vars)) {
		out = append(out, k+"="+vars[k])
	}
	return append(out, argv...)
}

func (l *Lab) exec(ctx context.Context, c core.LabExec, emit func(core.LabReply) error) error {
	if c.Detach {
		p, err := l.start(c, nil)
		if err != nil {
			return err
		}
		l.logf("%s started in %s, detached: %s", p.handle, p.room, strings.Join(p.argv, " "))
		return emit(p.state())
	}
	out := &emitter{emit: emit}
	p, err := l.start(c, out)
	if err != nil {
		return err
	}
	l.logf("%s started in %s: %s", p.handle, p.room, strings.Join(p.argv, " "))
	select {
	case <-p.done:
	case <-ctx.Done():
		// The caller went away: its command is ended, as a shell's is when its terminal closes.
		l.signal(p, core.LabTerm)
		l.mu.Lock()
		grace := l.bench.Grace
		l.mu.Unlock()
		if grace != nil {
			fired, stop := grace()
			select {
			case <-p.done:
			case <-fired:
			}
			stop()
		}
		l.signal(p, core.LabKill)
		p.p.Kill()
		<-p.done
		out.close()
		return ctx.Err()
	}
	if err := out.close(); err != nil {
		return err
	}
	return emit(*p.end)
}

func (l *Lab) kill(c core.LabKillCall, emit func(core.LabReply) error) error {
	l.mu.Lock()
	var p *process
	for _, have := range l.procs {
		if have.handle == c.Handle {
			p = have
		}
	}
	phase := l.phase
	l.mu.Unlock()
	switch {
	case p == nil:
		return &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("the lab started no process %s; lab ps lists them", c.Handle)}
	case phase != core.LabReady:
		return ended()
	case p.p == nil:
		return emit(p.state())
	}
	select {
	case <-p.done:
	default:
		l.signal(p, c.Signal)
		l.logf("%s sent %s", p.handle, c.Signal)
	}
	return emit(p.state())
}

// signal sends the signal to one process of the lab. In a room with a shell (Bench.Shell) it goes
// to that process alone, and a kill to what it started too, through a command run beside it; a
// process whose id is not known yet, or a room without a shell, gets its own Terminate or Kill.
func (l *Lab) signal(p *process, sig core.LabSignal) {
	l.mu.Lock()
	room, shell := l.bench.Rooms[p.room], l.bench.Shell
	l.mu.Unlock()
	if pid, ok := p.pid.get(); shell && ok && room != nil {
		tree := "0"
		if sig == core.LabKill {
			tree = "1"
		}
		name := map[core.LabSignal]string{core.LabTerm: "TERM", core.LabKill: "KILL"}[sig]
		if k, err := room.Start([]string{"/bin/sh", "-c", signalScript, "vs-lab-signal", name, strconv.Itoa(pid), tree}, io.Discard); err == nil {
			select {
			case <-k.Exited():
				return
			case <-p.done:
				return
			}
		}
	}
	if sig == core.LabKill {
		p.p.Kill()
		return
	}
	p.p.Terminate()
}

func (l *Lab) checkout(ctx context.Context, c core.LabCheckoutCall, emit func(core.LabReply) error) error {
	l.mu.Lock()
	var running []string
	for _, p := range l.procs {
		select {
		case <-p.done:
		default:
			running = append(running, string(p.handle))
		}
	}
	switch {
	case l.phase != core.LabReady:
		l.mu.Unlock()
		return ended()
	case l.busy:
		l.mu.Unlock()
		return &core.Failure{Kind: core.FailureConflict, Message: "the lab's checkout is already being replaced"}
	case len(running) > 0:
		l.mu.Unlock()
		return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf(
			"%s still run, and may read the checkout; end them with lab kill first", strings.Join(running, ", "))}
	}
	l.busy = true
	replaced := l.bench.Replaced
	l.mu.Unlock()
	err := l.cfg.Switch(ctx, c.Commit)
	if err == nil {
		err = replaced(ctx)
	}
	l.mu.Lock()
	l.busy = false
	if err == nil {
		l.commit = c.Commit
	}
	status := l.statusLocked()
	l.mu.Unlock()
	if err != nil {
		l.logf("replacing the checkout with %s failed: %v", c.Commit, err)
		return err
	}
	l.logf("the checkout is at %s", c.Commit)
	return emit(status)
}

func (l *Lab) pull(ctx context.Context, c core.LabPull, emit func(core.LabReply) error) error {
	l.mu.Lock()
	collect, phase := l.bench.Collect, l.phase
	l.mu.Unlock()
	if phase != core.LabReady {
		return ended()
	}
	if err := collect(ctx); err != nil {
		return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("bringing the artifacts back: %v", err)}
	}
	at := c.Path.Join(l.cfg.Artifacts)
	files, err := l.cfg.Files.Files(at)
	if errors.Is(err, fs.ErrNotExist) {
		return &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("the lab's artifacts directory has nothing at %q", string(c.Path))}
	}
	if err != nil {
		return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("reading the artifacts: %v", err)}
	}
	var sent uint64
	buf := make([]byte, Chunk)
	for _, f := range files {
		from := at
		if f.Path != "" {
			from = at + "/" + f.Path
		}
		var off int64
		for {
			if err := ctx.Err(); err != nil {
				return err
			}
			n, err := l.cfg.Files.ReadAt(from, buf, off)
			// A piece at offset 0 says the file is there, even when it is empty.
			if n > 0 || off == 0 {
				if err := emit(core.LabFile{Path: f.Path, Offset: uint64(off), Data: slices.Clone(buf[:n]), Executable: f.Executable}); err != nil {
					return err
				}
			}
			off += int64(n)
			sent += uint64(n)
			if errors.Is(err, io.EOF) || (err == nil && n == 0) {
				break
			}
			if err != nil {
				return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("reading %s: %v", from, err)}
			}
		}
	}
	return emit(core.LabPulled{Files: len(files), Bytes: sent})
}

func (l *Lab) push(ctx context.Context, c core.LabPush, emit func(core.LabReply) error) error {
	l.mu.Lock()
	added, phase := l.bench.Added, l.phase
	l.mu.Unlock()
	if phase != core.LabReady {
		return ended()
	}
	to := c.Path.Join(l.cfg.Artifacts)
	if err := l.cfg.Files.WriteAt(ctx, to, c.Data, int64(c.Offset), c.Executable); err != nil {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("writing %s: %v", c.Path, err)}
	}
	size := c.Offset + uint64(len(c.Data))
	if c.Last {
		if err := added(ctx, c.Path); err != nil {
			return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("copying %s to the lab: %v", c.Path, err)}
		}
		l.logf("took in %s (%d bytes)", c.Path, size)
	}
	return emit(core.LabPushed{Size: size})
}

// logf says a line in the lab's log.
func (l *Lab) logf(format string, args ...any) {
	_, _ = fmt.Fprintf(l.log, "[lab] "+format+"\n", args...)
}

func describeExit(e core.LabExit) string {
	if e.Lost != "" {
		return "ended, and how is not known: " + e.Lost
	}
	return fmt.Sprintf("exited %d", e.Code)
}

func roomList(rooms []core.LabRoom) string {
	names := make([]string, len(rooms))
	for i, r := range rooms {
		names[i] = string(r)
	}
	return strings.Join(names, " and ")
}

// emitter passes a foreground process's output to the caller, one write at a time, and keeps the
// first failure, after which it drops what comes: the process is not held up by a caller that left.
type emitter struct {
	mu     sync.Mutex
	emit   func(core.LabReply) error
	failed error
	closed bool
}

func (e *emitter) Write(p []byte) (int, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.failed == nil && !e.closed {
		e.failed = e.emit(core.LabOutput{Data: slices.Clone(p)})
	}
	return len(p), nil
}

// close drops what is written from now on, since the call has answered, and returns the first
// failure to pass output on.
func (e *emitter) close() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.closed = true
	return e.failed
}

// pidWrapper is how a command starts in a room with a shell: it says its process id on the first
// line of its output, which the lab reads and keeps out of the output, and becomes the command.
const pidWrapper = `printf '` + pidLine + `%s\n' "$$"; exec "$@"`

// pidLine starts the line pidWrapper says its process id on.
const pidLine = "vibesys-lab-pid "

// signalScript sends the signal $1 to the process $2 and, when $3 is 1, to every process it started,
// deepest first, as /proc lists them. A process that is gone already is no error.
const signalScript = `sig=$1; pid=$2; tree=$3
below() { for t in /proc/$1/task/*; do for c in $(cat "$t/children" 2>/dev/null); do below "$c"; echo "$c"; done; done; }
if [ "$tree" = 1 ]; then kill -s "$sig" $(below "$pid") "$pid" 2>/dev/null; else kill -s "$sig" "$pid" 2>/dev/null; fi
exit 0`

// pidSniffer passes a process's output on, less its first line when that line says the process's
// id (pidWrapper), which it keeps.
type pidSniffer struct {
	mu    sync.Mutex
	w     io.Writer
	head  []byte
	done  bool
	pid   int
	found bool
}

func (s *pidSniffer) Write(b []byte) (int, error) {
	s.mu.Lock()
	if s.done {
		s.mu.Unlock()
		return s.pass(b)
	}
	s.head = append(s.head, b...)
	i := bytes.IndexByte(s.head, '\n')
	if i < 0 && len(s.head) < 64 {
		s.mu.Unlock()
		return len(b), nil
	}
	s.done = true
	rest := s.head
	if i >= 0 {
		if pid, err := strconv.Atoi(string(bytes.TrimPrefix(s.head[:i], []byte(pidLine)))); err == nil && bytes.HasPrefix(s.head, []byte(pidLine)) {
			s.pid, s.found, rest = pid, true, s.head[i+1:]
		}
	}
	s.head = nil
	s.mu.Unlock()
	if len(rest) > 0 {
		if _, err := s.pass(rest); err != nil {
			return 0, err
		}
	}
	return len(b), nil
}

func (s *pidSniffer) pass(b []byte) (int, error) {
	if s.w == nil {
		return len(b), nil
	}
	return s.w.Write(b)
}

// get returns the process's id, once its first line said it.
func (s *pidSniffer) get() (int, bool) {
	if s == nil {
		return 0, false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.pid, s.found
}

// lockedWriter serializes the writes of the lab and its detached processes to the log.
type lockedWriter struct {
	mu sync.Mutex
	w  io.Writer
}

func (w *lockedWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.w.Write(p)
}

// prefixWriter writes whole lines to w, each under the prefix, and keeps a line that has not ended
// until it does or flush is called.
type prefixWriter struct {
	mu     sync.Mutex
	w      io.Writer
	prefix string
	line   []byte
}

func (p *prefixWriter) Write(b []byte) (int, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.line = append(p.line, b...)
	for {
		i := bytes.IndexByte(p.line, '\n')
		if i < 0 {
			break
		}
		_, _ = p.w.Write(append([]byte(p.prefix), p.line[:i+1]...))
		p.line = p.line[i+1:]
	}
	return len(b), nil
}

func (p *prefixWriter) flush() {
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(p.line) > 0 {
		_, _ = p.w.Write(append(append([]byte(p.prefix), p.line...), '\n'))
		p.line = nil
	}
}
