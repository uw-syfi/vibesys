package server

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"syscall"
	"time"

	"github.com/fsnotify/fsnotify"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"github.com/uw-syfi/vibesys/gateway/internal/subscription"
	"google.golang.org/protobuf/proto"
)

// maxRunBytes is the runs topic's bound on one entry, which a head must fit.
var maxRunBytes = hostoperations.Topics[hostoperations.TopicRuns].MaxValueBytes

// runWatch is the feed of directory changes a source rescans on: the platform's watcher,
// or a scripted one under test. A change carries no content; every one is followed by a
// full scan.
type runWatch interface {
	Add(path string) error
	Remove(path string) error
	WatchList() []string
	Close() error
	Changes() <-chan fsnotify.Event
	Failures() <-chan error
}

// runsTopic is the runs topic: the run index as a map of run id to its head and liveness.
func runsTopic(h *host, watch func() (runWatch, error)) subscription.Topic[*apiv1.RunEntry] {
	return subscription.Topic[*apiv1.RunEntry]{
		Name:   hostoperations.TopicRuns,
		Policy: hostoperations.Topics[hostoperations.TopicRuns],
		Source: runsSource{h, watch},
		Entry: func(key string, value *apiv1.RunEntry) *apiv1.TopicEntry {
			return &apiv1.TopicEntry{Key: key, Value: &apiv1.TopicEntry_Runs{Runs: value}}
		},
	}
}

// runsSource publishes the run index. It watches directories because run.head and
// index links are published by atomic rename, and installs each watch before the
// scan that follows it, closing the race. File contents are never polled. Bounded
// lock probes are the exception: a crashed writer releases flock without any
// portable filesystem notification.
type runsSource struct {
	host  *host
	watch func() (runWatch, error)
}

func (s runsSource) Run(ctx context.Context, sink subscription.Sink[*apiv1.RunEntry]) error {
	h := s.host
	watch, err := s.watch()
	if err != nil {
		return err
	}
	defer watch.Close()
	if err := watch.Add(h.config.Root); err != nil {
		return err
	}
	ticker := h.config.Probes.NewTicker(LivenessProbeInterval)
	defer ticker.Stop()
	// previous is what the sink holds, which a source cannot read back. It also holds
	// each run's last-known project.
	previous := map[string]*apiv1.RunEntry{}
	probes := map[string]writerProbe{}
	synced := false
rescan:
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		// Whether a run of this scan has no watch, and so no change of its is heard.
		polled := false
		// fsnotify drops watches when a directory is removed. Reconcile its live
		// watch set so a later recreation at the same path is watched again.
		watched := make(map[string]bool)
		for _, path := range watch.WatchList() {
			watched[path] = true
		}
		wanted := map[string]bool{h.config.Root: true}
		add := func(path string) error {
			if !watched[path] {
				if err := watch.Add(path); err != nil {
					return err
				}
				watched[path] = true
			}
			wanted[path] = true
			return nil
		}
		index := filepath.Join(h.config.Root, "runs")
		for _, directory := range []string{index, filepath.Join(h.config.Root, "run-addresses")} {
			if err := add(directory); err != nil && !errors.Is(err, os.ErrNotExist) {
				return err
			}
		}
		entries, err := os.ReadDir(index)
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		current := map[string]*apiv1.RunEntry{}
		directories := map[string]string{}
		for _, entry := range entries {
			if err := ctx.Err(); err != nil {
				return err
			}
			// A scan of a large index on a slow filesystem can take longer than the topic's source
			// lease. Each run read is a sign the scan is moving, and a read that never returns is
			// the lack of one.
			sink.Alive()
			if entry.Type()&os.ModeSymlink == 0 {
				continue
			}
			// Invalid index names and dangling links are not published runs.
			dir, err := h.directory(entry.Name())
			if err != nil {
				continue
			}
			if err := add(dir); err != nil {
				switch {
				case errors.Is(err, os.ErrNotExist):
					continue
				case watchBudgetSpent(err):
					// The host has no watch to give this run: it is listed all the same, and read
					// again on every probe tick instead of on a change.
					polled = true
				default:
					return err
				}
			}
			// A run whose data cannot be read is listed by name as a run without a head,
			// the shape of a phase 1 run. It never ends the topic for the other runs.
			head, err := h.heads.read(dir)
			if err != nil {
				head = nil
			}
			live, err := liveness(dir, head)
			if err != nil {
				live = apiv1.RunLiveness_RUN_LIVENESS_UNSPECIFIED
			}
			value := &apiv1.RunEntry{Head: head, Liveness: live}
			probes[entry.Name()] = probes[entry.Name()].observe(dir, head.GetEpoch(), live, h.config.Probes.Now())
			if project, err := h.heads.project(h.config.Root, entry.Name(), dir, head); err == nil {
				value.ProjectRoot = proto.String(project)
			} else if old := previous[entry.Name()]; old != nil && old.ProjectRoot != nil {
				// A last-known external root is still a proof only for the same physical key.
				if root, err := externalProject(h.config.Root, entry.Name(), dir, old.GetProjectRoot()); err == nil {
					value.ProjectRoot = proto.String(root)
				}
			}
			if proto.Size(value) > maxRunBytes {
				// One run past the bound, project included, must not fail the topic for every
				// subscriber: it is listed without its head.
				value.Head = nil
			}
			current[entry.Name()] = value
			directories[entry.Name()] = dir
			if !proto.Equal(previous[entry.Name()], value) {
				sink.Put(entry.Name(), value)
			}
		}
		for id := range previous {
			if current[id] == nil {
				delete(probes, id)
				sink.Remove(id)
			}
		}
		for path := range watched {
			if !wanted[path] {
				_ = watch.Remove(path)
				delete(watched, path)
			}
		}
		previous = current
		if !synced {
			sink.Synced()
			synced = true
		}
		for {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case _, ok := <-watch.Changes():
				if !ok {
					return os.ErrClosed
				}
				continue rescan
			case err, ok := <-watch.Failures():
				if !ok {
					return os.ErrClosed
				}
				if errors.Is(err, fsnotify.ErrEventOverflow) {
					continue rescan
				}
				return err
			case <-ticker.Ticks():
				// The source's own rhythm: a quiet index still shows the source is running.
				sink.Alive()
				if polled {
					// A run without a watch is only seen by reading it again: its next change is
					// found within a tick, as a watched run's is found at once.
					continue rescan
				}
				now := h.config.Probes.Now()
				for id, value := range previous {
					if value.Liveness != apiv1.RunLiveness_RUN_LIVENESS_ALIVE &&
						(probes[id].confirmed || now.After(probes[id].until)) {
						continue
					}
					live, err := liveness(directories[id], value.Head)
					if err != nil {
						continue
					}
					probes[id] = probes[id].observe(directories[id], value.Head.GetEpoch(), live, now)
					if live != value.Liveness {
						// The sink owns what it was given, so the change is a new value.
						observed := proto.Clone(value).(*apiv1.RunEntry)
						observed.Liveness = live
						previous[id] = observed
						sink.Put(id, observed)
					}
				}
			}
		}
	}
}

// watchBudgetSpent says whether a watch could not be added because the host has no more to give:
// the kernel's limit on watches (inotify) or on open descriptors (a kqueue watch holds one).
// Neither is the run's fault, and neither is a reason to hide the runs the host does hold.
func watchBudgetSpent(err error) bool {
	return errors.Is(err, syscall.ENOSPC) || errors.Is(err, syscall.EMFILE) || errors.Is(err, syscall.ENFILE)
}

// A free lock before this epoch's writer was seen alive may be its boot gap.
// Keep probing for one lease; unrelated directory events must not renew it.
// An alive writer stays monitored, and its later release is conclusive.
type writerProbe struct {
	directory string
	epoch     uint32
	until     time.Time
	confirmed bool
}

func (p writerProbe) observe(directory string, epoch uint32, live apiv1.RunLiveness, now time.Time) writerProbe {
	if p.directory != directory || p.epoch != epoch {
		p = writerProbe{directory: directory, epoch: epoch, until: now.Add(LivenessLease)}
	}
	switch live {
	case apiv1.RunLiveness_RUN_LIVENESS_ALIVE, apiv1.RunLiveness_RUN_LIVENESS_ENDED, apiv1.RunLiveness_RUN_LIVENESS_PARKED:
		p.confirmed = true
	}
	return p
}
