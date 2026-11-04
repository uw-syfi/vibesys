package wire

import (
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"google.golang.org/protobuf/proto"
)

// UpgradeEffectV2 normalizes schema 2's launches before strict decoding. Only the store's
// versioned upgrade calls this boundary. An empty owner means the row needed no upgrade.
func UpgradeEffectV2(data []byte, state core.State) ([]byte, core.RunID, error) {
	var effect pb.Effect
	if err := proto.Unmarshal(data, &effect); err != nil {
		return nil, "", err
	}
	launch := effect.GetLaunchSession()
	if launch == nil || launch.Task != "" {
		return data, "", nil
	}
	owner := core.RunID(launch.RunId)
	if run, ok := state.Run(owner); ok {
		launch.Task = run.Task
		launch.CliLocation = locationOut(run.Location)
	}
	data, err := proto.Marshal(&effect)
	if err == nil {
		_, err = DecodeEffect(data)
	}
	return data, owner, err
}
