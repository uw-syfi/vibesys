package clientcli

import (
	"context"
	"flag"
	"fmt"
	"path/filepath"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// HostConfigs is the host's configuration in the root of each --root value ("" for the default).
type HostConfigs interface {
	Load(root string) (*pb.HostConfig, error)
	// Save records what the next host of that root starts with, having refused what it can tell a host
	// could not use, naming the field. Nothing is written then.
	Save(root string, config *pb.HostConfig) error
}

// hostConfigure records what the next host starts with, or prints it when given no flag. A flag
// given replaces its field, and given empty clears it; the running host is not touched, and the next
// verb that finds it replaces it when it can, as for any host on another configuration.
func hostConfigure(_ context.Context, c *call, args []string) error {
	fs := flags("host configure")
	state := fs.String("state-directory", "", "where the orchestrator keeps its state: an absolute path on a local disk")
	holder := fs.String("session-holder", "", "the session holder's checkout, or the command that runs it, in place of the release's")
	skills := fs.String("skills-directory", "", "the skills marketplace checkout, in place of the release's")
	tool := fs.String("job-tool", "", "the job tool, in place of the release's")
	toolPath := fs.String("tool-path", "", "the directories the host looks for programs in, separated by colons")
	repositories := fs.String("repositories-directory", "", "where provisioned repositories go when a request names no parent: an absolute path, or ~ for the host user's home (default ~/.vibesys/repos)")
	clear := fs.Bool("clear", false, "forget everything first")
	positional, err := parse(fs, args)
	if err != nil {
		return err
	}
	if len(positional) != 0 {
		return usageError("host configure takes no argument, got %q", positional[0])
	}
	// --clear is how a file that does not read is put right, so it never reads the file.
	config := &pb.HostConfig{}
	if !*clear {
		if config, err = c.Configs.Load(c.root); err != nil {
			return loadFailure(err)
		}
	}
	given := 0
	fs.Visit(func(*flag.Flag) { given++ })
	if given == 0 {
		return printJSON(c.Stdout, config)
	}
	fs.Visit(func(f *flag.Flag) {
		switch f.Name {
		case "state-directory":
			config.StateDirectory = *state
		case "session-holder":
			config.SessionHolderCommand = *holder
		case "skills-directory":
			config.SkillsDirectory = *skills
		case "job-tool":
			config.JobTool = *tool
		case "repositories-directory":
			config.RepositoriesDirectory = *repositories
		case "tool-path":
			config.ToolPath = filepath.SplitList(*toolPath)
		}
	})
	if err := c.Configs.Save(c.root, config); err != nil {
		return placeOrFailed(err)
	}
	_, err = fmt.Fprintln(c.Stdout, "the next host starts with this; a host that is running keeps what it started with until it is replaced")
	return err
}

// loadFailure is placeOrFailed for a configuration that does not read, with the way out.
func loadFailure(err error) error {
	failure := placeFailure(err)
	if exitCodeOf(failure) == ExitUsage {
		return failure
	}
	return failed("%s", configProblem(err))
}

// placeOrFailed is a failure with a usage exit for a root that cannot be chosen, and a refusal for
// anything else: a configuration that does not read, or that the message's rules reject.
func placeOrFailed(err error) error {
	failure := placeFailure(err)
	if exitCodeOf(failure) == ExitUsage {
		return failure
	}
	return failed("%v", err)
}
