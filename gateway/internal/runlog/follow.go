package runlog

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"time"

	"github.com/fsnotify/fsnotify"
)

// Follower owns watches and an open descriptor for one append-only file. Read
// may block; Close must be called after the reader has stopped. Use one per Tail
// so a blocked consumer retains only its own bounded batch.
type Follower struct {
	path    string
	watch   *fsnotify.Watcher
	file    *os.File
	reader  *Reader
	pending []byte
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
	// This re-check recovers from coalesced/lost filesystem notifications. Normal
	// progress is event driven; no correctness test waits for the ticker.
	guard := time.NewTicker(3 * time.Second)
	defer guard.Stop()
	var batch [][]byte
	size := 0
	for {
		if err := ctx.Err(); err != nil {
			return batch, err
		}
		payload, err := f.next()
		if err == nil {
			if len(batch) > 0 && size+len(payload) > targetBytes {
				f.pending = payload
				return batch, nil
			}
			batch = append(batch, payload)
			size += len(payload)
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
		case <-guard.C:
		}
	}
}

func (f *Follower) next() ([]byte, error) {
	if f.pending != nil {
		payload := f.pending
		f.pending = nil
		return payload, nil
	}
	if f.file == nil {
		file, err := os.Open(f.path)
		if errors.Is(err, os.ErrNotExist) {
			return nil, io.EOF
		}
		if err != nil {
			return nil, err
		}
		if err := f.watch.Add(f.path); err != nil {
			file.Close()
			return nil, err
		}
		f.file = file
		f.reader = NewReader(file)
	}
	payload, err := f.reader.Next()
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
