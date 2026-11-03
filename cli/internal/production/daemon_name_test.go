package production

import (
	"os"
	"reflect"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/client"
)

func TestDaemonCommandName(t *testing.T) {
	root := t.TempDir()
	p := Process{Executable: "/some/vibesys-cli", UID: os.Getuid(), Getenv: func(key string) string {
		if key == "XDG_RUNTIME_DIR" {
			return root
		}
		return ""
	}}
	opts, err := p.ClientOptions()
	if err != nil {
		t.Fatal(err)
	}
	cmd := opts.Starter.(client.ExecStarter).Command()
	if cmd.Path != p.Executable || !reflect.DeepEqual(cmd.Args, []string{"vibesysd", "daemon", "run"}) {
		t.Fatalf("command: path %q args %q", cmd.Path, cmd.Args)
	}
}
