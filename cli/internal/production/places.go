package production

import (
	"fmt"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
)

// chosenRuntime is the runtime directory JobToolPlace names, empty when this process was given none.
// A value that cannot be a directory is refused here, naming its variable, so a process that was told
// which daemon to reach never reaches another.
func (p Process) chosenRuntime() (string, error) {
	place := &cliv1.JobToolPlace{}
	if err := envcodec.Decode(p.Getenv, place); err != nil {
		return "", &core.Failure{Kind: core.FailureInvalidInput, Message: err.Error()}
	}
	return place.GetRuntimeDirectory(), nil
}

// recordPlaces records where a daemon of the directory keeps state, from this process's environment,
// unless the directory says so already. It is what `daemon ensure` does before it starts anything,
// which is how whoever chooses a daemon for others, a person or the host, sets a directory up, and
// sets it up again after the directory went away.
func (p Process) recordPlaces(dir runtimedir.Dir) (*cliv1.DaemonPlaces, error) {
	return dir.RecordPlaces(filestore.StateHome(p.Getenv, p.Home))
}

// daemonPlaces is where the daemon of the directory keeps state: what the directory records. A
// process that was not told which daemon to reach records its own answer the first time, as a person
// starting a daemon by hand does. One that was told does not: its environment was not the one that
// chose this daemon, so its answer could be a second store. It is refused, and the command that
// started it says so.
func (p Process) daemonPlaces(dir runtimedir.Dir) (*cliv1.DaemonPlaces, error) {
	chosen, err := p.chosenRuntime()
	if err != nil {
		return nil, err
	}
	if chosen == "" {
		return p.recordPlaces(dir)
	}
	places, err := dir.Places()
	if err != nil {
		return nil, err
	}
	if places == nil {
		return nil, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(
			"the runtime directory %s, which this process was told to use, does not record where its daemon keeps state, "+
				"so no daemon is started from here; whoever chose it sets it up again with `daemon ensure`", dir.Path())}
	}
	return places, nil
}
