package server

import (
	"errors"
	"io"

	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// inputUpgradeAt seeds the one store upgrade at the reader's frozen checkpoint.
func inputUpgradeAt(dir string, position runlog.Position) (*runlog.InputUpgrade, error) {
	data, base, err := latestKeyframe(dir, position.ThroughSeq, maxStoredKeyframe)
	if status.Code(err) == codes.ResourceExhausted {
		data = nil
		base = runlog.Position{}
		err = nil
	}
	if err != nil {
		return nil, err
	}
	state := &storev1.Keyframe{}
	if len(data) > 0 {
		if err := proto.Unmarshal(data, state); err != nil {
			return nil, err
		}
	}
	upgrade := runlog.NewInputUpgrade(state)
	if base.ThroughSeq >= position.ThroughSeq {
		return upgrade, nil
	}
	f, err := openRegular(dir, "run.log")
	if err != nil {
		return nil, err
	}
	defer f.Close()
	reader := runlog.NewReaderAt(io.NewSectionReader(f, 0, position.Offset), base.Offset)
	for {
		payload, err := reader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, err
		}
		if _, err := upgrade.Record(payload); err != nil {
			return nil, err
		}
	}
	return upgrade, nil
}
