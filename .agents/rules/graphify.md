---
trigger: always_on
description: Consult the graphify knowledge graph at graphify-out/ for codebase, architecture, and documentation questions to drastically reduce token usage.
---

## graphify — Token-Efficient Knowledge Graph Guidance

This project contains massive documentation (`PRD.md`, `TRD.md`, `PHASES.md`, `README.md`) exceeding 370,000 words (~500,000 tokens). To prevent token exhaustion and stay within context budgets, Graphify is configured as the primary index and retrieval engine.

Rules for AI Agents:
- **Never Load Full Docs:** Do NOT dump or read entire multi-megabyte markdown files (`PRD.md`, `TRD.md`, `PHASES.md`). Blindly reading these files consumes hundreds of thousands of tokens and degrades reasoning.
- **Query First:** For any codebase, architecture, requirement, or implementation question, when `graphify-out/graph.json` exists, execute `py -m graphify query "<question>"` (or `graphify query "<question>"`). This returns targeted node definitions and exact source line ranges (e.g., `loc=L1467`), achieving up to 50x–70x token reduction.
- **Relational Pathing & Explanations:**
  - Use `py -m graphify path "<A>" "<B>"` to trace paths and interfaces between subsystems or concepts.
  - Use `py -m graphify explain "<concept>"` to inspect immediate neighbors and architectural clusters.
- **Modular Navigation:** If `graphify-out/wiki/index.md` exists, navigate community wiki pages instead of crawling raw documents.
- **Targeted Slicing:** Only read bounded line ranges (using specific offsets) based on the exact line citations surfaced by Graphify queries.
- **Zero-Token Maintenance:** After modifying code, schemas, or markdown in this session, run `py -m graphify update .` to keep `graphify-out/graph.json` synchronized at zero token cost (deterministic AST parsing).
- **Merge Integrity:** `graphify-out/graph.json` is configured with a dedicated git merge driver via `.gitattributes`. Diffs to this file are expected.
