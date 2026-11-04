package production

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// ErrHeld says another process holds the orchestrator's state directory. Its message names that
// process's pid and version, from the holder file it wrote.
var ErrHeld = errors.New("another process holds the orchestrator")

const holderFile = "holder"

// writeHolder records this process as the holder of the state directory. It is written only while
// the store's lock is held, so the file names the holder whenever the lock is taken.
func writeHolder(dir, version string) error {
	tmp := filepath.Join(dir, holderFile+".tmp")
	text := fmt.Sprintf("pid %d version %s\n", os.Getpid(), version)
	if err := os.WriteFile(tmp, []byte(text), 0o600); err != nil {
		return fmt.Errorf("write the holder file: %w", err)
	}
	return os.Rename(tmp, filepath.Join(dir, holderFile))
}

// ReadHolder describes the holder of a state directory for an error message.
func ReadHolder(dir string) string {
	data, err := os.ReadFile(filepath.Join(dir, holderFile))
	if err != nil {
		return "the holder of " + dir + " is unknown: " + err.Error()
	}
	return "held by " + strings.TrimSpace(string(data)) + " in " + dir
}
