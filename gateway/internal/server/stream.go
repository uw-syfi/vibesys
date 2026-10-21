package server

import (
	"errors"
	"io"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"slices"

	"github.com/fsnotify/fsnotify"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

var streamID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$`)

func waitChange(ctxDone <-chan struct{}, watch *fsnotify.Watcher) error {
	select {
	case <-ctxDone:
		return nil
	case _, ok := <-watch.Events:
		if !ok {
			return os.ErrClosed
		}
		return nil
	case err, ok := <-watch.Errors:
		if !ok {
			return os.ErrClosed
		}
		// Overflow asks the caller to rescan, never to infer a missing change.
		if errors.Is(err, fsnotify.ErrEventOverflow) {
			return nil
		}
		return err
	}
}

func (h *host) TailStream(request *apiv1.TailStreamRequest, stream apiv1.HostService_TailStreamServer) error {
	dir, err := h.directory(request.GetRunId())
	if err != nil {
		return err
	}
	if !streamID.MatchString(request.GetStreamId()) || request.GetFromOffset() > math.MaxInt64 {
		return status.Error(codes.InvalidArgument, "invalid stream_id or from_offset")
	}
	watch, err := fsnotify.NewWatcher()
	if err != nil {
		return rpcError(err)
	}
	defer watch.Close()
	if err := watch.Add(dir); err != nil {
		return rpcError(err)
	}
	path := filepath.Join("streams", request.GetStreamId())
	var file *os.File
	defer func() {
		if file != nil {
			file.Close()
		}
	}()
	offset := int64(request.GetFromOffset())
	observedSize := int64(0)
	// Install existing directory/file watches before advertising readiness.
	prepare := func() error {
		if !slices.Contains(watch.WatchList(), filepath.Join(dir, "streams")) {
			err := watch.Add(filepath.Join(dir, "streams"))
			if err != nil && !errors.Is(err, os.ErrNotExist) {
				return err
			}
		}
		if file == nil {
			f, err := openRegular(dir, path)
			if errors.Is(err, os.ErrNotExist) {
				return nil
			}
			if err != nil {
				return err
			}
			if err := watch.Add(f.Name()); err != nil {
				f.Close()
				return err
			}
			file = f
		}
		return nil
	}
	if err := prepare(); err != nil {
		return rpcError(err)
	}
	if err := stream.SendHeader(metadata.MD{}); err != nil {
		return err
	}
	buffer := make([]byte, 64<<10)
	for {
		if err := stream.Context().Err(); err != nil {
			return rpcError(err)
		}
		if err := prepare(); err != nil {
			return rpcError(err)
		}
		if file != nil {
			info, err := file.Stat()
			if err != nil {
				return rpcError(err)
			}
			current, err := os.Stat(filepath.Join(dir, path))
			if err != nil {
				return rpcError(err)
			}
			if !os.SameFile(info, current) {
				return status.Error(codes.DataLoss, "stream replaced")
			}
			if info.Size() < observedSize {
				return status.Error(codes.DataLoss, "stream truncated")
			}
			observedSize = info.Size()
			n, err := file.ReadAt(buffer, offset)
			if err != nil && !errors.Is(err, io.EOF) {
				return rpcError(err)
			}
			if n > 0 {
				offset += int64(n)
				if err := stream.Send(&apiv1.TailStreamResponse{Data: buffer[:n], NextOffset: uint64(offset)}); err != nil {
					return err
				}
				continue
			}
		}
		if err := waitChange(stream.Context().Done(), watch); err != nil {
			return rpcError(err)
		}
	}
}
