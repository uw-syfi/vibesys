package runtimedir

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/encoding/protojson"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// DrainFile is where the daemon keeps whether it drains: the protojson of a DaemonDrain while it
// drains, and no file when it does not. It is in the runtime directory, so a daemon that restarts
// in the same directory drains as the last one did; a reboot that empties the directory ends it,
// as it ends every command the daemon ran.
func (d Dir) DrainFile() string { return filepath.Join(d.path, "drain.json") }

// ReadDrain returns the drain the directory keeps, or nil when the daemon does not drain. A file
// that does not decode as a valid DaemonDrain is an error that names it, so a damaged file is never
// read as a daemon that does not drain.
func (d Dir) ReadDrain() (*cliv1.DaemonDrain, error) {
	data, err := os.ReadFile(d.DrainFile())
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	out := &cliv1.DaemonDrain{}
	if err := protojson.Unmarshal(data, out); err != nil {
		return nil, fmt.Errorf("%s is damaged: %w; remove it to stop draining", d.DrainFile(), err)
	}
	if err := protovalidate.Validate(out); err != nil {
		return nil, fmt.Errorf("%s is damaged: %w; remove it to stop draining", d.DrainFile(), err)
	}
	return out, nil
}

// WriteDrain keeps a drain, or with nil removes it. A drain is written to a file of its own and
// renamed over the last, and the directory is synced, so a crash leaves the old drain or the new
// one, never half of one.
func (d Dir) WriteDrain(drain *cliv1.DaemonDrain) error {
	name := d.DrainFile()
	if drain == nil {
		if err := os.Remove(name); err != nil && !errors.Is(err, fs.ErrNotExist) {
			return err
		}
		return syncDir(d.path)
	}
	if err := protovalidate.Validate(drain); err != nil {
		return err
	}
	data, err := protojson.Marshal(drain)
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(d.path, ".drain-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmp.Name(), name); err != nil {
		return err
	}
	return syncDir(d.path)
}

func syncDir(path string) error {
	dir, err := os.Open(path)
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}
