package server

import (
	"errors"
	"os"
	"path/filepath"
	"time"

	"github.com/fsnotify/fsnotify"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"google.golang.org/protobuf/proto"
)

// WatchRuns watches directories because run.head and index links are published
// by atomic rename. Watch installation precedes each scan, closing the race.
// File contents are never polled. Bounded lock probes are the exception: a
// crashed writer releases flock without any portable filesystem notification.
func (h *host) WatchRuns(_ *apiv1.WatchRunsRequest, stream apiv1.HostService_WatchRunsServer) error {
	watch, err := newRunWatcher()
	if err != nil {
		return rpcError(err)
	}
	defer watch.Close()
	if err := watch.Add(h.config.Root); err != nil {
		return rpcError(err)
	}
	ticker := h.config.Clock.NewTicker(LivenessProbeInterval)
	defer ticker.Stop()
	previous := map[string]*apiv1.WatchRunsResponse{}
	probes := map[string]writerProbe{}
	initial := true
rescan:
	for {
		if err := stream.Context().Err(); err != nil {
			return rpcError(err)
		}
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
				return rpcError(err)
			}
		}
		entries, err := os.ReadDir(index)
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return rpcError(err)
		}
		current := map[string]*apiv1.WatchRunsResponse{}
		directories := map[string]string{}
		for _, entry := range entries {
			if entry.Type()&os.ModeSymlink == 0 {
				continue
			}
			dir, err := h.directory(entry.Name())
			if err != nil {
				if errors.Is(err, os.ErrNotExist) {
					continue
				}
				// Invalid index names and dangling links are not published runs.
				continue
			}
			if err := add(dir); err != nil {
				if errors.Is(err, os.ErrNotExist) {
					continue
				}
				return rpcError(err)
			}
			// A run whose data cannot be read is listed by name as a run without a head,
			// the shape of a phase 1 run. It never ends the stream for the other runs.
			head, err := h.heads.read(dir)
			if err != nil {
				head = nil
			}
			live, err := liveness(dir, head)
			if err != nil {
				live = apiv1.RunLiveness_RUN_LIVENESS_UNSPECIFIED
			}
			response := &apiv1.WatchRunsResponse{RunId: entry.Name(), Head: head, Liveness: live}
			probes[entry.Name()] = probes[entry.Name()].observe(dir, head.GetEpoch(), live, h.config.Clock.Now())
			if project, err := h.heads.project(h.config.Root, entry.Name(), dir, head); err == nil {
				response.ProjectRoot = proto.String(project)
			}
			current[entry.Name()] = response
			directories[entry.Name()] = dir
			if !proto.Equal(previous[entry.Name()], response) {
				if err := stream.Send(response); err != nil {
					return err
				}
			}
		}
		for id := range previous {
			if current[id] == nil {
				delete(probes, id)
				if err := stream.Send(&apiv1.WatchRunsResponse{RunId: id, Removed: true}); err != nil {
					return err
				}
			}
		}
		for path := range watched {
			if !wanted[path] {
				_ = watch.Remove(path)
				delete(watched, path)
			}
		}
		previous = current
		if initial {
			if err := stream.Send(&apiv1.WatchRunsResponse{InitialComplete: true}); err != nil {
				return err
			}
			initial = false
		}
		for {
			select {
			case <-stream.Context().Done():
				return rpcError(stream.Context().Err())
			case _, ok := <-watch.Events:
				if !ok {
					return rpcError(os.ErrClosed)
				}
				continue rescan
			case err, ok := <-watch.Errors:
				if !ok {
					return rpcError(os.ErrClosed)
				}
				if errors.Is(err, fsnotify.ErrEventOverflow) {
					continue rescan
				}
				return rpcError(err)
			case <-ticker.Ticks():
				now := h.config.Clock.Now()
				for id, response := range previous {
					if response.Liveness != apiv1.RunLiveness_RUN_LIVENESS_ALIVE &&
						(probes[id].confirmed || now.After(probes[id].until)) {
						continue
					}
					live, err := liveness(directories[id], response.Head)
					if err != nil {
						continue
					}
					probes[id] = probes[id].observe(directories[id], response.Head.GetEpoch(), live, now)
					if live != response.Liveness {
						// Messages remain immutable after Send, including for gRPC stats.
						observed := proto.Clone(response).(*apiv1.WatchRunsResponse)
						observed.Liveness = live
						previous[id] = observed
						if err := stream.Send(observed); err != nil {
							return err
						}
					}
				}
				if err := stream.Send(&apiv1.WatchRunsResponse{Heartbeat: true}); err != nil {
					return err
				}
			}
		}
	}
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
