package filestore

import (
	"errors"
	"io/fs"
	"path"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// identityFile is the name of the daemon's identity in the directory of the per-user index.
const identityFile = "daemon-id" + recordSuffix

// DaemonID returns who this user's daemon is, made once: the identity kept in the directory of the
// index, or, when there is none, one that newID makes and the directory then keeps, so every later
// start of the daemon, and every Allocator it makes, has the same one. newID must return 16 to 32
// characters of a-z and 0-9.
func DaemonID(dir string, newID func() string, opts ...Option) (string, error) {
	c := optionsOf(opts)
	dir = path.Clean(dir)
	var made []string
	if err := makeAll(c.fs, dir, &made); err != nil {
		return "", unavailable(dir, err)
	}
	b := newBackend(c.fs, dir)
	name := path.Join(dir, identityFile)
	data, err := c.fs.ReadFile(name)
	switch {
	case err == nil:
		var file cliv1.DaemonIdentity
		if err := decode(name, data, &file); err != nil {
			return "", err
		}
		return file.GetId(), nil
	case !errors.Is(err, fs.ErrNotExist):
		return "", unavailable(dir, err)
	}
	id := newID()
	if err := b.publish(name, &cliv1.DaemonIdentity{Format: formatVersion, Id: id}); err != nil {
		return "", unavailable(dir, err)
	}
	return id, nil
}
