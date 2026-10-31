package server

import "github.com/fsnotify/fsnotify"

// inotifyWatch is fsnotify's watcher as a runWatch.
type inotifyWatch struct{ *fsnotify.Watcher }

func newRunWatch() (runWatch, error) {
	watcher, err := fsnotify.NewWatcher()
	if err != nil {
		return nil, err
	}
	return inotifyWatch{watcher}, nil
}

func (w inotifyWatch) Changes() <-chan fsnotify.Event { return w.Events }
func (w inotifyWatch) Failures() <-chan error         { return w.Errors }
