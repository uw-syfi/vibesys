package lab_test

import (
	"bytes"
	"context"
	"io"
	"os/exec"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/hostfs"
	"github.com/uw-syfi/vibesys/cli/internal/lab"
)

// backstop bounds every wait; no test passes or fails by how long it took.
const backstop = time.Minute

// shellRoom starts each command as a real process of this machine, whose Terminate and Kill reach
// every process the room started, as a container's do: what Bench.Shell exists for.
type shellRoom struct {
	mu    sync.Mutex
	procs []*exec.Cmd
}

type shellProc struct {
	room   *shellRoom
	cmd    *exec.Cmd
	exited chan environment.Exit
}

func (r *shellRoom) Start(argv []string, sink io.Writer) (environment.Process, error) {
	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Stdout, cmd.Stderr = sink, sink
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	r.mu.Lock()
	r.procs = append(r.procs, cmd)
	r.mu.Unlock()
	p := &shellProc{room: r, cmd: cmd, exited: make(chan environment.Exit, 1)}
	go func() {
		err := cmd.Wait()
		code := 0
		if e, ok := err.(*exec.ExitError); ok {
			code = e.ExitCode()
			if code < 0 {
				code = 128 + int(e.Sys().(syscall.WaitStatus).Signal())
			}
		}
		p.exited <- environment.Exit{Code: code}
	}()
	return p, nil
}

func (p *shellProc) Exited() <-chan environment.Exit { return p.exited }

// Terminate reaches every process of the room, as a container's does.
func (p *shellProc) Terminate() { p.room.signalAll(syscall.SIGTERM) }

// Kill ends only the client, as a container's does.
func (p *shellProc) Kill() { _ = p.cmd.Process.Kill() }

func (r *shellRoom) signalAll(sig syscall.Signal) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, c := range r.procs {
		_ = syscall.Kill(-c.Process.Pid, sig)
	}
}

// A lab in a room with a shell signals one process alone: a kill of one detached process leaves
// the other running, and its first line, which says its id, never reaches its output.
func TestALabSignalsOneProcessAloneThroughTheRoomsShell(t *testing.T) {
	if _, err := exec.LookPath("sh"); err != nil {
		t.Skip("no sh on this machine")
	}
	log := &watchedLog{seen: map[string]chan struct{}{}}
	l := lab.New(lab.Config{
		Clock: clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), 0), Log: log,
		Files: hostfs.NewMemory(), Artifacts: "/art", Commit: core.Commit(strings.Repeat("a", 40)), Time: time.Hour,
		Switch: func(context.Context, core.Commit) error { return nil },
	})
	room := &shellRoom{}
	end := make(chan struct{})
	held := make(chan core.ExecOutcome, 1)
	go func() {
		held <- l.Hold(environment.Bench{
			Rooms: map[core.LabRoom]environment.Launcher{core.LabCandidate: room}, Shell: true,
			Limit: make(chan struct{}), Cancelled: end,
			Grace:    func() (<-chan struct{}, func()) { c := make(chan struct{}); close(c); return c, func() {} },
			Collect:  func(context.Context) error { return nil },
			Replaced: func(context.Context) error { return nil },
			Added:    func(context.Context, core.LabPath) error { return nil },
		})
	}()
	ctx, cancel := context.WithTimeout(context.Background(), backstop)
	defer cancel()
	call := func(c core.LabCall) core.LabReply {
		t.Helper()
		var answer core.LabReply
		var out bytes.Buffer
		if err := l.Call(ctx, c, func(r core.LabReply) error {
			if o, ok := r.(core.LabOutput); ok {
				out.Write(o.Data)
			} else {
				answer = r
			}
			return nil
		}); err != nil {
			t.Fatal(err)
		}
		if strings.Contains(out.String(), "vibesys-lab-pid") {
			t.Fatalf("the output %q holds the wrapper's line", out.String())
		}
		return answer
	}
	if got := call(core.LabExec{Room: core.LabCandidate, Argv: []string{"sh", "-c", "echo hi; exit 4"}}); got != (core.LabExit{Code: 4}) {
		t.Fatalf("foreground: %+v", got)
	}
	forever := []string{"sh", "-c", "sleep 100000 & wait"}
	a := call(core.LabExec{Room: core.LabCandidate, Argv: forever, Detach: true}).(core.LabProcess)
	b := call(core.LabExec{Room: core.LabCandidate, Argv: forever, Detach: true}).(core.LabProcess)
	gone := log.await("[lab] " + string(a.Handle) + " exited")
	call(core.LabKillCall{Handle: a.Handle, Signal: core.LabKill})
	select {
	case <-gone:
	case <-ctx.Done():
		t.Fatal("the killed process never ended")
	}
	if s := call(core.LabStatusCall{}).(core.LabStatus); s.Processes[1].End == nil || s.Processes[2].End != nil {
		t.Fatalf("after killing %s: %+v; want it ended and %s running", a.Handle, s.Processes, b.Handle)
	}
	close(end)
	if got := <-held; got.Kind != core.ExecCancelled {
		t.Fatalf("the lab ended as %+v", got)
	}
	l.Close()
	if text := log.String(); strings.Contains(text, "vibesys-lab-pid") {
		t.Fatalf("the log %q holds the wrapper's line", text)
	}
}

// watchedLog is a lab's log that wakes a waiter once a text appears in it.
type watchedLog struct {
	mu   sync.Mutex
	b    bytes.Buffer
	seen map[string]chan struct{}
}

func (w *watchedLog) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.b.Write(p)
	for text, ch := range w.seen {
		if strings.Contains(w.b.String(), text) {
			close(ch)
			delete(w.seen, text)
		}
	}
	return len(p), nil
}

// await returns a channel closed once the text is in the log.
func (w *watchedLog) await(text string) <-chan struct{} {
	w.mu.Lock()
	defer w.mu.Unlock()
	ch := make(chan struct{})
	if strings.Contains(w.b.String(), text) {
		close(ch)
		return ch
	}
	w.seen[text] = ch
	return ch
}

func (w *watchedLog) String() string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.b.String()
}
