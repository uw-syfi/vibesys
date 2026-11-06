package environment

import (
	"bytes"
	"io"
	"strings"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// TailLines is how many of a service's last lines a failure that names them keeps.
const TailLines = 40

// tailBytes bounds what a Tail holds however long its lines are.
const tailBytes = 16 << 10

// Tail keeps the last lines written to it, for a failure that says what a service wrote before it
// ended. It is safe for concurrent use.
type Tail struct {
	mu  sync.Mutex
	buf []byte
}

var _ io.Writer = (*Tail)(nil)

// Write implements io.Writer. It never fails.
func (t *Tail) Write(p []byte) (int, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.buf = append(t.buf, p...)
	if over := len(t.buf) - 2*tailBytes; over > 0 {
		t.buf = append([]byte(nil), t.buf[over:]...)
	}
	return len(p), nil
}

// String returns the last TailLines lines written, at most tailBytes of them.
func (t *Tail) String() string {
	t.mu.Lock()
	b := bytes.TrimRight(t.buf, "\n")
	t.mu.Unlock()
	if len(b) > tailBytes {
		b = b[len(b)-tailBytes:]
	}
	lines := strings.Split(string(b), "\n")
	if len(lines) > TailLines {
		lines = lines[len(lines)-TailLines:]
	}
	return strings.Join(lines, "\n")
}

// Teeing is a launcher whose processes also write to the tail.
func Teeing(l Launcher, t *Tail) Launcher { return teeing{l: l, t: t} }

type teeing struct {
	l Launcher
	t *Tail
}

func (x teeing) Start(argv []string, sink io.Writer) (Process, error) {
	return x.l.Start(argv, io.MultiWriter(sink, x.t))
}

// WithServiceLog adds the service's last lines to an attempt its service ended.
func WithServiceLog(a core.Attempt, t *Tail) core.Attempt {
	if e, ok := a.(core.Ended); ok && (e.Exec.Kind == core.ExecServiceNotReady || e.Exec.Kind == core.ExecServiceExited) {
		e.ServiceLog = t.String()
		return e
	}
	return a
}
