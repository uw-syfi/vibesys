Accuracy checker for Olmo-Hybrid-7B.

Run: `python checker.py`

Compares the custom `VibeServeModel.generate()` against `transformers.AutoModelForCausalLM.generate()` on a fixed set of base-model completion prompts under greedy decoding. Outputs must match token-for-token.
