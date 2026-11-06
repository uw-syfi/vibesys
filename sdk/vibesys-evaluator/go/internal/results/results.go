// Package results writes the results file from its own goroutine, so a sender never waits for the
// file. Put encodes a result and queues it in memory; when the queue is full (the file is written
// slower than operations complete), results go to a spill file on local disk instead and the
// writer copies them over once it has caught up. Results keep the order Put saw them in.
package results

import (
	"errors"
	"io"
	"os"
	"sync"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/files"
)

// DefaultQueue is how many encoded results may wait in memory before results spill.
const DefaultQueue = 1 << 16

// Writer is a results file being written. Put is safe for concurrent use; Close once.
type Writer struct {
	out    *files.Writer
	format files.Format
	limit  int
	dir    string

	mu       sync.Mutex
	wake     *sync.Cond
	queue    [][]byte
	closed   bool
	spilling bool
	spill    *os.File
	spillW   int64 // bytes written to the spill file
	spillBuf []byte
	spilled  uint64
	err      error

	done chan struct{}
}

// Options configures a Writer.
type Options struct {
	// Queue is the in-memory queue's capacity in results; DefaultQueue if zero.
	Queue int
	// SpillDir holds the spill file; os.TempDir() if empty.
	SpillDir string
	// Writer names the writer in the FileHeader.
	Writer string
}

// New writes the results FileHeader to out and starts the writer goroutine.
func New(out io.Writer, format files.Format, options Options) (*Writer, error) {
	file, err := files.NewWriter(out, format, files.Header(pb.FileKind_FILE_KIND_RESULTS, options.Writer))
	if err != nil {
		return nil, err
	}
	w := &Writer{out: file, format: format, limit: options.Queue, dir: options.SpillDir, done: make(chan struct{})}
	if w.limit <= 0 {
		w.limit = DefaultQueue
	}
	w.wake = sync.NewCond(&w.mu)
	go w.run()
	return w, nil
}

// Put queues one result. It never waits for the results file.
func (w *Writer) Put(result *pb.OperationResult) {
	encoded, err := files.Append(nil, w.format, result)
	w.mu.Lock()
	defer w.mu.Unlock()
	if err != nil {
		w.fail(err)
		return
	}
	if !w.spilling && len(w.queue) < w.limit {
		w.queue = append(w.queue, encoded)
		w.wake.Signal()
		return
	}
	w.spilling = true
	if w.spill == nil {
		if w.spill, err = os.CreateTemp(w.dir, "loadgen-spill-*"); err != nil {
			w.fail(err)
			return
		}
	}
	// One write per result to the page cache; the spill file is local disk, never the reader.
	if _, err := w.spill.WriteAt(encoded, w.spillW); err != nil {
		w.fail(err)
		return
	}
	w.spillW += int64(len(encoded))
	w.spilled++
	w.wake.Signal()
}

// fail records the first error; later results are dropped and Close reports it. Holds mu.
func (w *Writer) fail(err error) {
	if w.err == nil {
		w.err = err
	}
}

func (w *Writer) run() {
	defer close(w.done)
	var spillRead int64
	for {
		w.mu.Lock()
		for len(w.queue) == 0 && spillRead == w.spillW && !w.closed {
			w.mu.Unlock()
			// Idle: make what was written visible to a reader of the file before sleeping.
			flushErr := w.out.Flush()
			w.mu.Lock()
			if flushErr != nil {
				w.fail(flushErr)
			}
			if len(w.queue) == 0 && spillRead == w.spillW && !w.closed {
				w.wake.Wait()
			}
		}
		batch := w.queue
		w.queue = nil
		spillEnd, spill := w.spillW, w.spill
		if len(batch) == 0 && spillRead == spillEnd {
			// Closed and drained.
			w.mu.Unlock()
			return
		}
		failed := w.err != nil
		w.mu.Unlock()
		if failed {
			spillRead = spillEnd
			continue
		}
		var err error
		for _, encoded := range batch {
			if err = w.out.WriteRaw(encoded); err != nil {
				break
			}
		}
		// The queue holds results older than any in the spill file, so it goes first; the spill
		// is copied only once the queue is empty, and spilling ends once the copy caught up.
		if err == nil && len(batch) == 0 && spillEnd > spillRead {
			err = w.copySpill(spill, spillRead, spillEnd)
			spillRead = spillEnd
		}
		w.mu.Lock()
		if err != nil {
			w.fail(err)
		}
		if spillRead == w.spillW && len(w.queue) == 0 {
			w.spilling = false
		}
		w.mu.Unlock()
	}
}

func (w *Writer) copySpill(spill *os.File, from, to int64) error {
	buf := make([]byte, 1<<20)
	section := io.NewSectionReader(spill, from, to-from)
	for {
		n, err := section.Read(buf)
		if n > 0 {
			if werr := w.out.WriteRaw(buf[:n]); werr != nil {
				return werr
			}
		}
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
	}
}

// Close writes every queued and spilled result, flushes, removes the spill file, and returns the
// number of results that were spilled and the first error.
func (w *Writer) Close() (uint64, error) {
	w.mu.Lock()
	w.closed = true
	w.wake.Signal()
	w.mu.Unlock()
	<-w.done
	err := w.out.Flush()
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.spill != nil {
		_ = w.spill.Close()
		_ = os.Remove(w.spill.Name())
	}
	if w.err != nil {
		err = w.err
	}
	return w.spilled, err
}
