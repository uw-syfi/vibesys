package host

import (
	"errors"
	"io"
	"os"

	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// fileSpool is a supervisor.Spool in a file that only the supervisor appends to.
type fileSpool struct{ f *os.File }

var _ supervisor.Spool = fileSpool{}

func openSpool(path string) (fileSpool, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR|os.O_APPEND, 0o600)
	return fileSpool{f: f}, err
}

func (s fileSpool) Append(data []byte) error {
	_, err := s.f.Write(data)
	return err
}

func (s fileSpool) ReadAt(p []byte, offset uint64) (int, error) {
	n, err := s.f.ReadAt(p, int64(offset))
	if errors.Is(err, io.EOF) && n > 0 {
		return n, nil
	}
	return n, err
}

func (s fileSpool) close() error { return s.f.Close() }
