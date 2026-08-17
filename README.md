# CA Assembler — Hermes Agent Plugin

*[中文](README.zh.md)*

**ContextAssembler v6.x** — the [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin
version of the Context Assembler: keeps the context window dense, cache-friendly and cheap by
spending as little as possible to feed the cloud LLM a context with the highest
mutual-information density per token.

> This is the **Hermes (Python) version**. The design has been carried forward into
> **CA-DSH V0.99** — the [DeepSeek Harness (dsh) plugin version](https://github.com/i1j/ca-dsh),
> which is under active development. This repo is maintained as the reference implementation
> of the original Hermes design.

## What it does

- **E-stage** — write-on-disk: every session turn is captured as it arrives (5 hooks), including
  tool calls, LLM calls and think traces (decision 44: block-level OODA tagging).
- **F-stage** — background local 4B daemon turns raw turns into facts: `Fct` (OODA four phases) +
  `Hdl` (handles), with multi-affair transaction frames.
- **Topic management** — real-time topic-block segmentation (forced phrases → confirmation turns →
  Jaccard with water-pressure: the more context accumulates, the more aggressively topics split;
  at peak it force-splits).
- **Grading & cache freezing** — on topic switch, old blocks are graded ACT/REL/FAR against the
  current question (embedding centroid radius); grades freeze until the next switch so the
  context prefix stays stable and cloud prompt caches keep hitting.
- **A-stage assembly** — `_build_conv_history_v6`: tail hard-protection (2 user turns verbatim),
  ACT/REL/FAR tiered downgrading, FAR thought/tool removal; topic-block summary versions built
  on switch (2–6 strands per block via one 4B call).
- **Reality merge & inject** — strands are merged into "realities" by work affinity
  (question-cloud / work-cloud + 4B acceptance); on new topic blocks, related realities are
  injected at the head (θ ≤ 0.5, top-15 pool → 4B pick; a legal empty pick means no injection —
  "rather missing than wrong").
- **Tool summarizer** — deterministic per-tool structured summaries of `toolCall`/`toolResult`
  (bash exit code/stderr/key lines, read path+lines, edit/write paths, MCP JSON key fields).
- **L-stage** — idle refinement daemon: merge review → detail regeneration → cross-validation →
  health scoring → knowledge subgraph; L1 code (0 tokens) → L2 local 4B → L3 cloud.

## Layout

```
ca/                  # plugin implementation (stage modules, store, reality, topic mgmt, ...)
topic_manager.py     # topic segmentation engine
tests/               # pytest suites (unit / stage / store / plugin / audit)
scripts/             # offline ops: reality migration, reprocess pipeline, wiki sync, ...
docs/                # dual-dimension wiki — see docs/README.md
├── architecture/    # current system components (01-overview … 14-idle-refinement + design notes)
└── decisions/       # decision tree (01–45, numbered ADRs)
plugin.yaml          # Hermes plugin manifest
```

## Install & run

The plugin runs inside a Hermes Agent profile. `plugin.yaml` declares the required
pip dependencies (`numpy`, `urllib3`, `sentence-transformers`, `pyyaml`) and the hooks it
registers. Point the plugin root of your profile at this repo, set the `CA_*` environment
overrides documented in `ca/config.py` (local 4B LLM endpoint, embedding endpoint, topic
thresholds, context budgets), then start Hermes.

Run the test suite:

```sh
python3 -m pytest tests/
```

## Docs

- [docs/README.md](docs/README.md) — the dual-dimension wiki entry (architecture + decisions)
- [docs/architecture/](docs/architecture/) — current system components
- [docs/decisions/](docs/decisions/) — decision records (ADR), 01–45
- The authoritative design intent and the mapping to CA-DSH V0.99 live in
  [CA-DSH docs/DESIGN.md](https://github.com/i1j/ca-dsh/blob/main/docs/DESIGN.md)

## License

MIT © 2026 [i1j](https://github.com/i1j) — see [LICENSE](LICENSE).
