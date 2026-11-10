package filestore

import (
	"context"
	"errors"
	"path"
	"slices"
	"strconv"
	"sync"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// allocationsDir is the directory of the allocation log, beside the index's tasks directory:
// allocations/<n>.json is the nth input, as an AllocationLogFile, written once and never changed.
// A restart finds the inputs by listing it, and they are contiguous from 1: a gap is a record
// that was lost, which Load refuses to read past.
const allocationsDir = "allocations"

// AllocLog is the store.AllocLog over files, in the directory of the per-user index. Create it with
// OpenAllocLog.
type AllocLog struct {
	mu   sync.Mutex
	b    *backend
	dir  string
	next uint64 // The place the next input takes; zero until the directory was listed.
}

var _ store.AllocLog = (*AllocLog)(nil)

// OpenAllocLog opens the allocation log in the directory dir, making it when it is missing. It is
// the directory of the index, which is the user's, so one daemon is the log's only writer.
func OpenAllocLog(dir string, opts ...Option) (*AllocLog, error) {
	c := optionsOf(opts)
	dir = path.Clean(dir)
	var made []string
	if err := makeAll(c.fs, path.Join(dir, allocationsDir), &made); err != nil {
		return nil, unavailable(dir, err)
	}
	return &AllocLog{b: newBackend(c.fs, dir), dir: dir}, nil
}

func (l *AllocLog) file(n uint64) string {
	return path.Join(l.dir, allocationsDir, strconv.FormatUint(n, 10)+recordSuffix)
}

// list returns the inputs' places, ascending, refusing a log with a gap or a name it does not know.
func (l *AllocLog) list() ([]uint64, error) {
	entries, err := l.b.fs.ReadDir(path.Join(l.dir, allocationsDir))
	if err != nil {
		return nil, unavailable(l.dir, err)
	}
	var places []uint64
	for _, e := range entries {
		if hidden(e.Name) {
			continue
		}
		name, ok := recordName(e.Name)
		n, canonical := canonicalNumber(name)
		if e.IsDir || !ok || !canonical {
			return nil, corrupt(path.Join(l.dir, allocationsDir, e.Name), "it is not an input of the allocation log")
		}
		places = append(places, n)
	}
	slices.Sort(places)
	for i, n := range places {
		if n != uint64(i)+1 {
			return nil, corrupt(l.file(uint64(i)+1), "the allocation log has no input %d, and has input %d", i+1, n)
		}
	}
	return places, nil
}

// Append implements store.AllocLog.
func (l *AllocLog) Append(ctx context.Context, in alloc.Input) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	m, err := wire.FromAllocationInput(in)
	if err != nil {
		return err
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.next == 0 {
		places, err := l.list()
		if err != nil {
			return err
		}
		l.next = uint64(len(places)) + 1
	}
	if err := l.b.publish(l.file(l.next), &cliv1.AllocationLogFile{Format: formatVersion, Seq: l.next, Input: m}); err != nil {
		var f *core.Failure
		if errors.As(err, &f) {
			return err
		}
		return unavailable(l.dir, err)
	}
	l.next++
	return nil
}

// Load implements store.AllocLog.
func (l *AllocLog) Load(ctx context.Context) ([]alloc.Input, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	places, err := l.list()
	if err != nil {
		return nil, err
	}
	out := make([]alloc.Input, 0, len(places))
	for _, n := range places {
		name := l.file(n)
		data, err := l.b.fs.ReadFile(name)
		if err != nil {
			return nil, unavailable(l.dir, err)
		}
		var file cliv1.AllocationLogFile
		if err := decode(name, data, &file); err != nil {
			return nil, err
		}
		if file.GetSeq() != n {
			return nil, corrupt(name, "it says it is input %d", file.GetSeq())
		}
		in, err := wire.ToAllocationInput(file.GetInput())
		if err != nil {
			return nil, corrupt(name, "%v", err)
		}
		out = append(out, in)
	}
	l.next = uint64(len(places)) + 1
	return out, nil
}
