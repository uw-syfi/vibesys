package server

import (
	"context"
	"errors"
	"io"
	"math"
	"os"
	"path/filepath"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

// skeletonBytes includes protobuf framing and leaves room under the default
// gRPC receive limit. Tail carries remaining records, even a large first one.
const skeletonBytes = 1 << 20

func (h *host) Skeleton(ctx context.Context, request *apiv1.SkeletonRequest) (*apiv1.SkeletonResponse, error) {
	dir, err := h.directory(request.GetRunId())
	if err != nil {
		return nil, err
	}
	cap, bounded, err := committed(dir)
	if err != nil {
		return nil, rpcError(err)
	}
	data, position, err := latestKeyframe(dir, cap, skeletonBytes-32)
	if status.Code(err) == codes.ResourceExhausted {
		// An oversized keyframe cannot fit a unary prefix. Replay from the log.
		data, position, err = nil, runlog.Position{}, nil
	}
	if err != nil {
		return nil, rpcError(err)
	}
	response := &apiv1.SkeletonResponse{Keyframe: data, CommittedSeq: position.ThroughSeq, NextSeq: position.ThroughSeq + 1, Complete: true}
	if position.Offset == 0 {
		response.NextSeq = 0 // Phase 1 logs may start at sequence zero.
	}
	if bounded {
		response.CommittedSeq = cap
	}
	f, err := openRegular(dir, "run.log")
	if errors.Is(err, os.ErrNotExist) {
		if (!bounded || cap == 0) && position.ThroughSeq == 0 {
			return response, nil
		}
		return nil, status.Error(codes.DataLoss, "committed run.log is missing")
	}
	if err != nil {
		return nil, rpcError(err)
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, rpcError(err)
	}
	if position.Offset > info.Size() {
		return nil, status.Error(codes.DataLoss, "keyframe offset exceeds run.log")
	}
	// Freeze the readable byte extent for phase 1 runs with a concurrent writer.
	reader := runlog.NewReaderAt(io.NewSectionReader(f, 0, info.Size()), position.Offset)
	previous := position.ThroughSeq
	seen := position.Offset > 0
	// Reserve maximum encodings of committed_seq, next_seq and complete.
	size := proto.Size(&apiv1.SkeletonResponse{Keyframe: data}) + 24
	for !bounded || previous < cap {
		if err := ctx.Err(); err != nil {
			return nil, rpcError(err)
		}
		payload, err := reader.Next()
		if errors.Is(err, io.EOF) {
			if bounded {
				return nil, status.Error(codes.DataLoss, "run.head commits beyond complete run.log")
			}
			break
		}
		if err != nil {
			return nil, rpcError(err)
		}
		seq, err := runlog.Sequence(payload)
		if err != nil {
			return nil, rpcError(err)
		}
		if (seen && seq <= previous) || (bounded && seq > cap) {
			return nil, status.Error(codes.DataLoss, "invalid committed log sequence")
		}
		recordBytes := 1 + protowire.SizeBytes(len(payload))
		if size+recordBytes > skeletonBytes {
			response.NextSeq = seq
			response.Complete = false
			break
		}
		size += recordBytes
		seen = true
		previous = seq
		response.NextSeq = seq + 1
		h.index.Observe(filepath.Join(dir, "run.log"), info, runlog.Position{Offset: reader.Offset(), ThroughSeq: seq})
		response.Records = append(response.Records, payload)
	}
	if !bounded {
		response.CommittedSeq = previous
	}
	return response, nil
}

func (h *host) seek(path string, from, cap uint64) (runlog.Position, error) {
	var position runlog.Position
	info, err := os.Stat(path)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return position, err
	}
	if info != nil {
		seekFrom := from
		if cap < ^uint64(0) && cap+1 < seekFrom {
			seekFrom = cap + 1
		}
		position = h.index.Before(path, info, seekFrom)
	}
	if from > 0 {
		if from-1 < cap {
			cap = from - 1
		}
		_, keyframe, err := latestKeyframe(filepath.Dir(path), cap, math.MaxInt32)
		if err != nil {
			return position, err
		}
		if keyframe.Offset > 0 && (info == nil || keyframe.Offset > info.Size()) {
			return position, status.Error(codes.DataLoss, "keyframe offset exceeds run.log")
		}
		if keyframe.ThroughSeq > position.ThroughSeq {
			position = keyframe
		}
	}
	return position, nil
}
