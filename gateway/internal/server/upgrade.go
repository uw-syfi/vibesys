package server

import (
	"errors"
	"io"
	"os"

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

func retainedInput(dir string, head *storev1.RunHead) error {
	if head.GetObjective() == nil || head.Objective.InputValue != nil {
		return nil
	}
	f, err := openRegular(dir, "run.log")
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return err
	}
	reader := runlog.NewReader(io.NewSectionReader(f, 0, info.Size()))
	upgrade := runlog.NewInputUpgrade(nil)
	var through uint64
	seen := false
	if head.CommittedSeq != nil && head.GetCommittedSeq() == 0 {
		return nil
	}
	for {
		payload, err := reader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return err
		}
		seq, err := runlog.Sequence(payload)
		if err != nil {
			return err
		}
		if head.CommittedSeq != nil && seq > head.GetCommittedSeq() {
			break
		}
		if seen && seq <= through {
			return &runlog.Corruption{Offset: -1, Reason: "non-increasing Record.seq"}
		}
		through, seen = seq, true
		if _, err := upgrade.Record(payload); err != nil {
			return err
		}
		if head.CommittedSeq != nil && seq == head.GetCommittedSeq() {
			break
		}
	}
	if head.CommittedSeq != nil && through < head.GetCommittedSeq() {
		return &runlog.Corruption{Offset: -1, Reason: "committed sequence exceeds log prefix"}
	}
	if measured := upgrade.Measurement().GetMeasured(); measured != nil {
		head.Objective.InputValue = proto.Float64(measured.Value)
	}
	return nil
}
