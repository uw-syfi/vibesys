package evaluator

import (
	"crypto/rand"
	"encoding/binary"
	"errors"
	"fmt"
	"math/big"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

// The files a sample leaves (docs/spec.md, "Files" and "Seeds"): `seeds.json` and one directory
// per phase in the artifacts directory, which vibesys-cli keeps with the job.
const (
	// SampleSeedEnv is the sample seed vibesys may pass; drawn fresh when unset.
	SampleSeedEnv = "VIBESYS_SAMPLE_SEED"
	// ArtifactsEnv is the directory vibesys keeps with the job.
	ArtifactsEnv = "VIBESYS_ARTIFACTS_DIR"
	// SeedsFile records the sample seed and where it came from, in the artifacts directory.
	SeedsFile = "seeds.json"
)

var phaseName = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,62}$`)

const (
	seedFromFlag  = "--seed"
	seedFromDrawn = "drawn"
)

// maxSeed is 2**64 - 1.
var maxSeed = new(big.Int).SetUint64(^uint64(0))

func seedRange(text string) (uint64, bool) {
	n, ok := new(big.Int).SetString(text, 0)
	if !ok || n.Sign() < 0 || n.Cmp(maxSeed) > 0 {
		return 0, false
	}
	return n.Uint64(), true
}

// SampleSeed is the sample seed and where it came from: the --seed flag (decimal text, "" when
// not given), the environment (env, as in $VIBESYS_SAMPLE_SEED, any Go or Python integer
// literal) or drawn at random.
func SampleSeed(flag string, hasFlag bool, env string) (seed uint64, source string, err error) {
	if hasFlag {
		n, ok := new(big.Int).SetString(strings.TrimSpace(flag), 10)
		if !ok || n.Sign() < 0 || n.Cmp(maxSeed) > 0 {
			return 0, "", fmt.Errorf("--seed must be in [0, 2**64), got %s", strings.TrimSpace(flag))
		}
		return n.Uint64(), seedFromFlag, nil
	}
	if value := strings.TrimSpace(env); value != "" {
		// Python's int(text, 0): a literal that starts with 0 is hex, octal or binary, or all zeros.
		seed, ok := uint64(0), false
		digits := strings.TrimLeft(value, "+-")
		leadingZero := len(digits) > 1 && digits[0] == '0' && !strings.ContainsRune("xXoObB", rune(digits[1]))
		if !leadingZero || strings.Trim(digits, "0_") == "" {
			seed, ok = seedRange(value)
		}
		if !ok {
			return 0, "", fmt.Errorf("$%s=%s is not an integer in [0, 2**64)", SampleSeedEnv, pyRepr(value))
		}
		return seed, SampleSeedEnv, nil
	}
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return 0, "", err
	}
	return binary.BigEndian.Uint64(b[:]) >> 1, seedFromDrawn, nil
}

// ArtifactsDir is the directory the sample keeps its files in, created: `envValue`
// ($VIBESYS_ARTIFACTS_DIR) when set, else `scratch/artifacts`.
func ArtifactsDir(envValue, scratch string) (string, error) {
	path := envValue
	if path == "" {
		path = filepath.Join(scratch, "artifacts")
	}
	return path, os.MkdirAll(path, 0o755)
}

// WriteSeeds records the seed in `seeds.json` of the artifacts directory.
func WriteSeeds(artifacts string, seed uint64, source string) error {
	text := `{"sample_seed": ` + strconv.FormatUint(seed, 10) + `, "source": ` + jsonString(source) + "}\n"
	return os.WriteFile(filepath.Join(artifacts, SeedsFile), []byte(text), 0o644)
}

// PhaseDir creates a new, empty directory for one phase's files, `<artifacts>/<name>`. The name
// is 1 to 63 of a-z, 0-9, '.', '_' and '-', starting with a letter or digit, and each phase of
// a sample has its own.
func PhaseDir(artifacts, name string) (string, error) {
	if !phaseName.MatchString(name) {
		return "", fmt.Errorf("phase name %s must be 1 to 63 of a-z, 0-9, '.', '_' and '-', starting with a letter or digit", pyRepr(name))
	}
	path := filepath.Join(artifacts, name)
	if err := os.MkdirAll(artifacts, 0o755); err != nil {
		return "", err
	}
	if err := os.Mkdir(path, 0o755); err != nil {
		if errors.Is(err, os.ErrExist) {
			return "", fmt.Errorf("phase name %s is used twice in this sample; give each phase its own", pyRepr(name))
		}
		return "", err
	}
	return path, nil
}
