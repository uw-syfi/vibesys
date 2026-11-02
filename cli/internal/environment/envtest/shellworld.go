//go:build unix

package envtest

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
)

// releaseLines is how many processes one Release can let go on: each waiting process takes one
// line from the point's pipe.
const releaseLines = 256

// ShellWorld is a World for environments that run a POSIX shell command on this machine's
// kernel: the host, or a container that mounts Dir() at the same path. It renders a Program to a
// `sh -c` script made of shell built-ins, and it signals through named pipes in Dir(), so every
// wait is on the process that produces the event.
//
// Each point is two pipes and a file: a pipe a Mark wakes the test on, a pipe a Hold waits on,
// and a file that says the point was marked. The test holds every pipe open for as long as the
// world lives, so a line written before anyone reads it is kept. A process is observable
// through a pipe it holds open for writing: the test sees it end as the end of the file.
type ShellWorld struct {
	dir string

	mu      sync.Mutex
	files   map[string]*os.File // by pipe path
	gone    map[string]bool
	closing bool
	wg      sync.WaitGroup
}

var _ World = (*ShellWorld)(nil)

// NewShellWorld returns a world in a fresh directory that the test removes when it ends. The
// goroutines that wait for the test's sake are joined then too.
func NewShellWorld(t testing.TB) *ShellWorld {
	t.Helper()
	// The directory is under the system temporary directory, with a short name, so that a path
	// into it stays short enough for every tool that takes one.
	dir, err := os.MkdirTemp("", "vsw")
	if err != nil {
		t.Fatalf("making the world's directory: %v", err)
	}
	if real, err := filepath.EvalSymlinks(dir); err == nil {
		dir = real
	}
	w := &ShellWorld{dir: dir, files: map[string]*os.File{}, gone: map[string]bool{}}
	t.Cleanup(w.close)
	return w
}

// Dir is the directory the world's pipes and files are in. A container that runs programs of
// this world mounts it at this path.
func (w *ShellWorld) Dir() string { return w.dir }

func (w *ShellWorld) close() {
	w.mu.Lock()
	w.closing = true
	for _, f := range w.files {
		f.Close()
	}
	w.mu.Unlock()
	w.wg.Wait()
	os.RemoveAll(w.dir)
}

func (w *ShellWorld) path(kind, name string) string {
	return filepath.Join(w.dir, kind+"-"+sanitize(name))
}

// sanitize keeps a point's name usable as part of a file name.
func sanitize(name string) string {
	return strings.Map(func(r rune) rune {
		if r == '/' || r == 0 {
			return '_'
		}
		return r
	}, name)
}

// pipe returns the test's open end of the named pipe, making the pipe first. The end is
// read-write, so that opening it does not wait for a process and bytes written to it are kept.
func (w *ShellWorld) pipe(kind, name string, flags int) (*os.File, error) {
	path := w.path(kind, name)
	w.mu.Lock()
	defer w.mu.Unlock()
	if f, ok := w.files[path]; ok {
		return f, nil
	}
	if w.closing {
		return nil, os.ErrClosed
	}
	if err := syscall.Mkfifo(path, 0o600); err != nil && !os.IsExist(err) {
		return nil, err
	}
	f, err := os.OpenFile(path, flags, 0)
	if err != nil {
		return nil, err
	}
	w.files[path] = f
	return f, nil
}

func (w *ShellWorld) must(kind, name string, flags int) *os.File {
	f, err := w.pipe(kind, name, flags)
	if err != nil {
		panic(fmt.Sprintf("envtest: preparing %s %q: %v", kind, name, err))
	}
	return f
}

// Argv implements World.
func (w *ShellWorld) Argv(p Program) []string {
	var script strings.Builder
	w.render(&script, p)
	return []string{"sh", "-c", script.String()}
}

// Missing implements World.
func (w *ShellWorld) Missing() []string {
	return []string{filepath.Join(w.dir, "no-such-command")}
}

func (w *ShellWorld) render(b *strings.Builder, p Program) {
	for _, s := range p {
		switch s.Op {
		case OpPrint:
			fmt.Fprintf(b, "printf '%%s\\n' %s\n", quote(s.Text))
		case OpWarn:
			fmt.Fprintf(b, "printf '%%s\\n' %s >&2\n", quote(s.Text))
		case OpDir:
			b.WriteString("pwd -P\n")
		case OpMark:
			w.must("wake", s.Name, os.O_RDWR)
			fmt.Fprintf(b, ": >> %s\nprintf '\\n' 1<>%s\n", quote(w.path("mark", s.Name)), quote(w.path("wake", s.Name)))
		case OpHold:
			w.must("hold", s.Name, os.O_RDWR)
			fmt.Fprintf(b, "IFS= read -r _ <>%s\n", quote(w.path("hold", s.Name)))
		case OpExit:
			fmt.Fprintf(b, "exit %d\n", s.Code)
		case OpCrash:
			b.WriteString("kill -KILL $$\n")
		case OpChild:
			// The child closes the descriptor its parent may have made observable, so the parent
			// ending is not hidden by it.
			b.WriteString("( exec 9>&-\n")
			w.render(b, s.Body)
			b.WriteString(") &\n")
		case OpAlive:
			// The test opens the read end first, without waiting, so that opening the write
			// end does not wait either.
			w.must("alive", s.Name, os.O_RDONLY|syscall.O_NONBLOCK)
			w.must("wake", AliveMark(s.Name), os.O_RDWR)
			fmt.Fprintf(b, "exec 9>%s\n", quote(w.path("alive", s.Name)))
			fmt.Fprintf(b, ": >> %s\nprintf '\\n' 1<>%s\n", quote(w.path("mark", AliveMark(s.Name))), quote(w.path("wake", AliveMark(s.Name))))
		case OpUnlessFlag:
			fmt.Fprintf(b, "[ -e %s ] || exit 1\n", quote(w.path("flag", s.Name)))
		default:
			panic(fmt.Sprintf("envtest: no rendering for step %q", s.Op))
		}
	}
}

// quote single-quotes text for the shell.
func quote(text string) string { return "'" + strings.ReplaceAll(text, "'", `'\''`) + "'" }

// Release implements World.
func (w *ShellWorld) Release(point string) {
	f := w.must("hold", point, os.O_RDWR)
	if _, err := f.Write([]byte(strings.Repeat("\n", releaseLines))); err != nil {
		panic(fmt.Sprintf("envtest: releasing %q: %v", point, err))
	}
}

// Marked implements World.
func (w *ShellWorld) Marked(point string) bool {
	_, err := os.Stat(w.path("mark", point))
	return err == nil
}

// Reached implements World.
func (w *ShellWorld) Reached(ctx context.Context, point string) bool {
	if w.Marked(point) {
		return true
	}
	f, err := w.pipe("wake", point, os.O_RDWR)
	if err != nil {
		return false
	}
	return w.wait(ctx, func() bool {
		var b [1]byte
		n, err := f.Read(b[:])
		return n == 1 && err == nil
	})
}

// SetFlag implements World.
func (w *ShellWorld) SetFlag(name string) {
	f, err := os.Create(w.path("flag", name))
	if err != nil {
		panic(fmt.Sprintf("envtest: setting flag %q: %v", name, err))
	}
	f.Close()
}

// Gone implements World.
func (w *ShellWorld) Gone(ctx context.Context, name string) bool {
	w.mu.Lock()
	done := w.gone[name]
	w.mu.Unlock()
	if done {
		return true
	}
	// Until the process has opened its end, the read end reports the end of the file at once.
	if !w.Reached(ctx, AliveMark(name)) {
		return false
	}
	f := w.must("alive", name, os.O_RDONLY|syscall.O_NONBLOCK)
	ended := w.wait(ctx, func() bool {
		var b [64]byte
		for {
			n, err := f.Read(b[:])
			if err != nil || n == 0 {
				return true // the end of the file: no process holds the pipe open
			}
		}
	})
	if ended {
		w.mu.Lock()
		w.gone[name] = true
		w.mu.Unlock()
	}
	return ended
}

// wait runs a blocking read in a goroutine the world owns, and returns what it reports or false
// when ctx ends first. The read ends when the world closes.
func (w *ShellWorld) wait(ctx context.Context, read func() bool) bool {
	result := make(chan bool, 1)
	w.mu.Lock()
	if w.closing {
		w.mu.Unlock()
		return false
	}
	w.wg.Go(func() { result <- read() })
	w.mu.Unlock()
	select {
	case ok := <-result:
		return ok
	case <-ctx.Done():
		return false
	}
}
