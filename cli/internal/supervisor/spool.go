package supervisor

import (
	"io"
	"sync"
)

// Spool keeps a job's output until the daemon has stored it. It is append-only and read by
// offset, so a reader that comes back with the offset it holds loses and repeats nothing.
type Spool interface {
	// Append adds to the end. After a successful Append the bytes are readable.
	Append(data []byte) error
	// ReadAt fills p from the offset and returns how many bytes it filled. At the end it returns
	// 0 and io.EOF.
	ReadAt(p []byte, offset uint64) (int, error)
}

// MemorySpool is a Spool in memory.
type MemorySpool struct {
	mu   sync.Mutex
	data []byte
}

var _ Spool = (*MemorySpool)(nil)

// Append implements Spool.
func (s *MemorySpool) Append(data []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.data = append(s.data, data...)
	return nil
}

// ReadAt implements Spool.
func (s *MemorySpool) ReadAt(p []byte, offset uint64) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if offset >= uint64(len(s.data)) {
		return 0, io.EOF
	}
	return copy(p, s.data[offset:]), nil
}
