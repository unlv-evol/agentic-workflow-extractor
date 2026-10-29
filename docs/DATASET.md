# GHAgentCI: dataset README

Histories of GitHub Actions workflow files that invoke a coding agent, mined
from 196,938 active GitHub repositories with `awe` and the frozen catalogue
`config/catalogue.toml`. The paper (MSR 2027 Data and Tool Showcase) describes
how it was built and validated; this file documents the tables field by field.

## Files

| file | unit of observation |
|---|---|
| `repositories.csv.gz` | one repository of the sampling frame, agentic or not |
| `revisions.csv.gz` | one touch of one workflow file by one commit, for every file that is agentic at some point |
| `steps.csv.gz` | one agent invocation in one revision |
| `ghaw_manifests.csv.gz` | one revision of one GitHub Agentic Workflows (gh-aw) artifact |
| `content.tar.gz` | every distinct workflow revision of an emitted history, named `<content_sha256>.yml` |
| `frame-2026-10-03.csv` | the sampling frame: SEART metadata plus the `ghagentfiles_comparable` flag |

Tables join on `repository`, on `(repository, uid)` for a file history, and on
`(repository, uid, commit_hash)` for a revision. `content_sha256` joins a
revision to its content. List-valued columns are `|`-separated. Dates are ISO
8601 with the committer's own UTC offset; compare them as instants, not as
strings.

## `repositories`

| field | meaning |
|---|---|
| `repository` | `owner/name` as listed in the frame |
| `n_commits` | commits reachable from the default branch |
| `n_workflow_files`, `n_workflow_revisions` | all workflow files and their revisions, agentic or not |
| `n_agentic_files`, `n_agentic_revisions` | files agentic at some point, and agentic revisions |
| `agents` | agent groups detected, in order of first detection |
| `first_agentic_date`, `last_agentic_date` | first and last agentic revision |
| `n_blobs_requested`, `n_blobs_read`, `n_blobs_missing` | workflow blobs asked for and read; a complete extraction has `n_blobs_missing = 0` |
| `status` | `ok`, `incomplete` (blobs missing), `clone-failed`, `extract-failed`, `error` or `excluded` |

Failed and excluded repositories keep a row, so the frame is the denominator.
`excluded` marks the four development repositories of gh-aw itself
(`[exclude]` in the catalogue), which were not mined.

## `revisions`

| field | meaning |
|---|---|
| `repository`, `uid`, `commit_hash` | the revision; `uid` identifies a logical file |
| `author_*`, `committer_*`, `authored_date`, `commit_date` | git metadata |
| `file_path`, `previous_file_path` | path after and before the commit |
| `file_hash`, `previous_file_hash` | git blob ids; empty for a deletion |
| `content_sha256` | SHA-256 of the content, the name of its file in `content.tar.gz` |
| `git_change_type` | `Added`, `Modified`, `Deleted` or `Renamed` |
| `symbolic_link`, `previous_symbolic_link` | whether the file is (was) a symbolic link |
| `parse_ok` | whether the revision is valid YAML; otherwise detection used a text scan |
| `workflow_name`, `triggers`, `n_jobs`, `n_steps` | the workflow's `name:`, its `on:` events, and its size |
| `permissions` | the workflow-level `permissions:` block, flattened; empty if none (job-level blocks are not included) |
| `agents`, `strong_agents` | agents with any evidence, and with strong evidence |
| `n_evidence` | evidence items in the revision |
| `agentic` | whether the revision carries strong evidence (see "Reproducing the paper") |
| `actor_gated` | heuristic: an `if:` condition refers to the triggering actor or their association |

`uid` survives content changes and renames; a file deleted and later recreated
at the same path receives a new one. A file enters the table with its whole
history once any revision is agentic, including revisions before the agent was
introduced and after it was removed.

## `steps`

| field | meaning |
|---|---|
| `repository`, `uid`, `commit_hash`, `commit_date`, `file_path`, `content_sha256`, `parse_ok` | the revision |
| `agent` | agent group, e.g. `claude`; `generic_ai` for unattributed Marketplace actions; for gh-aw, the declared engine, `unknown` when the header names none |
| `scope` | `step` when located at a job and step; `file` when the evidence concerns the whole workflow (reserved file name, workflow-level credential, gh-aw header) |
| `evidence_kind` | the deciding evidence: `ghaw`, `uses`, `run`, `path` (strong) or `job`, `url`, `env` (weak) |
| `evidence_kinds`, `n_evidence` | all evidence collapsed into this invocation |
| `pattern` | the catalogue pattern that matched |
| `job_id`, `step_index`, `step_name` | location; `step_index` restarts in every job, and is `-1` for a job that calls a reusable workflow |
| `action`, `action_version` | for `uses`, the action and its reference (tag, branch or 40-character commit SHA) |
| `triggers` | the workflow's events |
| `tasks` | heuristic purpose labels from triggers and names: `review`, `triage`, `implement`, `test`, `docs`, `security`, `scheduled`, `on_demand` |

One row is one invocation: evidence on the same step, or on the file as a
whole, is collapsed into a single row decided by its strongest kind. Rows are
written for agentic revisions only, and include their weak-evidence
invocations.

## `ghaw_manifests`

One row per revision of a gh-aw artifact: `artifact_kind` is `lock` (the
compiled `*.lock.yml`) or `source` (the authored Markdown). Columns:
`agent_id` (engine), `engine_version`, `schema_version`, `compiler_version`,
`model`, `strict`, `tools`, `mcp_tools` (`server:tool`), `safe_outputs`
(permitted outputs), `secrets`, `n_actions`, `n_containers`, `imports`,
`sandboxed` (a `sandbox` key is declared), `network` (`defaults`, `custom` or
empty), `permissions`, `n_evals`, `timeout_minutes`, `max_turns` and
`frontmatter_keys`, with `n_*` counts beside list columns. Lock files carry the
header fields, sources the frontmatter fields; compilers before schema v4 wrote
less of the header, so older lock files have empty columns.

## Reproducing the paper's figures

The released tables are what `awe` emitted. The paper applies two filters on
top, both in `scripts/paper_stats.py`:

1. **Named agents only.** `agentic` and `agents` count the `generic_ai` group
   too. The paper counts a revision as agentic only with strong evidence for a
   named agent (any `agent` other than `generic_ai`), and reports
   generic-only repositories separately.
2. **No invocation before 30 November 2022** (the public release of ChatGPT).
   Earlier matches are gh-aw false positives: ordinary
   Markdown files whose frontmatter carries a key such as `tools`.

"Latest snapshot" means the last revision of each file history, deleted files
excluded.

## Special cases

* **Symbolic links.** Nine revisions, all in `elastic/ai-github-actions`, are
  symbolic links (`symbolic_link = True`). A link does not change when its
  target does, so its history is not the target's; handle such files
  separately.
* **Unparsed revisions** (`parse_ok = False`) were classified by a text scan
  that keeps one evidence item per agent and has no step location.
* **gh-aw engines.** `unknown` agents are gh-aw lock files whose header names
  no engine; the files are certain gh-aw artifacts.
* **Failed repositories.** `clone-failed` rows are mostly repositories deleted
  or made private after the SEART query, plus a few very large mirrors that
  exceeded a one-hour budget even with a 1 MB blob cap.
