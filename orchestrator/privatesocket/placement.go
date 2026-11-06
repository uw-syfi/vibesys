package privatesocket

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"syscall"
)

// MaxPath is the longest socket path a state or root directory gets to keep its sockets in. A Unix
// socket address holds 104 bytes on macOS and 108 on Linux with its terminating NUL; one limit for
// both means a directory behaves the same on either.
const MaxPath = 100

// Fits says whether a socket path is short enough to be kept where it is.
func Fits(path string) bool { return len(path) <= MaxPath }

// Directory is the private directory of this user that holds the sockets whose natural path does not
// fit. /tmp is written out because $TMPDIR differs between shells and on macOS is too long itself.
func Directory() string { return "/tmp/vibesys-" + strconv.Itoa(os.Getuid()) }

// Short is the short path for the socket of owner, a string that names it for good: the same on
// every call, in every process, with nothing stored. Two owners never share one.
func Short(owner string) string {
	sum := sha256.Sum256([]byte(owner))
	return filepath.Join(Directory(), hex.EncodeToString(sum[:16])+".sock")
}

// Place is where the socket whose natural path is natural goes: there when it fits, and otherwise
// the short path derived from natural. It is a function of natural alone, so the host that binds the
// socket and whatever else asks where it is agree without either storing anything.
func Place(natural string) string {
	if Fits(natural) {
		return natural
	}
	return Short(natural)
}

// EnsureDirectory makes the directory of socket if it is the private one, with mode 0700, and checks
// it. Any other directory is the caller's.
func EnsureDirectory(socket string) error {
	if filepath.Dir(socket) == Directory() {
		if err := os.MkdirAll(Directory(), 0o700); err != nil {
			return err
		}
	}
	return CheckDirectory(socket)
}

// CheckDirectory refuses a socket whose directory another user could have made, which is only
// possible for the private directory. Anything that reaches for a socket checks this first: whoever
// owns that directory could otherwise answer as the host. A directory that does not exist yet is
// fine, because nobody can be listening.
func CheckDirectory(socket string) error {
	directory := filepath.Dir(socket)
	if directory != Directory() {
		return nil
	}
	info, err := os.Lstat(directory)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !info.IsDir() || !ok || int(stat.Uid) != os.Getuid() || info.Mode().Perm()&0o077 != 0 {
		return fmt.Errorf("refusing the socket directory %s: it must be a directory of this user that nobody else can reach", directory)
	}
	return nil
}
