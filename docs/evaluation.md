# Evaluation summary

Bebbi Bot deep-research evaluation, 7-13 October 2026, all runs on the production model (Gemma-4-31B NVFP4) and
production search stack (Serper, Firecrawl, MCP). Full report, per-task analyses and raw results are in the
evaluation repository `deep-research-evaluation` (`docs/bebbi-bot-deep-research.md`, `docs/analysis/`, `docs/research/`,
`results/`). This repository contains the engine version evaluated last (v7: v6 engine code, v7 preset settings).

## Decision

Own job service behind one Open WebUI pipe, two engines selected by preset. GPT-Researcher and NVIDIA Deep Researcher
were not adopted: same report quality, 280k and 780-860k tokens per report.

## Results in short

* **DeepResearch Bench, 20 English tasks (RACE):** research-agent v5 46.1 (v2 46.0), GPT-Researcher (patched) 46.3, NVIDIA
  46.5 (lean 45.2), Gemma without search 43.9. Citation accuracy 82.5% vs 75.7% / 62.6% (lean); supported citations per
  report 84 vs 32 / 24; median tokens 169k vs 282k / 775k; runtime 6.0 min vs 13.6 / 13.8 min. RACE differences below
  1.5 points are noise; no engine beats Gemma without search by 3 points, so report quality is limited by the model.
* **German administrative set v0.3, 19 tasks (rubric):** focus engine 66.6% on the 13 original tasks (3 repeats) vs 58.9%
  for a simple agent loop with the same MCP tools and 54.6% for NVIDIA; about 60k tokens and 3 minutes per report. On the
  6 Grossrat full-text tasks v7 reaches 80.2% (agent loop 79.4%) after two fixes: citation repair no longer picks sources
  by bare years (v6), and the grossrat preset writes shorter reports (v7).
* **Statistik:** DE-ST1 67.8% with the data.bs fixes deployed (50.0 before). The statistics MCP server is switched off
  until its container can reach statistik.bs.ch.
* **Known gaps:** "laufende Legislatur" (no tool returns the legislature periods; requested in
  [grossrat-mcp-changes.md](grossrat-mcp-changes.md)), readability of survey reports.

## Decisions carried into this repository

On-prem models only; night window 20:00-05:00 with quick focus runs allowed by day (one at a time); no per-user quota;
delivery into the chat with an admin API key and the pipe fallback; clarify (max 2 questions), plan edit and approval as
chat turns; statistik MCP server switched off until it works.

## Open items

Statistics MCP container egress; legislature periods in the Grossrat server; domain review of the German test set and
the comparison with Gemini and Perplexity Deep Research; end-to-end test on the test cluster; UAT pilot with vLLM load
measurement.
