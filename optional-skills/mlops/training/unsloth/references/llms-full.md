# Unsloth Full Reference Index

This file intentionally no longer stores the full scraped Unsloth documentation body.
The prior aggregate exceeded the repository's default non-leak scan limit and
included raw exported page bodies that are better kept in smaller, targeted
references.

Use these local companions instead:

- `index.md` - compact reference category index.
- `llms.md` - concise page map for Unsloth documentation topics.
- `llms-txt.md` - detailed extracted notes and examples, kept below the default
  tracked-file scan limit.

Primary topic areas preserved by the local references:

- Installation and update paths for Linux, Windows, WSL, Conda, Docker, Colab,
  AMD, NVIDIA, and Intel environments.
- Fine-tuning guides covering LoRA, QLoRA, datasets, hyperparameters,
  continued pretraining, checkpoint continuation, chat templates, and
  quantization-aware training.
- Reinforcement learning workflows including GRPO, GSPO, DPO, ORPO, KTO,
  reward-hacking considerations, and memory-efficient RL.
- Running and saving models through GGUF, Ollama, vLLM, SGLang, inference
  troubleshooting, and hot-swapping adapters.
- Model-family notes for Qwen, DeepSeek, Gemma, Llama, Mistral, Kimi, GLM,
  Granite, Phi, gpt-oss, vision models, and TTS.
- Hardware and scaling references for multi-GPU training, Blackwell/RTX
  systems, DGX Spark, AMD, and long-context training.

When refreshing this reference, prefer a bounded index or topic-specific
summaries over a monolithic scrape. Do not include credential-shaped example
values, tokenized media links, promotional boilerplate, or unrelated injected
content in tracked reference files.
