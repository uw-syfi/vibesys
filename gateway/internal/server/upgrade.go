package server

import (
	"io"
	"os"
	"path/filepath"

	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// inputUpgradeAt seeds the one store upgrade at the reader's frozen checkpoint.
func inputUpgradeAt(dir string, position runlog.Position) (*runlog.InputUpgrade, error) {
	projection, err := projectionAt(dir, position)
	if err != nil {
		return nil, err
	}
	return runlog.NewInputUpgrade(projection.State), nil
}

// projectionAt restores metadata once at a follower's cursor, then folds only appends.
func projectionAt(dir string, position runlog.Position) (*runlog.Projection, error) {
	if position.ThroughSeq == 0 && position.Offset == 0 {
		return runlog.NewProjection(), nil
	}
	f, err := openRegular(dir, "run.log")
	if err != nil {
		return nil, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if position.Offset > info.Size() {
		return nil, &runlog.Corruption{Offset: position.Offset, Reason: "cursor offset exceeds run.log"}
	}
	return projectionFromFile(f, position.Offset, &position.ThroughSeq)
}

func projectionFromFile(f *os.File, size int64, committed *uint64) (*runlog.Projection, error) {
	cap := ^uint64(0)
	if committed != nil {
		cap = *committed
	}
	data, base, err := latestKeyframe(filepath.Dir(f.Name()), cap, maxStoredKeyframe)
	if status.Code(err) == codes.ResourceExhausted {
		data, base, err = nil, runlog.Position{}, nil
	}
	if err != nil {
		return nil, err
	}
	if base.Offset > size {
		return nil, &runlog.Corruption{Offset: base.Offset, Reason: "keyframe offset exceeds run.log"}
	}
	state := &storev1.Keyframe{}
	if len(data) > 0 {
		if err := proto.Unmarshal(data, state); err != nil {
			return nil, &runlog.Corruption{Offset: base.Offset, Reason: err.Error()}
		}
		if _, err := runlog.UpgradeSkeleton(state); err != nil {
			return nil, &runlog.Corruption{Offset: -1, Reason: err.Error()}
		}
	}
	projection := runlog.NewProjectionFrom(state)
	err = runlog.ReadPrefixAt(io.NewSectionReader(f, 0, size), base, committed, projection.Record)
	return projection, err
}
