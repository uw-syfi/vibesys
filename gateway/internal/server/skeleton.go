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

// maxStoredKeyframe bounds what the gateway reads to rewrite. A stored keyframe
// is larger than its wire form, which names descriptors by hash.
const maxStoredKeyframe = 16 << 20

// keyframeCandidates bounds how many older keyframes a reply tries when the
// newest does not fit; each older one is smaller.
const keyframeCandidates = 4

func (h *host) Skeleton(ctx context.Context, request *apiv1.SkeletonRequest) (*apiv1.SkeletonResponse, error) {
	dir, err := h.directory(request.GetRunId())
	if err != nil {
		return nil, err
	}
	cap, bounded, err := committed(dir)
	if err != nil {
		return nil, rpcError(err)
	}
	sessionIDs, err := sessions(dir)
	if err != nil {
		return nil, rpcError(err)
	}
	listingBytes := proto.Size(&apiv1.SkeletonResponse{Sessions: sessionIDs})
	if listingBytes+32 > skeletonBytes {
		return nil, status.Error(codes.ResourceExhausted, "session listing exceeds skeleton budget")
	}
	data, position, sets, err := wireKeyframe(dir, cap, request.GetKnownDescriptorsSha256(), sessionIDs)
	if err != nil {
		return nil, rpcError(err)
	}
	response := &apiv1.SkeletonResponse{Keyframe: data, DescriptorSets: sets, CommittedSeq: position.ThroughSeq, NextSeq: position.ThroughSeq + 1, Complete: true, Sessions: sessionIDs}
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
	size := proto.Size(&apiv1.SkeletonResponse{Keyframe: data, DescriptorSets: sets, Sessions: sessionIDs}) + 24
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

// wireKeyframe picks the newest keyframe whose wire form fits the reply beside
// the session listing, naming descriptors by hash and sending the sets the
// client did not list. No keyframe fits: the client replays from the log.
func wireKeyframe(dir string, cap uint64, known []string, sessionIDs []string) ([]byte, runlog.Position, []*apiv1.DescriptorSet, error) {
	seqs, err := keyframeSeqs(dir, cap)
	if err != nil {
		return nil, runlog.Position{}, nil, err
	}
	if len(seqs) > keyframeCandidates {
		seqs = seqs[:keyframeCandidates]
	}
	listed := make(map[string]bool, len(known))
	for _, hash := range known {
		listed[hash] = true
	}
	for _, seq := range seqs {
		stored, position, err := readKeyframe(dir, seq, maxStoredKeyframe)
		if status.Code(err) == codes.ResourceExhausted {
			continue
		}
		if err != nil {
			return nil, runlog.Position{}, nil, err
		}
		wire, addressed, err := addressDescriptors(stored)
		if err != nil {
			return nil, runlog.Position{}, nil, err
		}
		var sent []*apiv1.DescriptorSet
		for _, hash := range addressed.order {
			if !listed[hash] {
				sent = append(sent, &apiv1.DescriptorSet{Sha256: hash, Data: addressed.data[hash]})
			}
		}
		reply := &apiv1.SkeletonResponse{Keyframe: wire, DescriptorSets: sent, Sessions: sessionIDs}
		if proto.Size(reply)+32 <= skeletonBytes {
			return wire, position, sent, nil
		}
	}
	return nil, runlog.Position{}, nil, nil
}
