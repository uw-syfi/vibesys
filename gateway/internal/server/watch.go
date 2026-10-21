package server

import (
	"errors"
	"os"
	"path/filepath"

	"github.com/fsnotify/fsnotify"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"google.golang.org/protobuf/proto"
)

// WatchRuns watches directories because run.head and index links are published
// by atomic rename. Watch installation precedes each scan, closing the race.
// Liveness is a hint sampled on subscription and filesystem changes; flock
// itself has no portable change notification (unlock does not modify a file).
func (h *host) WatchRuns(_ *apiv1.WatchRunsRequest, stream apiv1.HostService_WatchRunsServer) error {
	watch, err := fsnotify.NewWatcher()
	if err != nil {
		return rpcError(err)
	}
	defer watch.Close()
	if err := watch.Add(h.config.Root); err != nil {
		return rpcError(err)
	}
	previous := map[string]*apiv1.WatchRunsResponse{}
	initial := true
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
		err := add(index)
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return rpcError(err)
		}
		entries, err := os.ReadDir(index)
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return rpcError(err)
		}
		current := map[string]*apiv1.WatchRunsResponse{}
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
			head, err := readHead(dir)
			if err != nil {
				return rpcError(err)
			}
			live, err := liveness(dir, head)
			if err != nil {
				return rpcError(err)
			}
			response := &apiv1.WatchRunsResponse{RunId: entry.Name(), Head: head, Liveness: live}
			current[entry.Name()] = response
			if !proto.Equal(previous[entry.Name()], response) {
				if err := stream.Send(response); err != nil {
					return err
				}
			}
		}
		for id := range previous {
			if current[id] == nil {
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
		if err := waitChange(stream.Context().Done(), watch); err != nil {
			return rpcError(err)
		}
	}
}
