package server

import (
	"sync"
	"syscall"

	"github.com/fsnotify/fsnotify"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/subscription"
)

// ScriptedFeed is the runs topic's change feed under the test's control. It reports a
// change only when told to, and its reads block on channels, which a synctest bubble
// sees as idle; a platform watcher's reads are system calls, which it does not.
type ScriptedFeed struct {
	changes chan fsnotify.Event
	failure chan error

	mu      sync.Mutex
	watched map[string]bool
	// AddFails, when set, is what every Add returns: a root the source cannot watch.
	AddFails error
	// Budget, when positive, is how many paths the feed holds: each Add past it fails as a host
	// out of watches does.
	Budget int
}

func NewScriptedFeed() *ScriptedFeed {
	return &ScriptedFeed{changes: make(chan fsnotify.Event, 1), failure: make(chan error, 1), watched: map[string]bool{}}
}

// Change reports that something in a watched directory changed. Changes that pile up
// while the source scans are one, as a platform watcher's are.
func (f *ScriptedFeed) Change() {
	select {
	case f.changes <- fsnotify.Event{}:
	default:
	}
}

func (f *ScriptedFeed) Add(path string) error {
	if f.AddFails != nil {
		return f.AddFails
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.Budget > 0 && !f.watched[path] && len(f.watched) >= f.Budget {
		return syscall.ENOSPC
	}
	f.watched[path] = true
	return nil
}

func (f *ScriptedFeed) Remove(path string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.watched, path)
	return nil
}

func (f *ScriptedFeed) WatchList() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	paths := make([]string, 0, len(f.watched))
	for path := range f.watched {
		paths = append(paths, path)
	}
	return paths
}

func (f *ScriptedFeed) Close() error                   { return nil }
func (f *ScriptedFeed) Changes() <-chan fsnotify.Event { return f.changes }
func (f *ScriptedFeed) Failures() <-chan error         { return f.failure }

// RunsTopicOver is the runs topic over config's root, reading its changes from feed.
func RunsTopicOver(config Config, feed *ScriptedFeed) subscription.Topic[*apiv1.RunEntry] {
	return runsTopic(&host{config: config.withDefaults()}, func() (runWatch, error) { return feed, nil })
}
