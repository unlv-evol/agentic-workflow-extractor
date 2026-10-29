# awe — Agentic Workflow Extractor

`awe` mines the git history of CI workflow files and detects **coding-agent
invocations inside them**: steps that call Claude Code, Codex, Copilot, Gemini,
Cursor, Aider and similar agents as part of a repository's continuous
integration.

It is built to support a dataset of *agentic CI workflow histories* at the scale
of ~10^5 GitHub repositories.

## Why detection is content-based

Existing context-file miners (e.g. `cofee`, which extracts `CLAUDE.md` /
`AGENTS.md` histories) can classify by **path**: the filename identifies the
agent. That does not work here. An agentic CI workflow is an ordinary
`.github/workflows/whatever.yml`, indistinguishable by name from a build or
lint workflow. `awe` therefore parses each revision of each workflow file,
walks its jobs and steps, and matches each step against an agent catalogue.

### Evidence tiers

Not every mention of an agent is an agent invocation. A workflow that sets
`ANTHROPIC_API_KEY` may simply be feeding an API-backed test suite. Evidence is
therefore tiered:

| kind   | meaning                                              | strength |
| ------ | ---------------------------------------------------- | -------- |
| `uses` | step invokes a dedicated agent GitHub Action         | strong   |
| `run`  | step shell-invokes an agent CLI                      | strong   |
| `path` | the workflow file has a reserved, agent-specific name | strong   |
| `job`  | a job carries a reserved, agent-specific id          | weak     |
| `url`  | step talks to an LLM inference endpoint              | weak     |
| `env`  | an agent/LLM credential is bound at that scope       | weak     |

Not every agent announces itself in a step. GitHub's Copilot coding agent is
wired in by *reserved filename*: GitHub reads
`.github/workflows/copilot-setup-steps.yml` and runs the job of the same name,
and neither carries a `uses:` or `run:` that names Copilot. A purely
content-based detector scores those repositories as zero. Only the filename is
strong evidence -- the reserved file is the contract GitHub honours, whereas
the reserved job id in some other file is a copied template GitHub never reads.

`env` evidence is attributed **once at the scope that declares it**: a step's
own `env:` to that step, a job's to the job, the workflow's to the workflow.
Folding outer scopes into each step would emit one row per step in the file,
claiming `actions/checkout` is an agent invocation because the workflow exports
`ANTHROPIC_API_KEY` at the top.

A revision is agentic only if it carries **strong** evidence. Pass
`--include-weak` to relax this (useful for measuring the false-positive
boundary during manual validation).

## Installation

```bash
pip install -e .          # or: pip install -r requirements.txt
```

Requires Python 3.11+ (uses `tomllib`) and `git` 2.30+. The only runtime
dependency is PyYAML.

## Usage

```bash
# one repository, agent steps to stdout as CSV
awe /path/to/clone

# many repositories, full CSV outputs plus a content store
awe --from-file repos.txt -o out/ --content-dir out/content
```

Key options:

| option | effect |
| ------ | ------ |
| `-c, --config` | detector catalogue (TOML); defaults to `config/default.toml` |
| `--from-file`  | file listing one clone path per line |
| `-o, --out-dir`| write `revisions.csv`, `steps.csv`, `repositories.csv` |
| `--content-dir`| store each distinct revision as `<sha256>.yml` |
| `--since`      | ignore commits older than a date |
| `--include-weak` | count `env`/`url` evidence as sufficient |
| `--keep-all`   | emit every workflow revision, not just agentic histories |
| `--no-prefetch`| disable the batched blob prefetch (see below) |

### What gets emitted

By default only the histories of files that are agentic **at some point** are
emitted — but their *entire* history comes with them, including revisions from
before the agent was introduced and after it was removed. This keeps adoption
and abandonment observable while avoiding storage of every CI workflow revision
ever made.

Repository-level denominators are recorded regardless, so the proportion of a
repository's workflows that are agentic remains computable.

For the MSR 2027 dataset run on a server, see [docs/FULL_RUN.md](docs/FULL_RUN.md)
(preflight checks, launch, monitoring, resume, retries).

## Cloning strategy (important)

Clone with a **size-capped blob filter**:

```bash
git clone --filter=blob:limit=64k --no-checkout --single-branch \
    https://github.com/OWNER/NAME.git
```

A size cap fetches everything `awe` reads in a single server-built packfile
while leaving large blobs behind. Both caps produce byte-identical output; the
choice is a time/bandwidth trade that splits by repository type:

| repository | class | cap | total | disk |
| ---------- | ----- | --- | ----- | ---- |
| vite | ordinary | **64k** | **5 s** | **31 MB** |
| vite | | 1m | 7 s | 66 MB |
| langchain | large | **64k** | **18 s** | **108 MB** |
| langchain | | 1m | 33 s | 300 MB |
| godot | large | **64k** | **42 s** | **454 MB** |
| godot | | 1m | 71 s | 1176 MB |
| amplihack | gh-aw adopter | 64k | 41 s | 48 MB |
| amplihack | | **1m** | **16 s** | 196 MB |

`64k` is the default. It wins everywhere except on gh-aw adopters, whose
compiled workflows reach 240 kB and so have to be backfilled -- and a backfill
costs a full fetch negotiation per repository however few objects it needs. On
*large* repositories 64k is about 1.8x faster and 2.7x smaller, because a large
repository's bulk is sub-1MB source files, which a 1m cap pulls and a 64k cap
does not. Since gh-aw adopters are ~1% of repositories, 1m would optimise the
1% and tax the rest. Pass `CAP=1m` when mining a gh-aw-heavy list.

For comparison, the strategies that lose outright:

| strategy | vite | uv |
| -------- | ---- | -- |
| blobless (`--filter=blob:none`) + prefetch | 210 s | >300 s (timeout) |
| full clone, no filter | 13 s | 72 s |

Blobless clones are a trap here. On one, `git cat-file` backfills missing blobs
one network round trip at a time, and requesting the object ids up front does
not fix it: `git fetch <oids>` costs server-side resolution against the whole
repository, roughly independent of how many ids you ask for. `uv` never
completed at all. `awe` still detects a partial clone and prefetches in bulk as
a fallback (disable with `--no-prefetch`), but a `blob:limit` clone is faster
and simpler.

After this change **extraction, not cloning, is the bottleneck** on large
repositories: `uv` spends 53 s of its 65 s CPU-bound in YAML parsing.
Extraction parallelises across repositories; clone bandwidth does not. Size a
large run on cores rather than network.

A workflow file larger than the cap is not lost -- `awe` detects which blobs
are absent locally (no network: `cat-file` under `GIT_NO_LAZY_FETCH`) and
bulk-fetches exactly those. The blob-accounting columns make the outcome
checkable rather than assumed: a clean run has `n_blobs_missing == 0` for every
repository.

## GitHub Agentic Workflows

`gh-aw` is GitHub's first-party agentic workflow framework. An agent is
authored as Markdown in `.github/workflows/` and compiled by `gh aw compile`
into an ordinary `.github/workflows/<name>.lock.yml`; both halves are
committed, so both appear in history.

Detection here is **exact rather than inferred**. The compiler stamps a
machine-readable header into every file it generates:

```
# gh-aw-metadata: {"schema_version":"v4","agent_id":"copilot", ...}
# gh-aw-manifest: {"secrets":[...],"actions":[...],"mcp_servers":[...]}
# This file was automatically generated by gh-aw. DO NOT EDIT.
```

The header layout has changed across compiler releases and both generations
appear in history, so both are parsed: in v0.44 there is only the
generated-by marker, v0.56 (schema `v2`) adds `gh-aw-metadata` after an
ASCII-art banner, and by schema `v4` the metadata and manifest are the first
two lines. `compiler_version` and `schema_version` are recorded per revision,
which makes the framework's own evolution part of the dataset.

So this tier needs no catalogue, no evidence tiering and no manual validation
of whether a match is real -- the framework identifies itself. It also carries
a much richer payload than "an agent is present here": which agent and which
version, the MCP tools it may call, the outputs it is permitted to emit, its
network egress policy and whether it runs sandboxed. Every one of those is a
constraint a maintainer chose, and all of them are visible over history.

Both halves are collected, because only the source carries the tool grants and
output constraints while only the compiled file carries resolved versions,
pinned action SHAs and container digests.

## Output schema

### `revisions.csv` — one row per touch of a matched file

`repository`, `uid`, `commit_hash`, `author_name`, `author_email`,
`committer_name`, `committer_email`, `authored_date`, `commit_date`,
`file_path`, `previous_file_path`, `file_hash`, `previous_file_hash`,
`content_sha256`, `git_change_type`, `symbolic_link`, `previous_symbolic_link`,
`parse_ok`, `workflow_name`, `triggers`, `n_jobs`, `n_steps`, `permissions`,
`agents`, `strong_agents`, `n_evidence`, `agentic`, `actor_gated`

`uid` identifies a logical file across content changes **and renames**. A file
that is deleted and later recreated at the same path receives a new `uid`.

### `steps.csv` — one row per agent invocation


`repository`, `uid`, `commit_hash`, `commit_date`, `file_path`,
`content_sha256`, `parse_ok`, `agent`, `scope`, `evidence_kind`,
`evidence_kinds`, `n_evidence`, `pattern`, `job_id`, `step_index`, `step_name`,
`action`, `action_version`, `triggers`, `tasks`

Evidence items are collapsed to one row per invocation, so counting rows counts
invocations. A step that both `uses:` an agent action and binds that agent's
credential is one invocation carrying two evidence items, not two rows.

`scope` is `step` when the evidence is attached to a specific step, or `file`
when it describes the revision as a whole -- a reserved filename, a reserved job
id, a workflow-level credential, a gh-aw manifest. `evidence_kind` names the
most decisive item (ranked `ghaw`, `uses`, `run`, `path`, `job`, `url`, `env`),
`evidence_kinds` lists all of them, and `n_evidence` says how many were
collapsed.

`tasks` holds purpose labels (`review`, `triage`, `implement`, `test`, `docs`,
`security`, `scheduled`, `on_demand`) inferred from workflow triggers and from
workflow/job/step names.

### `ghaw_manifests.csv` — one row per gh-aw artifact revision

`repository`, `uid`, `commit_hash`, `commit_date`, `file_path`,
`content_sha256`, `artifact_kind`, `parse_ok`, `agent_id`, `engine_version`,
`schema_version`, `compiler_version`, `model`, `strict`, `n_tools`, `tools`, `n_mcp_tools`,
`mcp_tools`, `n_safe_outputs`, `safe_outputs`, `n_secrets`, `secrets`,
`n_actions`, `n_containers`, `n_imports`, `imports`, `sandboxed`, `network`,
`permissions`, `n_evals`, `timeout_minutes`, `max_turns`, `frontmatter_keys`

`artifact_kind` is `source` (the authored Markdown) or `lock` (the compiled
workflow). List-valued columns are `|`-joined, as elsewhere in the schema.

### `repositories.csv` — one row per repository

`repository`, `n_workflow_files`, `n_workflow_revisions`, `n_agentic_files`,
`n_agentic_revisions`, `agents`, `first_agentic_date`, `last_agentic_date`,
`n_blobs_requested`, `n_blobs_read`, `n_blobs_missing`, `status`

Written even when a repository contains no agentic workflows — and also when
one fails outright (`status = error`), so that adoption rates have an honest
denominator. A repository that silently vanishes from this file is
indistinguishable from one that was never sampled.

`status` is `ok`, `incomplete` (some blobs never arrived, so detection is
under-counted for that repository) or `error` (traversal failed). On a partial
clone an unfetched blob is simply absent, which otherwise looks exactly like a
repository with no agentic workflows; the counters make the difference
visible.

## Detector catalogue

`config/default.toml` is the catalogue. Extending it needs no code changes:

```toml
[agents.myagent]
display_name = "My Agent"
uses = ["myorg/my-agent-action"]
run  = ["myagent --run"]
env  = ["MYAGENT_API_KEY"]
```

### The frozen dataset catalogue

`config/catalogue.toml` is what `scripts/mine.sh` uses. It is not a third list
of patterns but a merge: it `include`s `default.toml` and `marketplace.toml`,
unions their patterns per agent group, renames groups with `[aliases]` (the
generator's `qodo` is the hand-written `pr_agent`), and reassigns individual
`uses` patterns with `[overrides]`. Neither source file is edited, so the
generated one can still be regenerated and diffed. Freeze it before mining:
the validation sample only holds for the patterns that produced the data.

`uses` patterns match an action exactly -- `owner/repo` matches
`owner/repo@v1` and `owner/repo/sub@v1`, not `owner/repo-other` -- while the
other kinds match as substrings.

### Regenerating it from the Marketplace

`config/default.toml` is hand-written and small. It is being superseded by
`config/marketplace.toml`, generated from the GitHub Actions Marketplace
`ai-assisted` category:

```bash
python3 scripts/build_catalogue.py -o config/marketplace.toml
```

The generator enumerates the category, resolves each listing to its source
repository, and groups repositories belonging to a known agent vendor under
that agent, with everything else under `generic_ai`. This replaces deriving
the catalogue by hand from vendor documentation, which does not scale and does
not stay true: three entries in the hand-written catalogue name repositories
that no longer exist, so those patterns can never match, and it misses the long
tail of third-party agent actions entirely.

Output is dated and committed, so a dataset build is reproducible without
network access. Vendor grouping rules live in `_AGENT_RULES` in the generator.

## Degraded parsing

Workflow revisions mid-history frequently fail to parse as YAML. When that
happens `awe` falls back to a whole-file text scan, records `parse_ok = false`,
and reports at most one evidence item per agent (patterns overlap across kinds:
`claude-code` occurs inside `anthropics/claude-code-action`). Downstream users
can filter on `parse_ok`.

## Traversal semantics

History is walked with `--first-parent`, so only the mainline of each
repository is considered; commits that only ever existed on merged side
branches are not visited. Rename detection uses git's default similarity
threshold via `-M`. File modes are read from the raw log output, so symbolic
links are flagged rather than silently treated as regular files.

## Tests

```bash
PYTHONPATH=src python3 -m pytest tests -q
```

43 tests cover glob semantics, uid propagation across renames and
delete/recreate cycles, evidence tiering, the YAML-boolean `on:` quirk,
degraded parsing, reserved-name detection, env scope attribution, blob
accounting, gh-aw manifest parsing across compiler generations, and
end-to-end extraction.

## Status

Prototype. Extraction, detection and the clone strategy are working, measured
and tested. The hand-written `config/default.toml` is being superseded by the
generated `config/marketplace.toml`; three entries in the hand-written
catalogue name repositories that no longer exist and can never match.

## License

MIT.
