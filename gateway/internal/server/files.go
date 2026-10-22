package server

import (
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"syscall"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runindex"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

// Blobs have a 16 MiB content budget, excluding response metadata.
const blobBytes = 16 << 20

func (h *host) directory(id string) (string, error) {
	path, err := runindex.Resolve(h.config.Root, id)
	if err != nil {
		return "", rpcError(err)
	}
	return filepath.Dir(path), nil
}

// OpenRoot confines content paths even when a file or ancestor is a symlink.
// Nonblocking open also prevents a malicious FIFO from hanging an RPC.
func openRegular(dir, path string) (*os.File, error) {
	root, err := os.OpenRoot(dir)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	f, err := root.OpenFile(path, os.O_RDONLY|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, err
	}
	info, err := f.Stat()
	if err == nil && !info.Mode().IsRegular() {
		err = status.Error(codes.InvalidArgument, "expected a regular file: "+path)
	}
	if err != nil {
		f.Close()
		return nil, err
	}
	return f, nil
}

func readFile(dir, path string, cap int) ([]byte, error) {
	f, err := openRegular(dir, path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, int64(cap)+1))
	if err != nil {
		return nil, err
	}
	if len(data) > cap {
		return nil, status.Errorf(codes.ResourceExhausted, "%s exceeds %d byte cap", path, cap)
	}
	return data, nil
}

func readHead(dir string) (*storev1.RunHead, error) {
	data, err := readFile(dir, "run.head", 1<<20)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	head := &storev1.RunHead{}
	if err := proto.Unmarshal(data, head); err != nil {
		return nil, status.Errorf(codes.DataLoss, "invalid run.head: %v", err)
	}
	hasLifecycle := head.Lifecycle != nil && head.Lifecycle.State != nil
	if hasLifecycle == head.AwaitingLifecycle || (head.AwaitingLifecycle && (head.Epoch == 0 || head.CommittedSeq == nil)) {
		return nil, status.Error(codes.DataLoss, "run.head requires lifecycle or an explicitly awaiting epoch")
	}
	return head, nil
}

func committed(dir string) (uint64, bool, error) {
	head, err := readHead(dir)
	if err != nil {
		return 0, false, err
	}
	if head == nil || head.CommittedSeq == nil {
		return math.MaxUint64, false, nil
	}
	return head.GetCommittedSeq(), true, nil
}

func liveness(dir string, head *storev1.RunHead) (apiv1.RunLiveness, error) {
	if head != nil && head.GetLifecycle().GetParked() != nil {
		return apiv1.RunLiveness_RUN_LIVENESS_PARKED, nil
	}
	if head != nil && head.GetLifecycle().GetEnded() != nil {
		return apiv1.RunLiveness_RUN_LIVENESS_ENDED, nil
	}
	f, err := openRegular(dir, "run.lock")
	if errors.Is(err, os.ErrNotExist) {
		if head == nil {
			return apiv1.RunLiveness_RUN_LIVENESS_UNSPECIFIED, nil
		}
		return apiv1.RunLiveness_RUN_LIVENESS_INTERRUPTED, nil
	}
	if err != nil {
		return 0, err
	}
	defer f.Close()
	// Writers hold LOCK_EX. Shared probes do not contend with one another, so
	// concurrent gateway readers cannot be mistaken for a live writer. They also
	// work with a read-only descriptor on filesystems that emulate flock via fcntl.
	err = syscall.Flock(int(f.Fd()), syscall.LOCK_SH|syscall.LOCK_NB)
	if errors.Is(err, syscall.EWOULDBLOCK) || errors.Is(err, syscall.EAGAIN) {
		return apiv1.RunLiveness_RUN_LIVENESS_ALIVE, nil
	}
	if err != nil {
		return 0, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_UN); err != nil {
		return 0, err
	}
	return apiv1.RunLiveness_RUN_LIVENESS_INTERRUPTED, nil
}

// Read only scalar keyframe headers, leaving the potentially large skeleton
// opaque. Field numbers come from the generated descriptor, not a second schema.
func keyframePosition(data []byte) (runlog.Position, error) {
	fields := (&storev1.Keyframe{}).ProtoReflect().Descriptor().Fields()
	seqField := protowire.Number(fields.ByName("through_seq").Number())
	offsetField := protowire.Number(fields.ByName("log_offset").Number())
	invalid := func() (runlog.Position, error) {
		return runlog.Position{}, status.Error(codes.DataLoss, "invalid keyframe header")
	}
	var seq, offset uint64
	for len(data) > 0 {
		number, kind, n := protowire.ConsumeTag(data)
		if n < 0 {
			return invalid()
		}
		data = data[n:]
		if number == seqField || number == offsetField {
			if kind != protowire.VarintType {
				return invalid()
			}
			value, n := protowire.ConsumeVarint(data)
			if n < 0 {
				return invalid()
			}
			data = data[n:]
			if number == seqField {
				seq = value
			} else {
				offset = value
			}
		} else {
			n := protowire.ConsumeFieldValue(number, kind, data)
			if n < 0 {
				return invalid()
			}
			data = data[n:]
		}
	}
	if seq == 0 || offset < uint64(len(runlog.Magic)) || offset > math.MaxInt64 {
		return invalid()
	}
	return runlog.Position{ThroughSeq: seq, Offset: int64(offset)}, nil
}

func latestKeyframe(dir string, cap uint64, maxBytes int) ([]byte, runlog.Position, error) {
	root, err := os.OpenRoot(dir)
	if err != nil {
		return nil, runlog.Position{}, err
	}
	defer root.Close()
	frames, err := root.Open("keyframes")
	if errors.Is(err, os.ErrNotExist) {
		return nil, runlog.Position{}, nil
	}
	if err != nil {
		return nil, runlog.Position{}, err
	}
	defer frames.Close()
	names, err := frames.Readdirnames(-1)
	if err != nil {
		return nil, runlog.Position{}, err
	}
	var best uint64
	for _, name := range names {
		if filepath.Ext(name) != ".pb" {
			continue
		}
		seq, err := strconv.ParseUint(name[:len(name)-3], 10, 64)
		if err != nil || name != strconv.FormatUint(seq, 10)+".pb" {
			continue
		}
		if seq <= cap && seq > best {
			best = seq
		}
	}
	if best == 0 {
		return nil, runlog.Position{}, nil
	}
	data, err := readFile(dir, fmt.Sprintf("keyframes/%d.pb", best), maxBytes)
	if err != nil {
		return nil, runlog.Position{}, err
	}
	position, err := keyframePosition(data)
	if err != nil {
		return nil, runlog.Position{}, err
	}
	if position.ThroughSeq != best {
		return nil, runlog.Position{}, status.Error(codes.DataLoss, "keyframe filename and through_seq differ")
	}
	return data, position, nil
}

var sessionID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`)

func (h *host) contentDirectory(runID, session string) (string, error) {
	dir, err := h.directory(runID)
	if err != nil || session == "" {
		return dir, err
	}
	if !sessionID.MatchString(session) {
		return "", status.Error(codes.InvalidArgument, "invalid session_id")
	}
	path := filepath.Join(dir, "sessions", session)
	// Session directories are owned by the session process, never symlink routes.
	for _, component := range []string{filepath.Join(dir, "sessions"), path} {
		info, err := os.Lstat(component)
		if err != nil {
			return "", rpcError(err)
		}
		if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return "", status.Error(codes.InvalidArgument, "session path must be a directory")
		}
	}
	return path, nil
}

func (h *host) logPath(runID, session string) (string, error) {
	dir, err := h.contentDirectory(runID, session)
	if err != nil {
		return "", err
	}
	name := "run.log"
	if session != "" {
		name = "session.log"
	}
	return filepath.Join(dir, name), nil
}

func sessions(dir string) ([]string, error) {
	entries, err := os.ReadDir(filepath.Join(dir, "sessions"))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	result := []string{}
	for _, entry := range entries {
		if entry.IsDir() && sessionID.MatchString(entry.Name()) {
			result = append(result, entry.Name())
		}
	}
	sort.Strings(result)
	return result, nil
}
