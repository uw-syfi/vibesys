// Package hostconfig is a host's configuration: one HostConfig, as protobuf JSON, in the host's root.
// The host reads it when it starts, and vibesys-client reads it to tell whether a running host
// started with what the file now says. Nothing of the configuration comes from the environment of
// whoever started the host: Environ says what a host's environment is, whatever it inherited.
package hostconfig

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// Config is a host's configuration, for the programs that only hold one and pass it on.
type Config = pb.HostConfig

// IdentifyResponse is what a host says it is, likewise.
type IdentifyResponse = pb.IdentifyResponse

// fileName is the configuration's place in a root.
const fileName = "host-config.json"

// Path is where the configuration of the host of root is.
func Path(root string) string { return filepath.Join(root, fileName) }

// Load reads the configuration in root. A missing file is the empty configuration.
func Load(root string) (*pb.HostConfig, error) {
	data, err := os.ReadFile(Path(root))
	if errors.Is(err, os.ErrNotExist) {
		return &pb.HostConfig{}, nil
	}
	if err != nil {
		return nil, err
	}
	config, err := Parse(data)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", Path(root), err)
	}
	return config, nil
}

// Parse reads a configuration, refusing a field it does not know (the error names it) and any value
// the message's rules reject.
func Parse(data []byte) (*pb.HostConfig, error) {
	var config pb.HostConfig
	if err := protojson.Unmarshal(data, &config); err != nil {
		return nil, fmt.Errorf("not a host configuration: %w", err)
	}
	if err := protovalidate.Validate(&config); err != nil {
		return nil, fmt.Errorf("not a valid host configuration: %w", err)
	}
	return &config, nil
}

// Save puts the configuration in root in one atomic step. The empty configuration removes the file,
// since no file means the same thing.
func Save(root string, config *pb.HostConfig) error {
	if err := protovalidate.Validate(config); err != nil {
		return fmt.Errorf("not a valid host configuration: %w", err)
	}
	if proto.Size(config) == 0 {
		if err := os.Remove(Path(root)); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		return nil
	}
	data, err := protojson.MarshalOptions{Multiline: true}.Marshal(config)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(root, 0o700); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(root, "."+fileName+"-*")
	if err != nil {
		return err
	}
	defer os.Remove(temporary.Name())
	_, err = temporary.Write(append(data, '\n'))
	if err = errors.Join(err, temporary.Close()); err != nil {
		return err
	}
	return os.Rename(temporary.Name(), Path(root))
}

// Check says whether what the configuration names is there, as far as that can be known without
// starting anything: the skills directory is a directory, and the job tool an executable file. A command
// is only known when it runs. It names the field of the first that is not, and an empty field is not
// looked at. The state directory is checked with its root, by gatewayroot.CheckState.
func Check(config *pb.HostConfig) error {
	if dir := config.GetSkillsDirectory(); dir != "" {
		if info, err := os.Stat(dir); err != nil {
			return fmt.Errorf("skills_directory: %w", err)
		} else if !info.IsDir() {
			return fmt.Errorf("skills_directory: %s is not a directory", dir)
		}
	}
	if tool := config.GetJobTool(); tool != "" {
		if info, err := os.Stat(tool); err != nil {
			return fmt.Errorf("job_tool: %w", err)
		} else if !info.Mode().IsRegular() || info.Mode().Perm()&0o111 == 0 {
			return fmt.Errorf("job_tool: %s is not an executable file", tool)
		}
	}
	return nil
}

// Digest names a configuration by what it says, however its file is laid out. The empty
// configuration, which is what a missing file means, has the empty digest.
func Digest(config *pb.HostConfig) string {
	if proto.Size(config) == 0 {
		return ""
	}
	data, err := proto.MarshalOptions{Deterministic: true}.Marshal(config)
	if err != nil {
		// A message of strings always marshals; a digest that could not name it must not match.
		panic(fmt.Sprintf("hostconfig: marshal: %v", err))
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// defaultToolPath is where a host looks for programs when its configuration names no directories.
// It is the same whoever starts the host; `host configure --tool-path` is how to change it.
var defaultToolPath = []string{"/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"}

// ToolPath is where the host of config looks for programs.
func ToolPath(config *pb.HostConfig, home string) []string {
	if dirs := config.GetToolPath(); len(dirs) > 0 {
		return dirs
	}
	if home == "" {
		return defaultToolPath
	}
	return append([]string{filepath.Join(home, ".local", "bin")}, defaultToolPath...)
}

// DefaultRepositoriesDirectory is where a host provisions repositories when its configuration names
// no directory. It is written with "~" as the configuration may write it.
const DefaultRepositoriesDirectory = "~/.vibesys/repos"

// RepositoriesDirectory is the directory the host of config provisions repositories in when a request
// names no parent: the configured one or the default, with a leading "~" expanded to home. This is the
// one place the host's default is worked out; the catalogue shows it and the provisioner uses it.
func RepositoriesDirectory(config *pb.HostConfig, home string) string {
	dir := config.GetRepositoriesDirectory()
	if dir == "" {
		dir = DefaultRepositoriesDirectory
	}
	switch {
	case dir == "~":
		return home
	case strings.HasPrefix(dir, "~/"):
		return filepath.Join(home, dir[2:])
	}
	return filepath.Clean(dir)
}

// sessionFacts are what a host keeps of the environment it was started in. None of them chooses
// anything about the host: they say who the user is and how this login's programs reach each other.
// The others are what the job tool resolves its daemon, store and operator configuration from when the
// host runs it: without them the host would ask for a daemon in another place than the operator's own
// shell, which one daemon per machine refuses, and a daemon the host starts would ignore the Slurm or
// Docker settings the operator's own vibesys-cli obeys.
var sessionFacts = []string{"USER", "LOGNAME", "SHELL", "TMPDIR", "XDG_RUNTIME_DIR", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "VIBESYS_CLI_CONFIG"}

// Environ is a host's whole environment: its home, PATH from its configuration, and the session
// facts of getenv. Nothing else of getenv is looked at, so no setting of whoever started the host
// can reach the host or anything it starts.
func Environ(config *pb.HostConfig, home string, getenv func(string) string) []string {
	env := []string{"HOME=" + home, "PATH=" + strings.Join(ToolPath(config, home), string(os.PathListSeparator))}
	for _, name := range sessionFacts {
		if value := getenv(name); value != "" {
			env = append(env, name+"="+value)
		}
	}
	return env
}

// Retired are the environment settings a host used to read and now ignores, each with the flag of
// `vibesys-client host configure` that took its place. A client names the ones it sees when it
// starts a host, so a setting that stopped working does not stop silently.
var Retired = []struct{ Name, Flag string }{
	{"VIBESYS_ORCHESTRATOR_STATE_DIR", "--state-directory"},
	{"VIBESYS_ORCHESTRATOR_SIDECAR", "--session-holder"},
	{"VIBESYS_ORCHESTRATOR_MARKETPLACE", "--skills-directory"},
	{"VIBESYS_ORCHESTRATOR_CLI", "--job-tool"},
}
