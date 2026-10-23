package server

import (
	"buf.build/go/protovalidate"
	"errors"
	"io"
	"os"
	"sync"
	"syscall"

	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// observedMemoCapacity bounds the recovered facts a host keeps in memory.
const observedMemoCapacity = 1024

// headObserver reads run heads for observation. A head states its epoch and whether the
// input is measured when its writer knows them. Only a head written without them needs
// the log, and what the log says about a committed prefix never changes, so each prefix
// is recovered once. Content readers use the raw commit fence and never come here.
type headObserver struct {
	mu    sync.Mutex
	memo  map[observedPrefix]recoveredHead
	order []observedPrefix
	scans int
}

// observedPrefix identifies the committed prefix of one log file, by what a rewrite,
// replacement or growth of the file would change.
type observedPrefix struct {
	device, inode uint64
	modifiedNs    int64
	size          int64
	committed     uint64
	fenced        bool
}

type recoveredHead struct {
	epoch uint32
	input *float64
}

// read returns the head as stored, plus the canonical epoch and the input value of a previous
// writer's head. An error concerns this run only; callers keep serving others.
func (o *headObserver) read(dir string) (*storev1.RunHead, error) {
	head, err := readHead(dir)
	if err != nil || head == nil {
		return head, err
	}
	// The stream validates what it sends, so an invalid head must be this run's error.
	if err := protovalidate.Validate(head); err != nil {
		return nil, status.Errorf(codes.DataLoss, "invalid run.head: %v", err)
	}
	wantsEpoch := head.Epoch == 0 && (head.CommittedSeq == nil || head.GetCommittedSeq() != 0)
	wantsInput := head.GetObjective() != nil && !head.Objective.InputStated && head.Objective.InputValue == nil &&
		(head.CommittedSeq == nil || head.GetCommittedSeq() != 0)
	if !wantsEpoch && !wantsInput {
		return head, nil
	}
	f, err := openRegular(dir, "run.log")
	if errors.Is(err, os.ErrNotExist) {
		// A head can precede log creation. Its facts are still unknown.
		return head, nil
	}
	if err != nil {
		return nil, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	recovered, err := o.recovered(f, info, head)
	if err != nil {
		return nil, status.Errorf(codes.DataLoss, "invalid retained prefix: %v", err)
	}
	if wantsEpoch {
		head.Epoch = recovered.epoch
	}
	if wantsInput && recovered.input != nil {
		head.Objective.InputValue = proto.Float64(*recovered.input)
	}
	return head, nil
}

func (o *headObserver) recovered(f *os.File, info os.FileInfo, head *storev1.RunHead) (recoveredHead, error) {
	key := observedPrefix{modifiedNs: info.ModTime().UnixNano(), size: info.Size()}
	if stat, ok := info.Sys().(*syscall.Stat_t); ok {
		key.device, key.inode = uint64(stat.Dev), uint64(stat.Ino)
	}
	if head.CommittedSeq != nil {
		key.committed, key.fenced = head.GetCommittedSeq(), true
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if known, ok := o.memo[key]; ok {
		return known, nil
	}
	o.scans++
	var epoch uint32
	upgrade := runlog.NewInputUpgrade(nil)
	err := runlog.ReadPrefix(io.NewSectionReader(f, 0, info.Size()), head.CommittedSeq, func(payload []byte) error {
		declared, err := runlog.DeclaredEpoch(payload)
		if err != nil {
			return err
		}
		epoch = max(epoch, declared)
		_, err = upgrade.Record(payload)
		return err
	})
	if err != nil {
		return recoveredHead{}, err
	}
	result := recoveredHead{epoch: epoch}
	if measured := upgrade.Measurement().GetMeasured(); measured != nil {
		result.input = proto.Float64(measured.Value)
	}
	if o.memo == nil {
		o.memo = map[observedPrefix]recoveredHead{}
	}
	o.memo[key] = result
	o.order = append(o.order, key)
	if len(o.order) > observedMemoCapacity {
		delete(o.memo, o.order[0])
		o.order = o.order[1:]
	}
	return result, nil
}
