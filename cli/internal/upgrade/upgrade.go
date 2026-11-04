package upgrade

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// Limits on what a release may ask the client to read. They guard against a runaway response;
// a release within them is never refused for size.
const (
	maxTextBytes   = 1 << 20
	maxBinaryBytes = 512 << 20
)

// Errors a caller can tell apart.
var (
	// ErrChecksumMismatch means the downloaded binary is not the one the release published. A
	// truncated or corrupted download ends here.
	ErrChecksumMismatch = errors.New("downloaded binary does not match the published checksum")
	// ErrNoChecksum means checksums.txt has no entry for this platform's binary.
	ErrNoChecksum = errors.New("no published checksum for this platform's binary")
)

// Outcome says what Upgrade did.
type Outcome string

// The outcomes.
const (
	// UpToDate means the installed version is already the newest, and nothing was downloaded.
	UpToDate Outcome = "up_to_date"
	// Upgraded means the binary at Target now is the newest release.
	Upgraded Outcome = "upgraded"
)

// Request is one upgrade.
type Request struct {
	// Source is where the newest release is read from.
	Source Source
	// Platform picks the binary to install.
	Platform Platform
	// Current is the version of the running binary, as --version reports it.
	Current string
	// Target is the path of the binary to replace. It must be the real file, not a symlink to it.
	Target string
}

// Result is what an Upgrade did.
type Result struct {
	Outcome Outcome
	// From is the version that was installed before.
	From string
	// To is the newest release's version.
	To string
}

// Upgrade installs the newest release over Target, or reports that Target is already current.
//
// The new binary is staged in Target's directory, so the final rename never crosses a file
// system, and it is renamed into place only after its SHA-256 equals the digest published in
// checksums.txt. A download cut short, corrupted, or from a release without a checksum for this
// platform is refused, and Target and its directory are left as they were.
func Upgrade(ctx context.Context, request Request) (Result, error) {
	asset, err := AssetName(request.Platform)
	if err != nil {
		return Result{}, err
	}
	versionText, err := readAsset(ctx, request.Source, VersionAsset, maxTextBytes)
	if err != nil {
		return Result{}, err
	}
	latest, err := ParseVersion(string(versionText))
	if err != nil {
		return Result{}, err
	}
	result := Result{Outcome: UpToDate, From: request.Current, To: latest}
	if latest == request.Current {
		return result, nil
	}
	checksumsText, err := readAsset(ctx, request.Source, ChecksumsAsset, maxTextBytes)
	if err != nil {
		return Result{}, err
	}
	sums, err := ParseChecksums(string(checksumsText))
	if err != nil {
		return Result{}, err
	}
	want, ok := sums[asset]
	if !ok {
		return Result{}, fmt.Errorf("%w: %s", ErrNoChecksum, asset)
	}
	if err := install(ctx, request, asset, want); err != nil {
		return Result{}, err
	}
	result.Outcome = Upgraded
	return result, nil
}

func readAsset(ctx context.Context, source Source, asset string, limit int64) ([]byte, error) {
	body, err := source.Open(ctx, asset)
	if err != nil {
		return nil, err
	}
	defer func() { _ = body.Close() }()
	data, err := io.ReadAll(io.LimitReader(body, limit+1))
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", asset, err)
	}
	if int64(len(data)) > limit {
		return nil, fmt.Errorf("%s is larger than %d bytes", asset, limit)
	}
	return data, nil
}

// install stages the asset next to Target, checks it against want, and renames it over Target.
func install(ctx context.Context, request Request, asset, want string) (err error) {
	body, err := request.Source.Open(ctx, asset)
	if err != nil {
		return err
	}
	defer func() { _ = body.Close() }()

	directory := filepath.Dir(request.Target)
	staged, err := os.CreateTemp(directory, ".vibesys-cli-upgrade-*")
	if err != nil {
		return fmt.Errorf("cannot stage the new binary in %s (is the directory writable? reinstall with install.sh into a directory you own): %w", directory, err)
	}
	defer func() {
		if err != nil {
			_ = staged.Close()
			_ = os.Remove(staged.Name())
		}
	}()

	digest := sha256.New()
	copied, err := io.Copy(io.MultiWriter(staged, digest), io.LimitReader(body, maxBinaryBytes+1))
	if err != nil {
		return fmt.Errorf("download %s: %w", asset, err)
	}
	if copied > maxBinaryBytes {
		return fmt.Errorf("%s is larger than %d bytes", asset, int64(maxBinaryBytes))
	}
	if copied == 0 {
		return fmt.Errorf("%w: %s is empty", ErrChecksumMismatch, asset)
	}
	if got := hex.EncodeToString(digest.Sum(nil)); got != want {
		return fmt.Errorf("%w: %s has sha256 %s, published %s", ErrChecksumMismatch, asset, got, want)
	}
	if err := staged.Chmod(0o755); err != nil {
		return fmt.Errorf("make the new binary executable: %w", err)
	}
	if err := staged.Sync(); err != nil {
		return fmt.Errorf("flush the new binary: %w", err)
	}
	if err := staged.Close(); err != nil {
		return fmt.Errorf("close the new binary: %w", err)
	}
	if err := os.Rename(staged.Name(), request.Target); err != nil {
		return fmt.Errorf("replace %s: %w", request.Target, err)
	}
	syncDirectory(directory)
	return nil
}

// syncDirectory makes the rename durable. It is best effort: the rename already happened and is
// atomic, so a directory that cannot be synced leaves a correct installation, only one a crash
// could roll back to the previous binary.
func syncDirectory(directory string) {
	handle, err := os.Open(directory)
	if err != nil {
		return
	}
	_ = handle.Sync()
	_ = handle.Close()
}
