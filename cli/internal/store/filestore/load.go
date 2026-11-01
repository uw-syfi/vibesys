package filestore

import (
	"fmt"
	"path"
	"slices"
	"sort"
	"strconv"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
	"github.com/uw-syfi/vibesys/cli/internal/store/ledger"
)

// Recover reads the durable state. It is what Open runs and what the Engine runs after a Commit
// failed, so a failed commit and a crash lead to the same code.
//
// A crash leaves two kinds of mess, and Recover handles both without being told which happened.
// A temporary file that was never renamed is removed. A result file whose job does not say it
// owns the result, because the crash came between the two renames of a finished job, belongs to
// no change that happened and is ignored; the next commit of that job writes it again.
func (b *backend) Recover() (ledger.Snapshot, error) {
	b.durableDirs = map[string]bool{}
	b.logs = map[core.JobID][]chunk{}
	var snap ledger.Snapshot
	jobs, err := b.loadJobs()
	if err != nil {
		return snap, err
	}
	snap.Jobs = jobs
	if snap.Results, err = b.loadResults(jobs); err != nil {
		return snap, err
	}
	if snap.Plans, err = b.loadPlans(); err != nil {
		return snap, err
	}
	if snap.Promotions, err = b.loadPromotions(); err != nil {
		return snap, err
	}
	if snap.Events, err = b.countEvents(); err != nil {
		return snap, err
	}
	if snap.Logs, err = b.indexLogs(); err != nil {
		return snap, err
	}
	return snap, nil
}

// list returns the entries of a directory that are records. The temporary files of a commit that
// did not finish are removed on the way, and so is anything else a program other than this one
// would have to have put in a dot file.
func (b *backend) list(dir string) ([]fsys.Entry, error) {
	entries, err := b.fs.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	var records []fsys.Entry
	for _, e := range entries {
		switch {
		case strings.HasPrefix(e.Name, tempPrefix):
			_ = b.fs.Remove(path.Join(dir, e.Name))
		case hidden(e.Name):
		default:
			records = append(records, e)
		}
	}
	return records, nil
}

func strayEntry(name string) error { return corrupt(name, "it is not a name this store writes") }

// readRecord reads and decodes one file.
func (b *backend) readRecord(name string, file any) error {
	data, err := b.fs.ReadFile(name)
	if err != nil {
		return err
	}
	return decode(name, data, file)
}

// recordFiles lists the record files of a directory with the number each is named for.
func (b *backend) recordFiles(dir string) (map[uint64]string, error) {
	entries, err := b.list(dir)
	if err != nil {
		return nil, err
	}
	out := make(map[uint64]string, len(entries))
	for _, e := range entries {
		full := path.Join(dir, e.Name)
		base, ok := recordName(e.Name)
		n, numbered := canonicalNumber(base)
		if e.IsDir || !ok || !numbered {
			return nil, strayEntry(full)
		}
		out[n] = full
	}
	return out, nil
}

func (b *backend) loadJobs() ([]core.Job, error) {
	dir := path.Join(b.root, jobsDir)
	entries, err := b.list(dir)
	if err != nil {
		return nil, err
	}
	jobs := make([]core.Job, 0, len(entries))
	for _, e := range entries {
		full := path.Join(dir, e.Name)
		base, ok := recordName(e.Name)
		if _, numbered := ledger.JobNumber(core.JobID(base)); e.IsDir || !ok || !numbered {
			return nil, strayEntry(full)
		}
		var file jobFile
		if err := b.readRecord(full, &file); err != nil {
			return nil, err
		}
		job, err := jobFromRec(file.Job)
		if err != nil {
			return nil, corrupt(full, "%v", err)
		}
		if string(job.ID) != base {
			return nil, corrupt(full, "it holds job %s", job.ID)
		}
		jobs = append(jobs, job)
	}
	return jobs, nil
}

func (b *backend) loadResults(jobs []core.Job) ([]ledger.SavedResult, error) {
	owner := make(map[core.JobID]bool, len(jobs))
	for _, job := range jobs {
		owner[job.ID] = job.ResultJob == job.ID
	}
	dir := path.Join(b.root, resultsDir)
	entries, err := b.list(dir)
	if err != nil {
		return nil, err
	}
	var results []ledger.SavedResult
	for _, e := range entries {
		full := path.Join(dir, e.Name)
		base, ok := recordName(e.Name)
		if _, numbered := ledger.JobNumber(core.JobID(base)); e.IsDir || !ok || !numbered {
			return nil, strayEntry(full)
		}
		if !owner[core.JobID(base)] {
			continue
		}
		var file resultFile
		if err := b.readRecord(full, &file); err != nil {
			return nil, err
		}
		result, err := resultFromRec(file.Result)
		if err != nil {
			return nil, corrupt(full, "%v", err)
		}
		if string(result.Job) != base || file.Seq == 0 {
			return nil, corrupt(full, "it holds the result of job %s, number %d", result.Job, file.Seq)
		}
		results = append(results, ledger.SavedResult{Seq: file.Seq, Result: result})
	}
	return results, nil
}

func (b *backend) loadPlans() ([]core.Plan, error) {
	files, err := b.recordFiles(path.Join(b.root, plansDir))
	if err != nil {
		return nil, err
	}
	plans := make([]core.Plan, 0, len(files))
	for _, n := range slices.Sorted(mapKeys(files)) {
		var file planFile
		if err := b.readRecord(files[n], &file); err != nil {
			return nil, err
		}
		if uint64(file.Plan.Number) != n {
			return nil, corrupt(files[n], "it holds plan %d", file.Plan.Number)
		}
		plans = append(plans, planFromRec(file.Plan))
	}
	return plans, nil
}

func (b *backend) loadPromotions() ([]core.Promotion, error) {
	files, err := b.recordFiles(path.Join(b.root, promotionsDir))
	if err != nil {
		return nil, err
	}
	promotions := make([]core.Promotion, 0, len(files))
	for n := uint64(1); n <= uint64(len(files)); n++ {
		name, ok := files[n]
		if !ok {
			return nil, corrupt(promotionPath(b.root, n), "it is missing, and later promotions are there")
		}
		var file promotionFile
		if err := b.readRecord(name, &file); err != nil {
			return nil, err
		}
		if file.Seq != n {
			return nil, corrupt(name, "it holds promotion %d", file.Seq)
		}
		promotions = append(promotions, promotionFromRec(file.Promotion))
	}
	return promotions, nil
}

// countEvents counts the events of each run by their file names. The events themselves are read
// when a caller asks for them, so opening a store costs nothing in the length of its history.
func (b *backend) countEvents() (map[core.RunID]uint64, error) {
	dir := path.Join(b.root, runsDir)
	runs, err := b.list(dir)
	if err != nil {
		return nil, err
	}
	counts := map[core.RunID]uint64{}
	for _, run := range runs {
		full := path.Join(dir, run.Name)
		if !run.IsDir {
			return nil, strayEntry(full)
		}
		files, err := b.recordFiles(full)
		if err != nil {
			return nil, err
		}
		for n := uint64(1); n <= uint64(len(files)); n++ {
			if _, ok := files[n]; !ok {
				return nil, corrupt(eventPath(b.root, core.RunID(run.Name), n), "it is missing, and later events of the run are there")
			}
		}
		if len(files) > 0 {
			counts[core.RunID(run.Name)] = uint64(len(files))
		}
	}
	return counts, nil
}

// indexLogs reads the sizes of every job's log chunks, checks that they follow one another with
// no gap, and keeps their places so that a read opens only the chunks it needs.
func (b *backend) indexLogs() (map[core.JobID]uint64, error) {
	dir := path.Join(b.root, logsDir)
	jobs, err := b.list(dir)
	if err != nil {
		return nil, err
	}
	lengths := map[core.JobID]uint64{}
	for _, job := range jobs {
		full := path.Join(dir, job.Name)
		if !job.IsDir {
			return nil, strayEntry(full)
		}
		chunks, err := b.list(full)
		if err != nil {
			return nil, err
		}
		var total uint64
		var index []chunk
		for _, c := range chunks {
			offset, err := strconv.ParseUint(c.Name, 10, 64)
			if err != nil || chunkName(offset) != c.Name || c.IsDir || c.Size == 0 {
				return nil, strayEntry(path.Join(full, c.Name))
			}
			if offset != total {
				return nil, corrupt(path.Join(full, c.Name), "the log has %d bytes before it, not %d", total, offset)
			}
			index = append(index, chunk{offset: offset, size: uint64(c.Size)})
			total += uint64(c.Size)
		}
		if total > 0 {
			lengths[core.JobID(job.Name)] = total
			b.logs[core.JobID(job.Name)] = index
		}
	}
	return lengths, nil
}

func mapKeys[V any](m map[uint64]V) func(yield func(uint64) bool) {
	return func(yield func(uint64) bool) {
		for k := range m {
			if !yield(k) {
				return
			}
		}
	}
}

// Events implements ledger.Backend.
func (b *backend) Events(run core.RunID, first, last uint64) ([]core.Event, error) {
	events := make([]core.Event, 0, last-first+1)
	for seq := first; seq <= last; seq++ {
		name := eventPath(b.root, run, seq)
		var file eventFile
		if err := b.readRecord(name, &file); err != nil {
			return nil, err
		}
		event, err := eventFromRec(file.Event)
		if err != nil {
			return nil, corrupt(name, "%v", err)
		}
		if event.Sequence != seq || event.Run != run {
			return nil, corrupt(name, "it holds event %d of run %s", event.Sequence, event.Run)
		}
		events = append(events, event)
	}
	return events, nil
}

// Log implements ledger.Backend.
func (b *backend) Log(job core.JobID, from, to uint64) ([]byte, error) {
	chunks := b.logs[job]
	i := sort.Search(len(chunks), func(i int) bool { return chunks[i].offset+chunks[i].size > from })
	out := make([]byte, 0, to-from)
	for ; i < len(chunks) && chunks[i].offset < to; i++ {
		c := chunks[i]
		name := chunkPath(b.root, job, c.offset)
		data, err := b.fs.ReadFile(name)
		if err != nil {
			return nil, err
		}
		if uint64(len(data)) != c.size {
			return nil, corrupt(name, "it has %d bytes, and the log expects %d", len(data), c.size)
		}
		lo, hi := max(from, c.offset)-c.offset, min(to, c.offset+c.size)-c.offset
		out = append(out, data[lo:hi]...)
	}
	if uint64(len(out)) != to-from {
		return nil, &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("the log of job %s has %d of the %d bytes asked for", job, len(out), to-from)}
	}
	return out, nil
}
