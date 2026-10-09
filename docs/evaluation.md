# Evaluation summary

Bebbi Bot deep-research evaluation, 7-9 October 2026, all runs on the production model (Gemma-4-31B NVFP4) and
production search stack (Serper, Firecrawl, MCP). Full report, per-task analyses and raw results are in the
evaluation repository `deep-research-evaluation` (`docs/bebbi-bot-deep-research.md`, `docs/analysis/`, `docs/research/`,
`results/`). This repository contains the engine version v5 that was evaluated last.

## Decision

Own job service behind one Open WebUI pipe, two engines selected by preset. GPT-Researcher and NVIDIA Deep Researcher
were not adopted: same report quality, 280k and 780-860k tokens per report.

## Results in short

* **DeepResearch Bench, 20 English tasks (RACE):** research-agent 46.0, GPT-Researcher (patched) 46.3, NVIDIA 46.5, Gemma
  without search 43.9. Citation accuracy 82.7% vs 75.7% / 54.8%; supported citations per report 61 vs 32 / 20; median tokens
  133k vs 282k / 862k; runtime 4.5 min vs 13.6 / 14.5 min. RACE differences below 1.5 points are noise.
* **v5 survey settings** (`web_survey`, 3 tuning tasks): RACE 46.4, level with NVIDIA (46.6); 80-86% citation accuracy.
* **German administrative set, 13 tasks (rubric):** focus engine 66.0% (3 repeats: 66.2 / 69.8 / 62.0) vs 58.9% simple agent
  loop with the Grossrat MCP and 54.6% NVIDIA. About 60k tokens and 3 minutes per report. Gain from sourcing and dates
  (every claim sourced: 1.15 vs 0.31 for the pipeline engine on German tasks).
* **Weak spots:** Statistik tasks (data.bs.ch server errors reached the model without a reason; fix merged in the server, not
  yet deployed), "laufende Legislatur" (no tool returns the legislature periods), readability of survey reports.
* The MCP front proxy returned 503 in 85 of 232 health samples; the client retries, no call failed because of it.

## Decisions carried into this repository

On-prem models only; night window 20:00-05:00 with quick focus runs allowed by day (one at a time); no per-user quota;
delivery into the chat with an admin API key and the pipe fallback; clarify (max 2 questions), plan edit and approval as
chat turns; statistik MCP server switched off until it works.

## Open items (from the evaluation)

Deploy `mcp-data-bs` #7; confirm v5 on the 10 held-out DRB tasks; extend the German set with Grossrat full-text tasks;
domain review of the German criteria; pilot on UAT with vLLM load measurement; test the pipe in a real Open WebUI.
