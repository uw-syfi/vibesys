package plan

import (
	"bufio"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"runtime"
	"sync"
	"sync/atomic"
	"time"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/files"
)

// ErrStreamEnded means the generator's stream ended while loadgen still read it: a generator
// writes until loadgen closes the stream.
var ErrStreamEnded = errors.New("the generator's request stream ended before the phase did")

const (
	// maxFrameBytes bounds one PlannedOperation.
	maxFrameBytes = 16 << 20
	// maxBatch bounds the operations compiled as one unit of work.
	maxBatch = 128
)

// Stream reads a generator's request stream (binpb) and compiles it ahead of the senders, on a
// few CPUs, into a buffer of at most Buffer operations: a reader splits the stream into frames,
// workers validate and compile them in parallel, and a sequencer chains the exclusive keys in
// stream order and fills the buffer. A full buffer blocks the sequencer, then the workers, the
// reader, and through the pipe the generator: the generator never runs further ahead than the
// buffer and the pipe hold.
type Stream struct {
	names  []string
	buffer int
	ops    chan *Op
	stop   chan struct{}
	halt   sync.Once
	ready  chan struct{}
	ending sync.Once
	wg     sync.WaitGroup

	mu        sync.Mutex
	err       error
	started   time.Time
	readyAt   time.Time
	first     time.Time
	last      time.Time
	blocked   time.Duration
	delivered uint64
	stopped   bool
}

type batch struct {
	first  uint64
	frames [][]byte
	ops    []*Op
	err    error
	done   chan struct{}
}

// OpenStream reads the stream's FileHeader and RequestsHeader from in, then starts compiling the
// operations after them into a buffer of buffer operations. started is when loadgen started, for
// GeneratorStats.ready_secs. Errors wrap ErrInvalid or files.ErrSchemaVersion for a stream that
// is not runnable.
func OpenStream(in io.Reader, buffer int, started time.Time) (*Stream, error) {
	if buffer < 1 {
		return nil, fmt.Errorf("--buffer must be at least 1")
	}
	reader := bufio.NewReaderSize(in, 1<<16)
	names, err := readStreamHeader(reader)
	if err != nil {
		return nil, err
	}
	s := &Stream{
		names:   names,
		buffer:  buffer,
		ops:     make(chan *Op, buffer),
		stop:    make(chan struct{}),
		ready:   make(chan struct{}),
		started: started,
	}
	workers := max(1, min(runtime.GOMAXPROCS(0)/2, 4))
	order := make(chan *batch, 2*workers)
	work := make(chan *batch, 2*workers)
	s.wg.Add(2 + workers)
	go s.read(reader, order, work)
	for range workers {
		go s.compile(work)
	}
	go s.sequence(order)
	return s, nil
}

// readStreamHeader reads and checks the FileHeader and the RequestsHeader.
func readStreamHeader(in *bufio.Reader) ([]string, error) {
	frame, err := readFrame(in)
	if err != nil {
		if errors.Is(err, io.EOF) {
			return nil, fmt.Errorf("%w: before its FileHeader", ErrStreamEnded)
		}
		return nil, invalid(fmt.Errorf("FileHeader: %w", err))
	}
	fileHeader := &pb.FileHeader{}
	if err := proto.Unmarshal(frame, fileHeader); err != nil {
		return nil, fmt.Errorf("%w: FileHeader: %v", ErrInvalid, err)
	}
	if err := files.CheckHeader(fileHeader, pb.FileKind_FILE_KIND_REQUESTS); err != nil {
		return nil, invalid(err)
	}
	if frame, err = readFrame(in); err != nil {
		if errors.Is(err, io.EOF) {
			return nil, fmt.Errorf("%w: no RequestsHeader after the FileHeader", ErrInvalid)
		}
		return nil, invalid(fmt.Errorf("RequestsHeader: %w", err))
	}
	h := &pb.RequestsHeader{}
	if err := proto.Unmarshal(frame, h); err != nil {
		return nil, fmt.Errorf("%w: RequestsHeader: %v", ErrInvalid, err)
	}
	if err := validator.Validate(h); err != nil {
		return nil, fmt.Errorf("%w: RequestsHeader: %v", ErrInvalid, err)
	}
	return h.GetOperationNames(), nil
}

// Names is the stream's operation names.
func (s *Stream) Names() []string { return s.names }

func (s *Stream) read(in *bufio.Reader, order, work chan<- *batch) {
	defer s.wg.Done()
	defer close(work)
	defer close(order)
	next := uint64(0)
	for {
		b := &batch{first: next, done: make(chan struct{})}
		frame, err := readFrame(in)
		for err == nil {
			b.frames = append(b.frames, frame)
			if len(b.frames) >= maxBatch || !frameBuffered(in) {
				break
			}
			frame, err = readFrame(in)
		}
		next += uint64(len(b.frames))
		if err != nil {
			b.err = err
			if errors.Is(err, io.EOF) {
				b.err = fmt.Errorf("%w (after %d operations)", ErrStreamEnded, next)
			}
		}
		// The workers may set b.err once they have b: the reader decides on its own copy.
		failed, empty := b.err != nil, len(b.frames) == 0
		if failed && empty {
			close(b.done)
		}
		select {
		case order <- b:
		case <-s.stop:
			return
		}
		if failed && empty {
			return
		}
		select {
		case work <- b:
		case <-s.stop:
			return
		}
		if failed {
			return
		}
	}
}

// frameBuffered reports whether a whole frame is already buffered.
func frameBuffered(in *bufio.Reader) bool {
	n := in.Buffered()
	if n == 0 {
		return false
	}
	head, _ := in.Peek(min(n, binary.MaxVarintLen64))
	size, used := binary.Uvarint(head)
	return used > 0 && uint64(n-used) >= size
}

func readFrame(in *bufio.Reader) ([]byte, error) {
	size, err := binary.ReadUvarint(in)
	if err != nil {
		if errors.Is(err, io.EOF) {
			return nil, io.EOF
		}
		return nil, fmt.Errorf("%w: %v", files.ErrMalformed, err)
	}
	if size > maxFrameBytes {
		return nil, fmt.Errorf("%w: a message of %d bytes", files.ErrMalformed, size)
	}
	frame := make([]byte, size)
	if _, err := io.ReadFull(in, frame); err != nil {
		return nil, fmt.Errorf("%w: truncated message: %v", files.ErrMalformed, err)
	}
	return frame, nil
}

func (s *Stream) compile(work <-chan *batch) {
	defer s.wg.Done()
	for b := range work {
		b.ops = make([]*Op, 0, len(b.frames))
		for i, frame := range b.frames {
			index := b.first + uint64(i)
			planned := &pb.PlannedOperation{}
			if err := proto.Unmarshal(frame, planned); err != nil {
				b.err = fmt.Errorf("%w: operation %d: %w: %v", ErrInvalid, index, files.ErrMalformed, err)
				break
			}
			op, err := compile(index, planned, s.names)
			if err != nil {
				b.err = fmt.Errorf("%w: operation %d: %v", ErrInvalid, index, err)
				break
			}
			b.ops = append(b.ops, &op)
		}
		b.frames = nil
		close(b.done)
	}
}

func (s *Stream) sequence(order <-chan *batch) {
	defer s.wg.Done()
	last := map[string]chan struct{}{}
	for b := range order {
		select {
		case <-b.done:
		case <-s.stop:
			return
		}
		for _, op := range b.ops {
			if op.Key != "" {
				op.Prev = last[op.Key]
				op.Done = make(chan struct{})
				last[op.Key] = op.Done
			}
			if !s.deliver(op) {
				return
			}
		}
		if b.err != nil {
			s.fail(b.err)
			return
		}
	}
}

// deliver puts op into the buffer, timing how long a full buffer blocks it.
func (s *Stream) deliver(op *Op) bool {
	now := time.Now()
	s.mu.Lock()
	if s.delivered == 0 {
		s.first = now
	}
	s.mu.Unlock()
	select {
	case s.ops <- op:
	default:
		s.markReady()
		select {
		case s.ops <- op:
		case <-s.stop:
			return false
		}
		s.mu.Lock()
		s.blocked += time.Since(now)
		s.mu.Unlock()
	}
	s.mu.Lock()
	s.delivered++
	s.last = time.Now()
	full := s.delivered >= uint64(s.buffer)
	s.mu.Unlock()
	if full {
		s.markReady()
	}
	return true
}

func (s *Stream) markReady() {
	s.ending.Do(func() {
		s.mu.Lock()
		s.readyAt = time.Now()
		s.mu.Unlock()
		close(s.ready)
	})
}

func (s *Stream) fail(err error) {
	s.mu.Lock()
	s.err = err
	s.mu.Unlock()
	close(s.ops)
	s.markReady()
}

// failure is why the buffer closed.
func (s *Stream) failure() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.err
}

// WaitReady waits until the buffer is full: the generator has started. It returns the stream's
// error if the stream failed first.
func (s *Stream) WaitReady(ctx context.Context) error {
	select {
	case <-s.ready:
	case <-ctx.Done():
		return ctx.Err()
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.err != nil && s.delivered < uint64(s.buffer) {
		return s.err
	}
	return nil
}

// Take returns the next operation in stream order and how long the caller waited for it (0
// unless the buffer was empty). An error means the stream failed (ErrInvalid, ErrStreamEnded or
// an I/O error) or ctx ended.
func (s *Stream) Take(ctx context.Context) (*Op, time.Duration, error) {
	select {
	case op, ok := <-s.ops:
		if !ok {
			return nil, 0, s.failure()
		}
		return op, 0, nil
	default:
	}
	began := time.Now()
	select {
	case op, ok := <-s.ops:
		if !ok {
			return nil, time.Since(began), s.failure()
		}
		return op, time.Since(began), nil
	case <-ctx.Done():
		return nil, time.Since(began), ctx.Err()
	}
}

// Stop stops reading: the pipeline's goroutines return once the caller closes the stream's
// reader (closing the pipe ends the generator). Stats are final after Stop.
func (s *Stream) Stop() {
	s.halt.Do(func() {
		s.mu.Lock()
		s.stopped = true
		s.mu.Unlock()
		close(s.stop)
	})
}

// Wait waits for the pipeline's goroutines, after Stop and closing the reader.
func (s *Stream) Wait() { s.wg.Wait() }

// Stats is what GeneratorStats reports of the stream (wait, waited and max_wait are the
// summary's, from the senders).
func (s *Stream) Stats() *pb.GeneratorStats {
	s.mu.Lock()
	defer s.mu.Unlock()
	stats := &pb.GeneratorStats{Read: s.delivered, Buffer: uint32(s.buffer)}
	if !s.readyAt.IsZero() {
		stats.ReadySecs = max(s.readyAt.Sub(s.started).Seconds(), 0)
	}
	if busy := s.last.Sub(s.first) - s.blocked; s.delivered > 1 && busy > 0 {
		stats.Rate = float64(s.delivered-1) / busy.Seconds()
	}
	return stats
}

// Repeat is an endless source of copies of one operation without an exclusive key (the
// selftest's), indexed 0, 1, 2, ...
type Repeat struct {
	names []string
	op    Op
	next  atomic.Uint64
}

// NewRepeat repeats op, which must have no exclusive key.
func NewRepeat(names []string, op Op) *Repeat {
	if op.Key != "" {
		panic("plan.NewRepeat: an operation with an exclusive key cannot repeat")
	}
	return &Repeat{names: names, op: op}
}

// Names is the operation names.
func (r *Repeat) Names() []string { return r.names }

// Take returns the next copy at once.
func (r *Repeat) Take(context.Context) (*Op, time.Duration, error) {
	op := r.op
	op.Index = r.next.Add(1) - 1
	return &op, 0, nil
}
