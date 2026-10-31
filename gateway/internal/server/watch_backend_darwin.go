package server

import (
	"errors"
	"os"

	"github.com/fsnotify/fsnotify"
	"golang.org/x/sys/unix"
)

// runWatcher reports directory invalidations directly. fsnotify's kqueue
// backend synthesizes directory events by diffing children for additions and
// watching their targets. Unlinking an index symlink changes neither, so its
// removal can otherwise go unnoticed until an unrelated run changes.
//
// The subscription goroutine owns the watch set; the reader only delivers
// invalidations. One pending invalidation suffices because every receive is
// followed by a full scan, and EV_CLEAR retains changes during that scan.
type runWatcher struct {
	Events    chan fsnotify.Event
	Errors    chan error
	queue     int
	wakeRead  *os.File
	wakeWrite *os.File
	done      chan struct{}
	files     map[string]*os.File
}

func newRunWatch() (runWatch, error) {
	queue, err := unix.Kqueue()
	if err != nil {
		return nil, err
	}
	unix.CloseOnExec(queue)
	read, write, err := os.Pipe()
	if err != nil {
		unix.Close(queue)
		return nil, err
	}
	changes := []unix.Kevent_t{{Ident: uint64(read.Fd()), Filter: unix.EVFILT_READ, Flags: unix.EV_ADD | unix.EV_CLEAR}}
	if _, err := unix.Kevent(queue, changes, nil, nil); err != nil {
		read.Close()
		write.Close()
		unix.Close(queue)
		return nil, err
	}
	w := &runWatcher{Events: make(chan fsnotify.Event, 1), Errors: make(chan error, 1), queue: queue,
		wakeRead: read, wakeWrite: write, done: make(chan struct{}), files: map[string]*os.File{}}
	go w.readEvents()
	return w, nil
}

func (w *runWatcher) Changes() <-chan fsnotify.Event { return w.Events }
func (w *runWatcher) Failures() <-chan error         { return w.Errors }

func (w *runWatcher) Add(path string) error {
	if _, ok := w.files[path]; ok {
		return nil
	}
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	changes := []unix.Kevent_t{{Ident: uint64(file.Fd()), Filter: unix.EVFILT_VNODE,
		Flags: unix.EV_ADD | unix.EV_CLEAR, Fflags: unix.NOTE_WRITE | unix.NOTE_DELETE | unix.NOTE_RENAME}}
	if _, err := unix.Kevent(w.queue, changes, nil, nil); err != nil {
		file.Close()
		return err
	}
	w.files[path] = file
	return nil
}

func (w *runWatcher) Remove(path string) error {
	file, ok := w.files[path]
	if !ok {
		return nil
	}
	delete(w.files, path)
	// Closing the descriptor also removes its kqueue registration.
	return file.Close()
}

func (w *runWatcher) WatchList() []string {
	paths := make([]string, 0, len(w.files))
	for path, file := range w.files {
		opened, openErr := file.Stat()
		current, statErr := os.Stat(path)
		if openErr != nil || statErr != nil || !os.SameFile(opened, current) {
			_ = w.Remove(path)
			continue
		}
		paths = append(paths, path)
	}
	return paths
}

func (w *runWatcher) readEvents() {
	defer close(w.done)
	defer close(w.Events)
	defer close(w.Errors)
	events := make([]unix.Kevent_t, 16)
	for {
		n, err := unix.Kevent(w.queue, nil, events, nil)
		if errors.Is(err, unix.EINTR) {
			continue
		}
		if err != nil {
			w.Errors <- err
			return
		}
		for _, event := range events[:n] {
			if event.Filter == unix.EVFILT_READ {
				return
			}
			select {
			case w.Events <- fsnotify.Event{}:
			default:
			}
		}
	}
}

func (w *runWatcher) Close() error {
	_, wakeErr := w.wakeWrite.Write([]byte{1})
	<-w.done
	for path := range w.files {
		_ = w.Remove(path)
	}
	return errors.Join(wakeErr, w.wakeRead.Close(), w.wakeWrite.Close(), unix.Close(w.queue))
}
