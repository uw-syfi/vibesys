/**
 * The example catalogue a helper answers `examples.list` with when asked for `with_catalogue`
 * (feature `example_catalogue`): a small, fixed copy of what the real catalogue says about each
 * task, so a screen that offers examples by domain, model and hardware can be built and checked
 * without a helper. It spans the four domains, every maker and vendor the contract names, models
 * served on more than one kind of hardware, and tasks with and without objectives or a workload.
 * `examples.test.ts` holds it valid against the helper contract.
 */
import type {ExampleList, ExampleTask} from '@vibesys/contracts/host-helper';
import queueRsListing from './data/queue-rs-catalogue.json' with {type: 'json'};

const MAX_THROUGHPUT = {metric: 'output_token_throughput_per_s', direction: 'max'} as const;
const MIN_P90_LATENCY = {metric: 'p90_latency_ms', direction: 'min'} as const;

function serving(
  task: string,
  model: ExampleTask['model'],
  hardware: ExampleTask['hardware'],
  goal: string,
): ExampleTask {
  return {
    task,
    domain: 'llm-serving',
    hardware,
    model,
    workload: null,
    objectives: [MAX_THROUGHPUT, MIN_P90_LATENCY],
    goal,
    goal_truncated: false,
  };
}

const CPU = {accelerator: 'CPU', count: 1, vendor: 'cpu', backend: 'cpu'} as const;

export const exampleCatalogue: ExampleList = {
  version: 1,
  checkout: '/home/me/vibesys',
  examples: [
    {
      name: 'data-structures/stack',
      category: 'data-structures',
      tasks: ['mpmc', 'spsc'],
      external_repo: false,
      task_details: [
        {
          task: 'mpmc',
          domain: 'data-structures',
          hardware: CPU,
          model: null,
          workload: {
            name: 'Multiple producers, multiple consumers',
            description:
              'A bounded stack where several threads add items and several threads remove them; checked for linearizability, scored in operations per second.',
          },
          objectives: [],
          goal: 'Optimize a multi-producer, multi-consumer bounded stack.\n\nHeadline metric: `total_ops_per_sec` (maximize).\n',
          goal_truncated: false,
        },
        {
          task: 'spsc',
          domain: 'data-structures',
          hardware: CPU,
          model: null,
          workload: {
            name: 'Single producer, single consumer',
            description:
              'A bounded stack where one thread adds items and one thread removes them; checked for linearizability, scored in operations per second.',
          },
          objectives: [],
          goal: 'Optimize a single-producer, single-consumer bounded stack.\n\nHeadline metric: `total_ops_per_sec` (maximize).\n',
          goal_truncated: false,
        },
      ],
    },
    {
      name: 'database/kv-store',
      category: 'database',
      tasks: ['default'],
      external_repo: false,
      task_details: [
        {
          task: 'default',
          domain: 'databases',
          hardware: CPU,
          model: null,
          workload: null,
          objectives: [],
          goal: '# Objective: KV store (Redis RESP2)\n\nMaximize **throughput (ops/sec)** on YCSB Workload A under concurrent client load.\n',
          goal_truncated: false,
        },
      ],
    },
    {
      name: 'microservices/hotel-correctness',
      category: 'microservices',
      tasks: ['compose'],
      external_repo: false,
      task_details: [
        {
          task: 'compose',
          domain: 'microservices',
          hardware: CPU,
          model: null,
          workload: null,
          objectives: [],
          goal: "Optimize the Hotel Reservation service's throughput while preserving its public HTTP semantics.\n",
          goal_truncated: false,
        },
      ],
    },
    {
      name: 'model-serving/llama-70b-2xh100',
      category: 'model-serving',
      tasks: ['default'],
      external_repo: false,
      task_details: [
        serving(
          'default',
          {name: 'Llama 3.3 70B Instruct', maker: 'meta'},
          {accelerator: 'NVIDIA H100', count: 2, vendor: 'nvidia', backend: 'cuda'},
          '# Llama-3.3 70B Dense Multi-GPU Serving\n\nBuild an OpenAI-compatible chat server from scratch.\n',
        ),
      ],
    },
    {
      name: 'model-serving/Llama-3-8B-trn2',
      category: 'model-serving',
      tasks: ['default'],
      external_repo: false,
      task_details: [
        serving(
          'default',
          {name: 'Llama 3 8B Instruct', maker: 'meta'},
          {accelerator: 'AWS Trainium2', count: 1, vendor: 'aws', backend: 'trainium'},
          '# Objective: Llama-3-8B inference server on AWS Trainium (trn2)\n',
        ),
      ],
    },
    {
      name: 'model-serving/Llama-3.1-8B-Instruct-MLX-8bit',
      category: 'model-serving',
      tasks: ['default'],
      external_repo: false,
      task_details: [
        {
          ...serving(
            'default',
            {name: 'Llama 3.1 8B Instruct', maker: 'meta'},
            {accelerator: 'Apple Silicon', count: 1, vendor: 'apple', backend: 'metal'},
            '# Objective: Llama-3.1-8B-Instruct MLX 8-bit JSON-schema server\n',
          ),
          objectives: [],
        },
      ],
    },
    {
      name: 'model-serving/qwen3.5-9b-mi210',
      category: 'model-serving',
      tasks: ['default'],
      external_repo: false,
      task_details: [
        serving(
          'default',
          {name: 'Qwen3.5 9B', maker: 'qwen'},
          {accelerator: 'AMD MI210', count: 1, vendor: 'amd', backend: 'rocm'},
          '# Objective: Qwen3.5-9B agentic-coding-session serving on 1x MI210\n',
        ),
      ],
    },
    {
      name: 'model-serving/deepseek-v3.2-8xb200',
      category: 'model-serving',
      tasks: ['default'],
      external_repo: false,
      task_details: [
        serving(
          'default',
          {name: 'DeepSeek V3.2', maker: 'deepseek'},
          {accelerator: 'NVIDIA B200', count: 8, vendor: 'nvidia', backend: 'cuda'},
          '# Objective: DeepSeek-V3.2 From-Scratch 8xB200 Serving\n',
        ),
      ],
    },
    {
      name: 'model-serving/olmo-hybrid-prefix-caching',
      category: 'model-serving',
      tasks: ['from-scratch', 'vllm'],
      external_repo: false,
      task_details: ['from-scratch', 'vllm'].map(task =>
        serving(
          task,
          {name: 'Olmo Hybrid 7B', maker: 'ai2'},
          {accelerator: 'NVIDIA L4', count: 1, vendor: 'nvidia', backend: 'cuda'},
          '# Objective: Olmo-Hybrid-7B prefix-caching workload\n',
        ),
      ),
    },
    {
      name: 'model-serving/whisper-large-v3',
      category: 'model-serving',
      tasks: ['default'],
      external_repo: false,
      task_details: [
        serving(
          'default',
          {name: 'Whisper large v3', maker: 'openai'},
          {accelerator: 'NVIDIA GPU', count: 1, vendor: 'nvidia', backend: 'cuda'},
          '# Objective: whisper-large-v3 offline ASR server\n',
        ),
      ],
    },
    {
      name: 'model-serving/kimi-k3-8xb200',
      category: 'model-serving',
      tasks: ['default'],
      external_repo: false,
      task_details: [
        serving(
          'default',
          {name: 'Kimi K3', maker: 'moonshot'},
          {accelerator: 'NVIDIA B200', count: 8, vendor: 'nvidia', backend: 'cuda'},
          '# Objective: Kimi-K3 From-Scratch 8xB200 Serving\n',
        ),
      ],
    },
    {
      name: 'model-serving/glm-5.2-8xb200',
      category: 'model-serving',
      tasks: ['default'],
      external_repo: false,
      task_details: [
        serving(
          'default',
          {name: 'GLM 5.2', maker: 'zhipu'},
          {accelerator: 'NVIDIA B200', count: 8, vendor: 'nvidia', backend: 'cuda'},
          '# Objective: GLM-5.2 From-Scratch 8xB200 Serving\n',
        ),
      ],
    },
    {
      name: 'model-serving/repositories/vllm',
      category: 'model-serving',
      tasks: [],
      external_repo: true,
      task_details: [],
    },
  ],
};

/** The same catalogue as a helper that did not negotiate `example_catalogue` answers it. */
export const exampleCatalogueWithoutDetails: ExampleList = {
  ...exampleCatalogue,
  examples: exampleCatalogue.examples.map(({task_details: _details, ...example}) => example),
};

/**
 * What a current helper answers `examples.list` with for the `queue-rs` example, verbatim: read
 * from the example's real task files (`tests/entrypoints/test_example_catalog_fixture.py` holds it
 * equal to what the catalogue reads there), so a screen is checked on real goals, not invented ones.
 */
export const queueRsCatalogue: ExampleList = queueRsListing as ExampleList;
