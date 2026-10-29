"""Loading and validation of the awe detector catalogue."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# Evidence kinds ordered by how much confidence they carry.
#
# `path` and `job` cover agents that are wired into CI by *reserved name*
# rather than by an action reference: GitHub's Copilot coding agent is
# configured through `.github/workflows/copilot-setup-steps.yml` with a job of
# the same name, and carries no `uses:` or `run:` to match. A purely
# content-based detector cannot see it, which silently zeroes out one of the
# largest agents in the ecosystem.
#
# Only the filename is strong. The reserved *file* is the contract GitHub
# actually honours; the reserved job id in some other file is a copied
# template that GitHub never reads, so it corroborates rather than decides.
# `ghaw` is not catalogue-driven: GitHub Agentic Workflows stamp their own
# manifest into every compiled file, so the framework identifies itself and
# there is no pattern for an author to declare. See awe/ghaw.py.
STRONG_EVIDENCE = ("uses", "run", "path", "ghaw")
WEAK_EVIDENCE = ("url", "env", "job")
EVIDENCE_KINDS = STRONG_EVIDENCE + WEAK_EVIDENCE

# The subset an author may declare under [agents.*] in the catalogue.
CATALOGUE_KINDS = ("uses", "run", "path", "job", "url", "env")


@lru_cache(maxsize=None)
def _action_regex(pattern: str) -> re.Pattern:
    """An action reference, bounded so that it names exactly that action.

    A plain substring test lets `chinmaymk/ra` match `chinmaymk/ra-anything@v1`
    or `xchinmaymk/ra`, which a catalogue of a thousand third-party actions
    turns into a steady supply of false positives. Bounded, `owner/repo`
    matches `owner/repo@v1` and `owner/repo/sub@v1` -- a subdirectory action or
    reusable workflow of the same repository -- and nothing else.
    """
    return re.compile(
        r"(?<![\w.-])" + re.escape(pattern.lower().strip("/")) + r"(?![\w.-])"
    )


@dataclass(frozen=True)
class AgentDetector:
    """Content patterns identifying one coding agent."""

    key: str
    display_name: str
    patterns: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def match(self, kind: str, haystack: str) -> str | None:
        """Return the first pattern of `kind` occurring in `haystack`.

        `uses` patterns name actions and must match one exactly (see
        `_action_regex`); the other kinds are free text and match as
        substrings.
        """
        if not haystack:
            return None
        lowered = haystack.lower()
        for pattern in self.patterns.get(kind, ()):
            if kind == "uses":
                if _action_regex(pattern).search(lowered):
                    return pattern
            elif pattern.lower() in lowered:
                return pattern
        return None


@dataclass(frozen=True)
class TaskDetector:
    """Heuristics assigning a purpose to an agent step."""

    key: str
    name_hints: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    paths: dict[str, tuple[str, ...]]
    agents: tuple[AgentDetector, ...]
    tasks: tuple[TaskDetector, ...]
    exclude: tuple[str, ...] = ()

    def is_excluded(self, repository: str) -> bool:
        """Whether this repository is recorded but deliberately not mined."""
        lowered = repository.lower()
        return any(lowered == name.lower() for name in self.exclude)

    @property
    def pathspecs(self) -> list[str]:
        """All globs, flattened, for use as git pathspecs."""
        return [glob for globs in self.paths.values() for glob in globs]

    def path_group(self, file_path: str) -> str | None:
        """Which path group a repository-relative path belongs to."""
        candidate = PurePosixPathLike(file_path)
        for group, globs in self.paths.items():
            if any(candidate.match_glob(glob) for glob in globs):
                return group
        return None


class PurePosixPathLike:
    """Minimal glob matcher over posix paths.

    Supports `*` (no separator crossing) and `**` (any number of directories),
    matching the pathspec semantics git itself applies.
    """

    def __init__(self, path: str) -> None:
        self.path = path.strip("/")

    def match_glob(self, glob: str) -> bool:
        from fnmatch import fnmatchcase

        glob = glob.strip("/")
        if "**" not in glob:
            # fnmatch's `*` crosses `/`, so compare segment counts too.
            if glob.count("/") != self.path.count("/"):
                return False
            return fnmatchcase(self.path, glob)
        prefix, _, suffix = glob.partition("**")
        prefix = prefix.rstrip("/")
        suffix = suffix.lstrip("/")
        if prefix and not self.path.startswith(prefix + "/"):
            return False
        remainder = self.path[len(prefix) + 1 :] if prefix else self.path
        if not suffix:
            return True
        segments = remainder.split("/")
        for start in range(len(segments)):
            if fnmatchcase("/".join(segments[start:]), suffix):
                return True
        return False


class ConfigError(ValueError):
    """Raised when the detector catalogue is malformed."""


def _merge(base: dict, extra: dict, override: bool = False) -> dict:
    """Combine two raw catalogues: lists are unioned, tables merged key by key.

    Union rather than override, because the catalogues being combined are
    complementary: the hand-written one carries `run`, `env`, `path` patterns
    and the [paths], [tasks] and [exclude] sections, the generated one carries
    the long tail of `uses` patterns. Scalars (`display_name`) keep the first
    value seen, unless `override` is set: a file's own values beat those of
    the files it includes.
    """
    merged = dict(base)
    for key, value in extra.items():
        if key not in merged:
            merged[key] = value
        elif isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge(merged[key], value, override)
        elif isinstance(merged[key], list) and isinstance(value, list):
            seen = {str(v).lower() for v in merged[key]}
            merged[key] = merged[key] + [v for v in value if str(v).lower() not in seen]
        elif override:
            merged[key] = value
    return merged


def _load_raw(path: Path, stack: tuple[Path, ...] = ()) -> dict:
    """Read one catalogue, expanding its `include` list first.

    Included files are resolved relative to the including file and merged in
    order; the including file's own sections are merged last. Agent groups are
    renamed by [aliases] and individual `uses` patterns reassigned by
    [overrides] after merging, so both apply across every included file.
    """
    path = path.resolve()
    if path in stack:
        raise ConfigError(f"{path}: include cycle")
    with open(path, "rb") as handle:
        raw = tomllib.load(handle)

    includes = raw.pop("include", [])
    if not isinstance(includes, list):
        raise ConfigError(f"{path}: include must be a list of paths")
    merged: dict = {}
    for name in includes:
        merged = _merge(merged, _load_raw(path.parent / name, stack + (path,)))
    aliases = raw.pop("aliases", {})
    overrides = raw.pop("overrides", {})
    merged = _merge(merged, raw, override=True)

    agents = merged.get("agents", {})
    for old, new in aliases.items():
        if old in agents:
            renamed = agents.pop(old)
            renamed.pop("display_name", None)
            agents[new] = _merge(agents.get(new, {}), renamed)
    for pattern, target in overrides.items():
        if target not in agents:
            raise ConfigError(f"{path}: override target '{target}' is not an agent")
        moved = False
        for key, spec in agents.items():
            kept = [u for u in spec.get("uses", []) if u.lower() != pattern.lower()]
            if len(kept) != len(spec.get("uses", [])):
                spec["uses"] = kept
                moved = True
        if not moved:
            raise ConfigError(f"{path}: override pattern '{pattern}' matches no uses pattern")
        agents[target].setdefault("uses", []).append(pattern)
    for key in [k for k, spec in agents.items() if not any(spec.get(kind) for kind in CATALOGUE_KINDS)]:
        del agents[key]  # emptied by overrides
    return merged


def load_config(path: str | Path) -> Config:
    raw = _load_raw(Path(path))

    paths = raw.get("paths")
    if not paths:
        raise ConfigError(f"{path}: missing or empty [paths] section")

    normalised_paths: dict[str, tuple[str, ...]] = {}
    for group, globs in paths.items():
        if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
            raise ConfigError(f"{path}: [paths].{group} must be a list of strings")
        normalised_paths[group] = tuple(globs)

    agents: list[AgentDetector] = []
    for key, spec in raw.get("agents", {}).items():
        patterns = {}
        for kind in CATALOGUE_KINDS:
            values = spec.get(kind, [])
            if not isinstance(values, list):
                raise ConfigError(f"{path}: [agents.{key}].{kind} must be a list")
            if values:
                patterns[kind] = tuple(str(v) for v in values)
        if not patterns:
            raise ConfigError(f"{path}: agent '{key}' declares no patterns")
        agents.append(
            AgentDetector(
                key=key,
                display_name=spec.get("display_name", key),
                patterns=patterns,
            )
        )
    if not agents:
        raise ConfigError(f"{path}: no [agents.*] sections declared")

    tasks = tuple(
        TaskDetector(
            key=key,
            name_hints=tuple(spec.get("name_hints", [])),
            triggers=tuple(spec.get("triggers", [])),
        )
        for key, spec in raw.get("tasks", {}).items()
    )

    excluded = raw.get("exclude", {}).get("repositories", [])
    if not isinstance(excluded, list) or not all(isinstance(e, str) for e in excluded):
        raise ConfigError(f"{path}: [exclude].repositories must be a list of strings")

    return Config(
        paths=normalised_paths,
        agents=tuple(agents),
        tasks=tasks,
        exclude=tuple(excluded),
    )
