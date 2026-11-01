package filestore

import (
	"errors"
	"fmt"
	"io/fs"
	"path"

	"github.com/uw-syfi/vibesys/cli/internal/store/ledger"
)

// Commit makes one change durable. Every change is published by one rename, which is its commit
// point: a crash before it leaves the store as it was, and a crash after it leaves the change
// whole. A change that adds a result to a job writes the result first and publishes the job
// last, because the job is what says the result belongs to the store.
func (b *backend) Commit(change ledger.Change) error {
	switch c := change.(type) {
	case ledger.PutJob:
		if c.Result != nil {
			file, err := savedToFile(*c.Result)
			if err != nil {
				return err
			}
			if err := b.publish(resultPath(b.root, c.Result.Result.Job), file); err != nil {
				return err
			}
		}
		rec, err := jobToRec(c.Job)
		if err != nil {
			return err
		}
		return b.publish(jobPath(b.root, c.Job.ID), jobFile{Format: formatVersion, Job: rec})
	case ledger.PutPlan:
		return b.publish(planPath(b.root, c.Plan.Number), planFile{Format: formatVersion, Plan: planToRec(c.Plan)})
	case ledger.AddPromotion:
		return b.publish(promotionPath(b.root, c.Seq), promotionFile{Format: formatVersion, Seq: c.Seq, Promotion: promotionToRec(c.Promotion)})
	case ledger.AddEvent:
		rec, err := eventToRec(c.Event)
		if err != nil {
			return err
		}
		if err := b.ensureDir(path.Join(b.root, runsDir), string(c.Event.Run)); err != nil {
			return err
		}
		return b.publish(eventPath(b.root, c.Event.Run, c.Event.Sequence), eventFile{Format: formatVersion, Event: rec})
	case ledger.AddLog:
		if err := b.ensureDir(path.Join(b.root, logsDir), string(c.Job)); err != nil {
			return err
		}
		if err := b.publishBytes(chunkPath(b.root, c.Job, c.Offset), c.Data); err != nil {
			return err
		}
		b.logs[c.Job] = append(b.logs[c.Job], chunk{offset: c.Offset, size: uint64(len(c.Data))})
		return nil
	}
	return fmt.Errorf("filestore: a change of type %T has no encoding", change)
}

// publish writes a record under its final name.
func (b *backend) publish(name string, file any) error {
	data, err := encode(file)
	if err != nil {
		return err
	}
	return b.publishBytes(name, data)
}

// publishBytes writes data to a temporary file in the directory of name, makes the file durable,
// renames it to name, and makes the directory durable. The rename replaces a file already at
// name in one step, so a reader and a crash see the old content or the new and never part of
// either. Every operation is one NFS keeps atomic or leaves without effect: no append, no write
// to a published file.
func (b *backend) publishBytes(name string, data []byte) error {
	dir := path.Dir(name)
	tmp := path.Join(dir, fmt.Sprintf("%s%d", tempPrefix, b.temps))
	b.temps++
	if err := b.writeFile(tmp, data); err != nil {
		_ = b.fs.Remove(tmp)
		return err
	}
	if err := b.fs.Rename(tmp, name); err != nil {
		_ = b.fs.Remove(tmp)
		return err
	}
	return b.fs.SyncDir(dir)
}

func (b *backend) writeFile(name string, data []byte) error {
	f, err := b.fs.Create(name)
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		return err
	}
	return f.Close()
}

// ensureDir makes the directory name under parent, and makes its entry durable, once per
// Recover: a directory found on disk may have been made by a run that died before it synced
// the parent, so the first write into it syncs again.
func (b *backend) ensureDir(parent, name string) error {
	dir := path.Join(parent, name)
	if b.durableDirs[dir] {
		return nil
	}
	if err := b.fs.Mkdir(dir); err != nil && !errors.Is(err, fs.ErrExist) {
		return err
	}
	if err := b.fs.SyncDir(parent); err != nil {
		return err
	}
	b.durableDirs[dir] = true
	return nil
}
