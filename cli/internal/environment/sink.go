package environment

import (
	"io"
	"sync"
)

// Sink is where the several processes of one execution write their combined output. It
// serializes their writes to the one output, and it remembers the first failure of the output
// instead of passing it on: the writer of a process is a copier that must keep reading, so that
// the process is never blocked on an output that failed. The environment asks for the failure
// when the execution is over (Err), since output that was lost is a result it cannot vouch for.
type Sink struct {
	mu  sync.Mutex
	w   io.Writer
	err error
}

// NewSink returns a Sink that writes to w.
func NewSink(w io.Writer) *Sink { return &Sink{w: w} }

// Write implements io.Writer. It always reports the whole of p as written.
func (s *Sink) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.err == nil {
		if _, err := s.w.Write(p); err != nil {
			s.err = err
		}
	}
	return len(p), nil
}

// Err returns the first failure of the output, or nil.
func (s *Sink) Err() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.err
}
