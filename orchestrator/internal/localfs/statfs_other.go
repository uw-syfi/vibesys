//go:build !linux && !darwin

package localfs

import "errors"

// SystemDetector cannot tell on this platform, so every path is refused: the orchestrator runs on
// Linux and macOS.
type SystemDetector struct{}

// Detect implements Detector.
func (SystemDetector) Detect(string) (Filesystem, error) {
	return Filesystem{}, errors.New("filesystem detection is not supported on this platform")
}
