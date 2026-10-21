package runlog

import (
	"io"
	"os"
	"sort"
	"sync"
)

// Position names the next frame and the last sequence preceding it.
type Position struct {
	Offset     int64
	ThroughSeq uint64
}
type indexedFile struct {
	info   os.FileInfo
	points []Position
}

// Index retains sparse seek positions per process. Replacement/truncation
// invalidates cached positions. It never writes to a run directory.
type Index struct {
	mu    sync.Mutex
	files map[string]*indexedFile
}

func (i *Index) file(path string, info os.FileInfo) *indexedFile {
	if i.files == nil {
		i.files = make(map[string]*indexedFile)
	}
	f := i.files[path]
	if f == nil || !os.SameFile(f.info, info) || info.Size() < f.info.Size() {
		f = &indexedFile{}
		i.files[path] = f
	}
	f.info = info
	return f
}

// Before returns a position strictly before fromSeq, or the log beginning.
func (i *Index) Before(path string, info os.FileInfo, fromSeq uint64) Position {
	i.mu.Lock()
	defer i.mu.Unlock()
	points := i.file(path, info).points
	n := sort.Search(len(points), func(n int) bool { return points[n].ThroughSeq >= fromSeq })
	if n == 0 {
		return Position{}
	}
	return points[n-1]
}

// Observe records a checkpoint every 128 sequences. Concurrent tails may report
// positions out of order; each sequence is retained only once.
func (i *Index) Observe(path string, info os.FileInfo, p Position) {
	if p.ThroughSeq%128 != 0 {
		return
	}
	i.mu.Lock()
	defer i.mu.Unlock()
	f := i.file(path, info)
	n := sort.Search(len(f.points), func(n int) bool { return f.points[n].ThroughSeq >= p.ThroughSeq })
	if n < len(f.points) && f.points[n].ThroughSeq == p.ThroughSeq {
		return
	}
	f.points = append(f.points, Position{})
	copy(f.points[n+1:], f.points[n:])
	f.points[n] = p
}

// NewReaderAt starts at a previously verified frame boundary.
func NewReaderAt(source io.ReaderAt, offset int64) *Reader {
	return &Reader{source: source, offset: offset}
}

func (r *Reader) Offset() int64 { return r.offset }
