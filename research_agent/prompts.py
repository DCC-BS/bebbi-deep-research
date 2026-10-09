"""Prompt templates. Instructions in English (robust for Gemma), output language set per task."""

PLAN = """You plan a research report. Today is {today}.

TASK:
{task}

FIRST SEARCH RESULTS FOR ORIENTATION (titles and snippets, may be incomplete):
{discovery}

Return a JSON object:
{{"title": "report title",
  "task_core": "the task in one sentence, keeping every explicit requirement",
  "language": "de" or "en" (the language the report must be written in: the task's language unless it says otherwise),
  "parts": [{{"id": "P1", "title": "...", "goal": "what this part must establish"}}],
  "sub_questions": [{{"id": "Q1", "part": "P1", "text": "a concrete, searchable question", "intent": "primary|data|secondary"}}],
  "must_name": ["only entities the TASK ITSELF names explicitly: organisations, cantons, cities, countries, products, laws, time ranges"],
  "constraints": ["explicit requirements of the task: perspective, structure, comparisons, deliverables, scope limits"]}}

Rules:
- The parts mirror the structure, comparisons and deliverables the task asks for; if the task lists items or dimensions,
  each gets a part or is covered explicitly. {parts_hint}
- Plan {min_parts}-{max_parts} parts. Do NOT add a summary, analysis/assessment or closing/limitations part:
  they are written separately.
- If the task names entities to compare (e.g. cantons, cities, firms), every comparison covers exactly those entities,
  in the task's order; do not add others.
- At most {max_subq} sub-questions in total; each one answerable by searching; together they cover every part
  and every must_name item.
- intent: primary = laws, official decisions, official documents; data = statistics, datasets, figures;
  secondary = analyses, media, research.
"""

QUERIES = """You write web search queries for one sub-question of a research task. Today is {today}.

TASK: {task_core}
SUB-QUESTION ({intent}): {subq}
ALREADY SEARCHED (do not repeat or paraphrase): {previous}
STILL MISSING: {gap}

Rules:
1. One query = one aspect = one language (de, fr, it or en). Default {default_lang}; use fr/it only for French-/Italian-
   speaking regions or terms that officially exist only in that language; use en for international topics.
2. Use the official term in that language, not a translation of the task wording.
3. Short keyword queries, 3-8 words, no full sentences; quotes only around identifiers (law numbers, case numbers).
4. Name the jurisdiction or organisation when the task does.
5. Every query must serve the sub-question and stay within the task.
6. Never write site: operators or domain names into the query; source filters are added automatically.
{rules}

Write exactly {n} lines, nothing else, each line:
lang | query | purpose (max 10 words)
"""

TRIAGE = """Select the search results most useful for answering the sub-question.

TASK: {task_core}
SUB-QUESTION: {subq}

RESULTS:
{results}

Prefer primary/official sources and pages that directly address the sub-question; a page that merely mentions the
keywords is not useful. Answer with the indices of at most {k} results, most useful first, comma-separated
(e.g. "3, 0, 5"). Answer "none" if nothing is useful."""

EXTRACT = """Extract evidence from a source for one sub-question of a research task.

TASK: {task_core}
SUB-QUESTION: {subq}
SOURCE: {title} ({url})

SOURCE TEXT:
<<<
{text}
>>>

Return JSON: {{"relevant": true|false, "date": "publication date or 'Stand' stated in the source, else empty", "findings": [{{"finding": "one factual sentence in {lang}", "quote": "verbatim excerpt"}}]}}
Rules:
- At most 4 findings, only facts that help answer the sub-question.
- "quote" must be copied EXACTLY from the source text (one or two sentences, max 400 characters), including the
  numbers, dates and names the finding relies on. Never paraphrase inside "quote".
- The finding states only what the quote supports: same numbers, same scope, no generalisation.
- If the source does not help, return {{"relevant": false, "findings": []}}."""

MCP_AGENT = """You look up authoritative data for one sub-question with the provided tools. Today is {today}.
TASK: {task_core}
SUB-QUESTION: {subq}
Call the tools that answer the sub-question best (at most {max_calls} calls). Use precise search terms and identifiers.
When done, answer "done"."""

REFLECT = """You check the coverage of a research plan after research round {round}.

TASK: {task_core}
MUST BE NAMED IN THE REPORT: {must_name}

SUB-QUESTIONS AND EVIDENCE FOUND:
{status}

Return JSON:
{{"status": [{{"id": "Q1", "status": "covered|partial|missing", "missing": "what concrete information is still missing (identifiers, numbers, dates, entities) or empty"}}],
  "follow_up": [{{"part": "P1", "text": "new concrete, searchable question", "intent": "primary|data|secondary"}}]}}
Rules: at most {max_follow_up} follow-up questions; only for real gaps that matter for the task, including
must_name items without evidence; stay strictly within the task; no repetition of existing sub-questions."""

WRITE_PART = """You write one part of a research report in {language_name}. {spelling}

TASK: {task}
REPORT TITLE: {title}
THIS PART: {part_title}: {part_goal}
OTHER PARTS (do not repeat their content): {other_parts}
REQUIREMENTS OF THE TASK: {constraints}
REPORT REQUIREMENTS: {requirements}
ENTITIES TO NAME IF THE EVIDENCE COVERS THEM: {must_name}

EVIDENCE (only use these; cite by ID):
{cards}

Rules:
- {length_rule} Analytical prose: do not just list facts - explain
  mechanisms, causes and context, compare entities, quantify with the figures from the evidence, and point out
  disagreements or uncertainty between sources. {format_rule} Do not write the part title itself.
- Every sentence with a fact from the evidence ends with exactly ONE citation like [E12] - the single card that
  states this fact most directly. Never write two citations in a row.
- Readability: coherent paragraphs of 3-6 sentences with clear topic sentences and transitions; the citation goes at
  the end of its sentence. In tables, give each row at most one citation, in the last cell.
- Copy numbers, dates, names and identifiers exactly as in the quote of the cited card.
- Tables: only the entities the task names; every cell with a fact has a citation, otherwise write "nicht belegt" / "no evidence".
- Your own analysis, comparisons, assessments and recommendations get NO citation; mark them as such
  ("Daraus folgt ...", "This suggests ...").
- If the evidence does not cover something the part needs, say so briefly instead of guessing.
- Write only the content of this part. No summary, introduction, conclusion or "open points" sections inside
  it, even if the task asks for them: the report adds those parts separately.
- No URLs and no source list in the text."""

SUMMARY = """Write the summary at the start of a research report in {language_name}. {spelling}

TASK: {task}
REPORT PARTS:
{parts}

Write 120-220 words that answer the task directly with the key findings and conclusions. Reuse citations [E..]
exactly as they appear in the parts for the facts you take over (one per sentence); add no new facts. No heading."""

CLOSING = """Write the closing part "{closing_title}" of a research report in {language_name}. {spelling}

TASK: {task}
GAPS FOUND DURING RESEARCH:
{gaps}
ENTITIES THE TASK NAMES THAT THE REPORT COULD NOT COVER WITH EVIDENCE: {missing}

Write 80-180 words: what remains open, which sources were not available, where the reader should verify or
follow up. No citations, no heading."""

ENTAIL = """For each pair, does the QUOTE support the SENTENCE (same facts, numbers, scope)?
{pairs}
Answer one line per pair: "<index>: yes" or "<index>: no". Nothing else."""

CLARIFY = """A user wants a research report. Decide whether you must ask clarifying questions before planning.

TASK:
{task}

Return JSON: {{"questions": ["..."], "assumptions": ["..."]}}
Rules:
- Ask at most 2 short questions, only about things that change the research substantially (scope, jurisdiction,
  time range, intended use, required format) and that the task does not already answer.
- Never ask about two aspects of the same dimension. If the task is specific enough, return no questions.
- "assumptions": what you will assume if the user does not answer.
- Write in the language of the task."""

EDIT_PLAN = """Update a research plan according to the user's instruction. Keep everything the user did not ask to change.

CURRENT PLAN (JSON):
{plan}

USER INSTRUCTION:
{instruction}

Return the complete updated plan as JSON with the same fields (title, task_core, language, parts, sub_questions,
must_name, constraints). At most {max_subq} sub-questions."""

ANALYSIS = """Write the part "{analysis_title}" of a research report in {language_name}. {spelling}

TASK: {task}
REQUIREMENTS OF THE TASK: {constraints}
REPORT PARTS (with evidence citations):
{parts}

Write {words} words that answer the task at a higher level than the individual parts: draw conclusions across the
parts, weigh trade-offs and competing views, explain implications and causal links, and give a clear, reasoned
assessment or recommendation where the task asks for one. You may restate facts from the parts with exactly the same
citation [E..] (one per sentence); your own reasoning gets no citation. No new facts. Use ### sub-headings if helpful.
Do not write the part title itself."""

# ---------------------------------------------------------------- focus engine
FOCUS_SYSTEM = """You are a research agent for the public administration of the canton of Basel-Stadt. Today is {today}.
You answer a research question with a short, fully sourced report in {language_name}. {spelling}

HOW TO WORK
- Find evidence with the tools. You have at most {max_calls} tool calls in total; plan them, several calls per turn
  are allowed. Official primary sources (laws, council business, official publications and statistics) come
  before media and private sites.
- Every tool result shows source IDs like [S12] next to each item. Only these IDs can be cited.
- web_search returns titles, URLs and snippets. When a snippet is not enough (exact conditions, fees, dates,
  figures), read the page with fetch_page.
- Stop when the question is answered or the budget is used, then write the report as your final answer
  (no tool call in it).
{tool_hints}
SEARCH RULES
- Short keyword queries (3-8 words) in the language of the source: German for Swiss German-speaking
  authorities, French for Romandie, Italian for Ticino, English for international topics.
- Quotes only around identifiers (law or business numbers). No site: operators unless you search one official site.
{query_rules}

THE REPORT
- Structure: "# <title>", "## {summary}" (3-6 sentences that answer the question), then the main part as
  "## " sections that follow the question's structure (and the plan, if one is given), then
  "## {open_points}" with concrete items: what was not found, which source was unavailable, what to verify.
- Length: as long as the evidence carries, typically about {words} words. No filler, no speculation about
  motives, no general statements without a source.
- Every sentence with a fact ends with the ID of the source that states it, e.g. [S12]; at most two IDs per
  sentence ([S12][S15]). Never cite an ID that did not appear in a tool result, and never mention IDs in the
  text other than as such a citation. Your own conclusions get no
  ID and read as such ("Daraus folgt ...").
- Copy numbers, dates, names and identifiers exactly from the source. Give the date or Stand of a source
  where it matters ("Stand 1. Juni 2024", "in Kraft seit 16.01.2025").
- Compare several entities in a Markdown table; each row gets the ID of its source in the last cell; write
  "nicht belegt" where the evidence is silent. Cover exactly the entities the question names.
- No URLs and no source list: the numbered source list is generated from your IDs.
{writer_rules}"""

FOCUS_USER = """RESEARCH QUESTION:
{task}
{plan}{requirements}"""

FOCUS_FINAL = ("The tool budget is used up. Write the final report now from the evidence above, following the "
               "report rules. Cite only source IDs that appeared in tool results.")

# writer rules a preset can override (report.length_rule / report.format_rule); {words} is filled in
LENGTH_RULE = "Write up to {words} words - fewer if the evidence is thin; never pad with general statements."
FORMAT_RULE = "Use a Markdown table when comparing several entities, options or time periods; use ### sub-headings."
