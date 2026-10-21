package runlog

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"syscall"

	"github.com/fsnotify/fsnotify"
)

// Follower owns watches and an open descriptor for one append-only file. Read
// may block; Close must be called after the reader has stopped. Use one per Tail
// so a blocked consumer retains only its own bounded batch.
type Follower struct {
	path     string
	watch    *fsnotify.Watcher
	file     *os.File
	reader   *Reader
	pending  []byte
	position Position
	index    *Index
	limit    func() (uint64, bool, error)
	ceiling  uint64
	bounded  bool
}

// Open installs the directory watch before inspecting the file, closing the
// creation/append race. The parent directory must already exist.
func Open(path string) (*Follower, error) {
	watch, err := fsnotify.NewWatcher()
	if err != nil {
		return nil, err
	}
	if err := watch.Add(filepath.Dir(path)); err != nil {
		watch.Close()
		return nil, err
	}
	return &Follower{path: path, watch: watch}, nil
}

// Seek configures a committed reader before its first Read. The limit is
// reloaded after filesystem notifications, including atomic run.head renames.
func (f *Follower) Seek(position Position, index *Index, limit func() (uint64, bool, error)) {
	f.position = position
	f.index = index
	f.limit = limit
}

func (f *Follower) Close() error {
	err := f.watch.Close()
	if f.file != nil {
		err = errors.Join(err, f.file.Close())
	}
	return err
}

// Read waits for a record, then drains currently complete records toward the
// target batch size. A single larger record is returned alone (at most
// MaxRecordBytes). It may return a valid batch alongside a terminal error.
func (f *Follower) Read(ctx context.Context, targetBytes int) ([][]byte, error) {
	var batch [][]byte
	size := 0
	refresh := true
	for {
		if err := ctx.Err(); err != nil {
			return batch, err
		}
		if refresh && f.limit != nil {
			ceiling, bounded, err := f.limit()
			if err != nil {
				return batch, err
			}
			f.ceiling, f.bounded = ceiling, bounded
			refresh = false
		}
		var payload []byte
		var err error
		if f.bounded && f.position.ThroughSeq >= f.ceiling && f.pending == nil {
			err = io.EOF
		} else {
			payload, err = f.next()
		}
		if err == nil {
			if len(batch) > 0 && size+len(payload)+binary.MaxVarintLen64+1 > targetBytes {
				f.pending = payload
				return batch, nil
			}
			batch = append(batch, payload)
			size += len(payload) + binary.MaxVarintLen64 + 1
			if size >= targetBytes {
				return batch, nil
			}
			continue
		}
		if err != io.EOF {
			return batch, err
		}
		if len(batch) > 0 {
			return batch, nil
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case _, ok := <-f.watch.Events:
			if !ok {
				return nil, os.ErrClosed
			}
		case err, ok := <-f.watch.Errors:
			if !ok {
				return nil, os.ErrClosed
			}
			if !errors.Is(err, fsnotify.ErrEventOverflow) {
				return nil, err
			}
		}
		refresh = true
	}
}

func (f *Follower) next() ([]byte, error) {
	if f.pending != nil {
		payload := f.pending
		f.pending = nil
		return payload, nil
	}
	if f.file == nil {
		file, err := os.OpenFile(f.path, os.O_RDONLY|syscall.O_NONBLOCK, 0)
		if errors.Is(err, os.ErrNotExist) {
			return nil, io.EOF
		}
		if err != nil {
			return nil, err
		}
		info, err := file.Stat()
		if err != nil {
			file.Close()
			return nil, err
		}
		if !info.Mode().IsRegular() {
			file.Close()
			return nil, fmt.Errorf("run.log is not a regular file")
		}
		if err := f.watch.Add(f.path); err != nil {
			file.Close()
			return nil, err
		}
		f.file = file
		f.reader = NewReaderAt(file, f.position.Offset)
	}
	payload, err := f.reader.Next()
	if err == nil && f.limit != nil {
		seq, seqErr := Sequence(payload)
		if seqErr != nil {
			return nil, seqErr
		}
		if seq <= f.position.ThroughSeq {
			return nil, f.reader.corrupt("sequence does not increase")
		}
		if f.bounded && seq > f.ceiling {
			return nil, f.reader.corrupt("sequence exceeds committed boundary")
		}
		f.position = Position{Offset: f.reader.Offset(), ThroughSeq: seq}
		if f.index != nil {
			info, statErr := f.file.Stat()
			if statErr != nil {
				return nil, statErr
			}
			f.index.Observe(f.path, info, f.position)
		}
	}
	if err == io.EOF {
		opened, statErr := f.file.Stat()
		if statErr != nil {
			return nil, statErr
		}
		current, statErr := os.Stat(f.path)
		if statErr != nil {
			return nil, statErr
		}
		if !os.SameFile(opened, current) || opened.Size() < f.reader.offset {
			return nil, f.reader.corrupt("append-only file replaced or truncated")
		}
	}
	return payload, err
}
