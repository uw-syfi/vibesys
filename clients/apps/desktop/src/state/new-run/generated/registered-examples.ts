/**
 * Generated from examples/registry.toml by scripts/generate_desktop_examples.py. Do not edit.
 * source-sha256: b8f596570d5a358a3850460521c599915e859dda12e50084ae0bc59dcd98a156
 */

export type RegisteredExampleCategory =
  | 'data-structures'
  | 'database'
  | 'microservices'
  | 'model-serving';

export interface RegisteredExample {
  readonly id: string;
  readonly category: RegisteredExampleCategory;
  readonly needsClone: boolean;
}

/** Every runnable example registered by this VibeSys checkout, in registry order. */
export const REGISTERED_EXAMPLES = [
  {id: 'data-structures/kvstore-default', category: 'data-structures', needsClone: false},
  {id: 'data-structures/ordered-map', category: 'data-structures', needsClone: false},
  {id: 'data-structures/prioqueue', category: 'data-structures', needsClone: false},
  {id: 'data-structures/repositories/queue-rs', category: 'data-structures', needsClone: false},
  {id: 'data-structures/stack', category: 'data-structures', needsClone: false},
  {id: 'data-structures/unordered-map', category: 'data-structures', needsClone: false},
  {id: 'database/differential-dataflow', category: 'database', needsClone: false},
  {id: 'database/kv-store', category: 'database', needsClone: false},
  {id: 'microservices/repositories/deathstarbench', category: 'microservices', needsClone: true},
  {id: 'microservices/repositories/hotel', category: 'microservices', needsClone: true},
  {id: 'microservices/repositories/socialnet', category: 'microservices', needsClone: true},
  {id: 'microservices/repositories/train-ticket', category: 'microservices', needsClone: true},
  {id: 'model-serving/Llama-3-8B', category: 'model-serving', needsClone: false},
  {id: 'model-serving/Llama-3-8B-trn2', category: 'model-serving', needsClone: false},
  {
    id: 'model-serving/Llama-3.1-8B-Instruct-MLX-8bit',
    category: 'model-serving',
    needsClone: false,
  },
  {id: 'model-serving/deepseek-v3.2-8xb200', category: 'model-serving', needsClone: false},
  {id: 'model-serving/flint', category: 'model-serving', needsClone: false},
  {id: 'model-serving/glm-5.2-8xb200', category: 'model-serving', needsClone: false},
  {id: 'model-serving/kimi-k3-8xb200', category: 'model-serving', needsClone: false},
  {id: 'model-serving/llama-70b-2xh100', category: 'model-serving', needsClone: false},
  {id: 'model-serving/moonshine-streaming', category: 'model-serving', needsClone: false},
  {id: 'model-serving/olmo-hybrid-prefix-caching', category: 'model-serving', needsClone: false},
  {id: 'model-serving/qwen3-32b-code-edit', category: 'model-serving', needsClone: false},
  {id: 'model-serving/qwen3.5-397b-a17b-4xb200', category: 'model-serving', needsClone: false},
  {
    id: 'model-serving/qwen3.5-397b-a17b-mi300a-bespoke',
    category: 'model-serving',
    needsClone: false,
  },
  {id: 'model-serving/qwen3.5-9b-mi210', category: 'model-serving', needsClone: false},
  {id: 'model-serving/qwen3.6-35b-a3b', category: 'model-serving', needsClone: false},
  {id: 'model-serving/repositories/sglang', category: 'model-serving', needsClone: true},
  {id: 'model-serving/repositories/vllm', category: 'model-serving', needsClone: true},
  {id: 'model-serving/repositories/vllm-omni', category: 'model-serving', needsClone: true},
  {id: 'model-serving/show-o2-1.5B-HQ', category: 'model-serving', needsClone: false},
  {id: 'model-serving/whisper-large-v3', category: 'model-serving', needsClone: false},
] as const satisfies readonly RegisteredExample[];
