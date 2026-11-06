package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/k8s"
)

// The k8s group: deploy a task's application on Kubernetes and act on it (see
// proto/vibesys/evaluator/v1/k8s.proto and the Go package k8s).
//
//	k8s deploy    build, deploy into a fresh namespace, seed, probe; returns the target
//	k8s attach    check a namespace is the app's; returns its target
//	k8s teardown  delete the namespace
//	k8s run-pod   run a command of the task image in a pod and collect its files
//	k8s restart   delete one or every pod of Deployments, wait for the replacements
//	k8s exec      run a command in a pod or the pods of a Deployment
//	k8s wait      wait until the application serves again, then probe
//	k8s flush     empty a cache in place
//	k8s stop      scale the restartable Deployments to 0
//	k8s start     scale them back
//	k8s usage     per-pod CPU seconds since the previous call
//
// Every verb is one request and one response (no event stream): the work takes seconds to
// minutes and its progress is the stderr log. SIGINT and SIGTERM cancel the verb; `deploy` then
// deletes its namespace (without waiting) before it exits.

func init() {
	register("k8s", "deploy", k8sVerb(deploy))
	register("k8s", "attach", k8sVerb(attach))
	register("k8s", "teardown", k8sVerb(teardown))
	register("k8s", "run-pod", k8sVerb(runPod))
	register("k8s", "restart", k8sVerb(restart))
	register("k8s", "exec", k8sVerb(execPod))
	register("k8s", "wait", k8sVerb(wait))
	register("k8s", "flush", k8sVerb(flush))
	register("k8s", "stop", k8sVerb(stop))
	register("k8s", "start", k8sVerb(start))
	register("k8s", "usage", k8sVerb(usage2))
}

// k8sConnect is the cluster a verb works on; tests replace it with a fake.
var k8sConnect = func(env k8s.Environment, log func(string)) k8s.Cluster {
	return k8s.Cluster{
		Env: env, Kube: k8s.Kubectl{}, Clock: k8s.SystemClock{}, Log: log,
		Builder: k8s.BuildctlBuilder{Address: env.Builder, Clock: k8s.SystemClock{}},
	}
}

// k8sEnv is where a verb reads KUBECONFIG, VIBESYS_K8S_REGISTRY and the other variables.
var k8sEnv = os.Getenv

type verbContext struct {
	ctx    context.Context
	stderr io.Writer
}

func (v verbContext) log(line string) { fmt.Fprintln(v.stderr, "vibesys-evaluator k8s: "+line) }

// k8sVerb makes a handler of a typed verb: it decodes the request, runs the verb under a context
// that SIGINT and SIGTERM cancel, and encodes the response.
func k8sVerb[Req, Resp any, PReq interface {
	*Req
	proto.Message
}, PResp interface {
	*Resp
	proto.Message
}](run func(verbContext, PReq) (PResp, error)) Handler {
	return func(io IO) error {
		data, err := readAll(io.Stdin)
		if err != nil {
			return err
		}
		request := PReq(new(Req))
		if err := (protojson.UnmarshalOptions{}).Unmarshal(data, request); err != nil {
			return &DomainError{Kind: k8s.KindInvalid, Message: "request: " + err.Error()}
		}
		ctx, stopSignals := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
		defer stopSignals()
		response, err := run(verbContext{ctx: ctx, stderr: io.Stderr}, request)
		if err != nil {
			return domainError(err)
		}
		out, err := protojson.MarshalOptions{UseProtoNames: true}.Marshal(response)
		if err != nil {
			return err
		}
		_, err = io.Stdout.Write(append(out, '\n'))
		return err
	}
}

func readAll(r io.Reader) ([]byte, error) {
	data, err := io.ReadAll(r)
	if len(strings.TrimSpace(string(data))) == 0 {
		data = []byte("{}")
	}
	return data, err
}

func domainError(err error) error {
	var domain *DomainError
	if errors.As(err, &domain) {
		return err
	}
	return &DomainError{Kind: k8s.KindOf(err), Message: err.Error()}
}

func invalid(format string, args ...any) error {
	return &DomainError{Kind: k8s.KindInvalid, Message: fmt.Sprintf(format, args...)}
}

// -- conversions ----------------------------------------------------------------------------

func appOf(m *pb.K8SApp) k8s.App {
	app := k8s.App{
		Name: m.GetName(), Manifest: m.GetManifest(), Images: m.GetImages(), Gateway: m.GetGateway(),
		DataTier: m.GetDataTier(), HasSeed: m.GetHasSeed(), Restartable: m.GetRestartable(),
		WaitSecs: m.GetWaitSecs(),
	}
	if len(m.GetCaches()) > 0 {
		app.Caches = map[string]k8s.CacheKind{}
		for name, kind := range m.GetCaches() {
			switch kind {
			case pb.K8SCacheKind_K8S_CACHE_KIND_MEMCACHED:
				app.Caches[name] = k8s.Memcached
			case pb.K8SCacheKind_K8S_CACHE_KIND_REDIS:
				app.Caches[name] = k8s.Redis
			default:
				app.Caches[name] = k8s.CacheKind("unspecified")
			}
		}
	}
	return app
}

func roleOf(r pb.K8SRole) (k8s.Role, error) {
	switch r {
	case pb.K8SRole_K8S_ROLE_SEED:
		return k8s.RoleSeed, nil
	case pb.K8SRole_K8S_ROLE_PROBE:
		return k8s.RoleProbe, nil
	case pb.K8SRole_K8S_ROLE_PHASE:
		return k8s.RolePhase, nil
	}
	return "", invalid("run-pod: role must be seed, probe or phase")
}

func podCommandOf(m *pb.K8SPodCommand) (k8s.PodCommand, error) {
	command := k8s.PodCommand{Argv: m.GetArgv()}
	if len(m.GetInputFiles()) > 0 {
		command.Inputs = map[string][]byte{}
		for name, path := range m.GetInputFiles() {
			data, err := os.ReadFile(path)
			if err != nil {
				return command, invalid("input file %s: %v", name, err)
			}
			command.Inputs[name] = data
		}
	}
	return command, nil
}

func duration(secs float64) time.Duration { return time.Duration(secs * float64(time.Second)) }

func cluster(v verbContext, workDir, artifacts string) (k8s.Cluster, error) {
	if workDir == "" {
		return k8s.Cluster{}, invalid("work_dir is required")
	}
	if err := os.MkdirAll(workDir, 0o755); err != nil {
		return k8s.Cluster{}, err
	}
	env, err := k8s.ParseEnvironment(k8sEnv, workDir, artifacts)
	if err != nil {
		return k8s.Cluster{}, err
	}
	return k8sConnect(env, v.log), nil
}

// attached is the Deployment a verb after `deploy` acts on.
func attached(v verbContext, t *pb.K8STarget) (*k8s.Deployment, error) {
	if t == nil || t.GetNamespace() == "" {
		return nil, invalid("target.namespace is required")
	}
	c, err := cluster(v, t.GetWorkDir(), t.GetArtifactsDir())
	if err != nil {
		return nil, err
	}
	src, err := sourceOf(t.GetApp(), t.GetTaskDir(), "", t.GetProbePlanFile())
	if err != nil {
		return nil, err
	}
	d, err := k8s.Attach(v.ctx, src, t.GetNamespace(), c)
	if err != nil {
		return nil, err
	}
	if t.GetTaskImage() != "" {
		d.TaskImage = t.GetTaskImage()
	}
	return d, nil
}

func sourceOf(app *pb.K8SApp, taskDir, projectRoot, probePlanFile string) (k8s.AppSource, error) {
	if taskDir == "" {
		return k8s.AppSource{}, invalid("task_dir is required")
	}
	src := k8s.AppSource{App: appOf(app), TaskDir: taskDir, ProjectRoot: projectRoot}
	if probePlanFile != "" {
		data, err := os.ReadFile(probePlanFile)
		if err != nil {
			return src, invalid("probe_plan_file: %v", err)
		}
		src.ProbePlan = data
	}
	return src, nil
}

// -- verbs --------------------------------------------------------------------------------------

func deploy(v verbContext, req *pb.K8SDeployRequest) (*pb.K8SDeployResponse, error) {
	c, err := cluster(v, req.GetWorkDir(), req.GetArtifactsDir())
	if err != nil {
		return nil, err
	}
	src, err := sourceOf(req.GetApp(), req.GetTaskDir(), req.GetProjectRoot(), req.GetProbePlanFile())
	if err != nil {
		return nil, err
	}
	var seed *k8s.PodCommand
	if req.Seed != nil {
		command, err := podCommandOf(req.Seed)
		if err != nil {
			return nil, err
		}
		seed = &command
	}
	d, err := k8s.Deploy(v.ctx, src, seed, c)
	if err != nil {
		return nil, err
	}
	seconds := map[string]float64{}
	for image, secs := range d.BuildSeconds {
		seconds[image] = secs
	}
	return &pb.K8SDeployResponse{
		Target: &pb.K8STarget{
			App: req.GetApp(), TaskDir: req.GetTaskDir(), Namespace: d.Namespace, TaskImage: d.TaskImage, Uid: d.UID(),
			WorkDir: req.GetWorkDir(), ArtifactsDir: req.GetArtifactsDir(), ProbePlanFile: req.GetProbePlanFile(),
		},
		Images: d.Images, BuildSeconds: seconds,
	}, nil
}

func attach(v verbContext, req *pb.K8SAttachRequest) (*pb.K8SAttachResponse, error) {
	d, err := attached(v, req.GetTarget())
	if err != nil {
		return nil, err
	}
	target := proto.Clone(req.GetTarget()).(*pb.K8STarget)
	target.TaskImage = d.TaskImage
	return &pb.K8SAttachResponse{Target: target}, nil
}

func teardown(v verbContext, req *pb.K8STeardownRequest) (*pb.K8STeardownResponse, error) {
	t := req.GetTarget()
	if t.GetNamespace() == "" || t.GetUid() == "" {
		return nil, invalid("target.namespace and target.uid are required")
	}
	c, err := cluster(v, t.GetWorkDir(), t.GetArtifactsDir())
	if err != nil {
		return nil, err
	}
	d := k8s.Owned(appOf(t.GetApp()), t.GetNamespace(), t.GetUid(), c)
	// A teardown cut short by a signal still requests the deletion: Close ignores the cancellation
	// for that, and only skips its wait.
	if err := d.Close(v.ctx, k8s.CloseOptions{Fast: req.GetFast(), Failed: req.GetFailed()}); err != nil {
		return nil, err
	}
	return &pb.K8STeardownResponse{}, nil
}

func runPod(v verbContext, req *pb.K8SRunPodRequest) (*pb.K8SRunPodResponse, error) {
	role, err := roleOf(req.GetRole())
	if err != nil {
		return nil, err
	}
	command, err := podCommandOf(req.GetCommand())
	if err != nil {
		return nil, err
	}
	if req.GetTimeoutSecs() <= 0 {
		return nil, invalid("run-pod: timeout_secs must be positive")
	}
	d, err := attached(v, req.GetTarget())
	if err != nil {
		return nil, err
	}
	outputs := req.GetOutputsDir()
	if outputs != "" {
		outputs, _ = filepath.Abs(outputs)
	}
	run, err := d.RunPod(v.ctx, role, command, duration(req.GetTimeoutSecs()), k8s.RunOptions{Outputs: outputs, Name: req.GetPodName()})
	if err != nil {
		return nil, err
	}
	return &pb.K8SRunPodResponse{PodName: run.Name, ExitCode: int32(run.ExitCode), Files: run.Files, Log: run.Log, Seconds: run.Seconds}, nil
}

func restart(v verbContext, req *pb.K8SRestartRequest) (*pb.K8SRestartResponse, error) {
	if len(req.GetDeployments()) == 0 {
		return nil, invalid("restart: name at least one Deployment")
	}
	d, err := attached(v, req.GetTarget())
	if err != nil {
		return nil, err
	}
	replaced, err := d.Restart(v.ctx, req.GetDeployments(), k8s.RestartOptions{EveryPod: req.GetEveryPod(), NoWait: req.GetNoWait()})
	if err != nil {
		return nil, err
	}
	response := &pb.K8SRestartResponse{}
	for _, r := range replaced {
		response.Replaced = append(response.Replaced, &pb.K8SReplacedPod{Deployment: r.Deployment, Pod: r.Pod, Uid: r.UID})
	}
	return response, nil
}

func execPod(v verbContext, req *pb.K8SExecRequest) (*pb.K8SExecResponse, error) {
	d, err := attached(v, req.GetTarget())
	if err != nil {
		return nil, err
	}
	results, err := d.Exec(v.ctx, k8s.ExecTarget{Deployment: req.GetDeployment(), Pod: req.GetPod(), AllPods: req.GetAllPods()}, req.GetArgv(), duration(req.GetTimeoutSecs()))
	if err != nil {
		return nil, err
	}
	response := &pb.K8SExecResponse{}
	for _, r := range results {
		response.Results = append(response.Results, &pb.K8SExecResult{Pod: r.Pod, ExitCode: int32(r.ExitCode), Output: r.Output})
	}
	return response, nil
}

func wait(v verbContext, req *pb.K8SWaitRequest) (*pb.K8SWaitResponse, error) {
	d, err := attached(v, req.GetTarget())
	if err != nil {
		return nil, err
	}
	err = d.WaitReady(v.ctx, k8s.WaitOptions{
		Deployments: req.GetDeployments(), Replaced: req.GetReplacedUids(),
		Timeout: duration(req.GetTimeoutSecs()), NoProbe: req.GetNoProbe(),
	})
	return &pb.K8SWaitResponse{}, err
}

func flush(v verbContext, req *pb.K8SFlushRequest) (*pb.K8SFlushResponse, error) {
	d, err := attached(v, req.GetTarget())
	if err != nil {
		return nil, err
	}
	return &pb.K8SFlushResponse{}, d.Flush(v.ctx, req.GetCache())
}

func stop(v verbContext, req *pb.K8SScaleRequest) (*pb.K8SScaleResponse, error) {
	d, err := attached(v, req.GetTarget())
	if err != nil {
		return nil, err
	}
	return &pb.K8SScaleResponse{}, d.Stop(v.ctx)
}

func start(v verbContext, req *pb.K8SScaleRequest) (*pb.K8SScaleResponse, error) {
	d, err := attached(v, req.GetTarget())
	if err != nil {
		return nil, err
	}
	return &pb.K8SScaleResponse{}, d.Start(v.ctx)
}

func usage2(v verbContext, req *pb.K8SUsageRequest) (*pb.K8SUsageResponse, error) {
	if req.GetLabel() == "" {
		return nil, invalid("usage: label is required")
	}
	d, err := attached(v, req.GetTarget())
	if err != nil {
		return nil, err
	}
	delta, err := d.Usage(v.ctx, req.GetLabel())
	if err != nil {
		return nil, err
	}
	return &pb.K8SUsageResponse{CpuSeconds: delta}, nil
}
