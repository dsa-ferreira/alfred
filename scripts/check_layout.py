"""Check the code layout rule in AGENTS.md ("Code layout").

Parses ``src/alfred/`` and ``migrations/`` and reports, as ``path:line: message``:

1. a top-level name without ``_`` that no other source module uses;
2. a top-level ``_`` name that another source module imports;
3. module order: constants and variables, then public classes and functions,
   then ``_`` ones;
4. class order: attributes, dunders, public methods, then ``_`` methods;
5. two packages that import each other. A package is a subpackage of
   ``alfred`` or a top-level module (``alfred.history``); imports under
   ``TYPE_CHECKING`` and inside functions count too.

Python's definition order wins over the sections:

- an item that a later item needs at import time (a base class, a decorator,
  a default value, a module-level call) may sit in that item's section,
  directly above it;
- a variable that needs a public class or function at import time (such as
  ``X = A | B``) belongs to the public section, below what it needs.

Usage: ``python scripts/check_layout.py [root]`` (root defaults to the repo).
Exits 1 when it finds a violation. Uses only the standard library.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Public names that no source module imports because something else uses them:
# entry points in pyproject.toml, and the module attributes Alembic reads from a
# migration. (module pattern, name); a pattern ending in "*" matches a prefix.
ENTRY_POINTS = {
    ("alfred.cli", "run"),
    *(
        ("migrations.versions.*", name)
        for name in ("revision", "down_revision", "branch_labels", "depends_on")
    ),
    ("migrations.versions.*", "upgrade"),
    ("migrations.versions.*", "downgrade"),
}

# Module-level names that are never public, whatever their spelling: the
# module's logger is always ``log``.
ALWAYS_INTERNAL = {"log"}

SOURCE_DIRS = ("src/alfred", "migrations")

# The toolset contract every tool group and the agent build on; it imports
# nothing else from alfred at runtime, so it is not part of the package graph.
SHARED_CONTRACT = "alfred.tools.base"


@dataclass
class Module:
    name: str
    path: Path
    tree: ast.Module
    # Top-level names this module defines, with the line that defines them.
    defined: dict[str, int] = field(default_factory=dict)
    # Local name -> (module, name) for ``from alfred.x import name``.
    imported: dict[str, tuple[str, str]] = field(default_factory=dict)


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    modules = load_modules(root)
    problems = check(modules, root)
    for problem in sorted(problems, key=_sort_key):
        print(problem)
    return 1 if problems else 0


def load_modules(root: Path) -> dict[str, Module]:
    modules: dict[str, Module] = {}
    for source_dir in SOURCE_DIRS:
        base = root / source_dir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            name = _module_name(root, path)
            tree = ast.parse(path.read_text(), filename=str(path))
            modules[name] = Module(name, path, tree)
    for module in modules.values():
        module.defined = _defined_names(module.tree)
    return modules


def check(modules: dict[str, Module], root: Path) -> list[str]:
    problems: list[str] = []
    used = _names_used_elsewhere(modules)
    for module in modules.values():
        where = module.path.relative_to(root)
        for name, line in module.defined.items():
            if _is_dunder(name) or name in ALWAYS_INTERNAL:
                continue
            users = used.get((module.name, name), set())
            if name.startswith("_"):
                if users:
                    others = ", ".join(sorted(users))
                    problems.append(f"{where}:{line}: {name} starts with _ but {others} imports it")
            elif not users and not _is_entry_point(module.name, name):
                problems.append(f"{where}:{line}: only its module uses {name}; rename to _{name}")
        for line, message in _module_order(module.tree):
            problems.append(f"{where}:{line}: {message}")
        for node in ast.walk(module.tree):
            if isinstance(node, ast.ClassDef):
                for line, message in _class_order(node):
                    problems.append(f"{where}:{line}: {message}")
    problems += _package_cycles(modules, root)
    return problems


def _package_cycles(modules: dict[str, Module], root: Path) -> list[str]:
    # (from package, to package) -> where the first such import is
    first: dict[tuple[str, str], str] = {}
    for module in modules.values():
        source = _package(module.name)
        if source is None:
            continue
        for node in ast.walk(module.tree):
            for target in _imported_modules(node):
                package = _package(target)
                if package is not None and package != source:
                    where = f"{module.path.relative_to(root)}:{node.lineno}"
                    first.setdefault((source, package), where)
    problems = []
    for (source, target), where in first.items():
        back = first.get((target, source))
        if back is not None and source < target:
            problems.append(
                f"{where}: package {source} imports {target}, and {target} imports {source} "
                f"({back})"
            )
    return problems


def _package(module: str) -> str | None:
    """The package graph's node for ``module``; ``None`` outside the graph."""
    if not module.startswith("alfred.") or module == SHARED_CONTRACT:
        return None
    return ".".join(module.split(".")[:2])


def _imported_modules(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
        if node.module == "alfred":  # from alfred import notices
            return [f"alfred.{alias.name}" for alias in node.names]
        return [node.module]
    return []


def _sort_key(problem: str) -> tuple[str, int]:
    path, line, _ = problem.split(":", 2)
    return path, int(line)


def _module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root)
    parts = list(relative.with_suffix("").parts)
    if parts[0] == "src":
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _is_entry_point(module: str, name: str) -> bool:
    for pattern, entry in ENTRY_POINTS:
        if entry != name:
            continue
        if pattern == module or (pattern.endswith("*") and module.startswith(pattern[:-1])):
            return True
    return False


def _defined_names(tree: ast.Module) -> dict[str, int]:
    names: dict[str, int] = {}
    for node in tree.body:
        for name in _bound_names(node):
            names.setdefault(name, node.lineno)
    return names


def _bound_names(node: ast.stmt) -> list[str]:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return [node.name]
    if isinstance(node, ast.TypeAlias):
        return [node.name.id]
    if isinstance(node, ast.Assign):
        return [name for target in node.targets for name in _target_names(target)]
    if isinstance(node, ast.AnnAssign | ast.AugAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def _target_names(target: ast.expr) -> list[str]:
    """Names an assignment binds; ``x.y = ...`` and ``x[0] = ...`` bind none."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Tuple | ast.List):
        return [name for element in target.elts for name in _target_names(element)]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


def _names_used_elsewhere(modules: dict[str, Module]) -> dict[tuple[str, str], set[str]]:
    """(module, name) -> the other modules that use that top-level name."""
    used: dict[tuple[str, str], set[str]] = {}

    def use(target: str, name: str, user: str) -> None:
        origin = _resolve(modules, target, name)
        if origin[0] != user:
            used.setdefault(origin, set()).add(user)

    for module in modules.values():
        aliases: dict[str, str] = {}  # local name -> module, for module imports
        for node in ast.walk(module.tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                for alias in node.names:
                    submodule = f"{node.module}.{alias.name}"
                    local = alias.asname or alias.name
                    if submodule in modules:
                        aliases[local] = submodule
                    elif node.module in modules:
                        module.imported[local] = (node.module, alias.name)
                        use(node.module, alias.name, module.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in modules:
                        aliases[alias.asname or alias.name] = alias.name
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Attribute):
                target = _dotted(node.value)
                if target is None:
                    continue
                head, _, rest = target.partition(".")
                resolved = aliases.get(target) or (
                    f"{aliases[head]}.{rest}" if head in aliases and rest else None
                )
                if resolved in modules:
                    use(resolved, node.attr, module.name)
    return used


def _resolve(modules: dict[str, Module], module: str, name: str) -> tuple[str, str]:
    """Follow re-exports to the module that defines ``name``."""
    seen: set[tuple[str, str]] = set()
    while (module, name) not in seen:
        seen.add((module, name))
        found = modules.get(module)
        if found is None or name in found.defined or name not in found.imported:
            break
        module, name = found.imported[name]
    return module, name


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else None
    return None


# --- order -----------------------------------------------------------------


@dataclass
class _Item:
    node: ast.stmt
    names: list[str]
    rank: int
    label: str
    # Names this item looks up when Python executes its definition.
    needs: set[str]


_MODULE_SECTIONS = ("constant or variable", "public class or function", "_ class or function")
_CLASS_SECTIONS = ("class attribute", "dunder method", "public method", "_ method")


def _module_order(tree: ast.Module) -> list[tuple[int, str]]:
    return _order_problems(module_items(tree), pull_down=True)


def _class_order(cls: ast.ClassDef) -> list[tuple[int, str]]:
    return _order_problems(class_items(cls), pull_down=False)


def module_items(tree: ast.Module) -> list[_Item]:
    """The module's top-level items that have a section, in source order."""
    lazy = _has_future_annotations(tree)
    items = []
    for node in tree.body:
        rank = _module_rank(node)
        if rank is not None:
            names = _bound_names(node)
            items.append(_Item(node, names, rank, _MODULE_SECTIONS[rank], _needs(node, lazy)))
    return items


def class_items(cls: ast.ClassDef) -> list[_Item]:
    """The class body's items that have a section, in source order."""
    items = []
    for node in cls.body:
        rank = _class_rank(node)
        if rank is not None:
            names = _bound_names(node)
            # Class bodies resolve names as they run, so treat every lookup
            # in an attribute or decorator as needed now.
            items.append(_Item(node, names, rank, _CLASS_SECTIONS[rank], _needs(node, False)))
    return items


def effective_ranks(items: list[_Item], pull_down: bool) -> tuple[list[int], list[int]]:
    """Each item's section once Python's definition order is applied.

    Returns (down, effective). ``down``: with ``pull_down``, a variable that
    needs a public class or function at import time belongs to the public
    section, below what it needs. ``effective``: an item that a later item
    needs at import time moves up to that item's section (it must then sit
    directly above it).
    """
    down: list[int] = []
    for index, item in enumerate(items):
        rank = item.rank
        if pull_down and rank == 0:
            for earlier in range(index):
                if items[earlier].rank <= 1 and _provides(items[earlier], item):
                    rank = max(rank, down[earlier])
        down.append(rank)
    effective = list(down)
    for index in reversed(range(len(items))):
        for user in first_users(items, index):
            effective[index] = min(effective[index], effective[user])
    return down, effective


def first_users(items: list[_Item], index: int) -> list[int]:
    """Indexes of the later items that need ``items[index]`` at import time."""
    return [
        later for later in range(index + 1, len(items)) if _provides(items[index], items[later])
    ]


def _module_rank(node: ast.stmt) -> int | None:
    if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign | ast.TypeAlias):
        return 0
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return 2 if node.name.startswith("_") else 1
    return None


def _class_rank(node: ast.stmt) -> int | None:
    if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign | ast.TypeAlias):
        return 0
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        if _is_dunder(node.name):
            return 1
        return 3 if node.name.startswith("_") else 2
    return None


def _provides(item: _Item, user: _Item) -> bool:
    return any(name in user.needs for name in item.names)


def _order_problems(items: list[_Item], pull_down: bool) -> list[tuple[int, str]]:
    """Report items below a later section's item, and helpers placed too high.

    An item that a later item needs at import time may sit in that item's
    section, directly above it (other such helpers may stand in between).
    """
    down, effective = effective_ranks(items, pull_down)
    problems = []
    highest: int | None = None
    for index, item in enumerate(items):
        if highest is not None and effective[index] < effective[highest]:
            earlier = items[highest]
            problems.append(
                (
                    item.node.lineno,
                    f"{item.label} {_name(item)} is below {earlier.label} "
                    f"{_name(earlier)} (line {earlier.node.lineno})",
                )
            )
        elif highest is None or effective[index] >= effective[highest]:
            highest = index
    for index, item in enumerate(items):
        if effective[index] == down[index]:
            continue
        user = first_users(items, index)[0]
        between = range(index + 1, user)
        if any(effective[other] == down[other] for other in between):
            problems.append(
                (
                    item.node.lineno,
                    f"{item.label} {_name(item)} is above {_name(items[user])} only "
                    f"because that needs it; move it directly above it",
                )
            )
    return problems


def _name(item: _Item) -> str:
    return item.names[0] if item.names else "(statement)"


def _has_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def _needs(node: ast.stmt, lazy_annotations: bool) -> set[str]:
    """Names looked up when the statement runs (not when a function is called)."""
    roots: list[ast.AST] = []
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        roots += node.decorator_list
        roots += node.args.defaults
        roots += [d for d in node.args.kw_defaults if d is not None]
        if not lazy_annotations:
            roots += _annotations(node)
    elif isinstance(node, ast.ClassDef):
        roots += node.decorator_list + node.bases + [k.value for k in node.keywords]
        for statement in node.body:
            if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
                roots += statement.decorator_list + statement.args.defaults
                roots += [d for d in statement.args.kw_defaults if d is not None]
            else:
                roots.append(statement)
    elif isinstance(node, ast.TypeAlias):
        return set()
    elif isinstance(node, ast.AnnAssign):
        roots += [node.value] if node.value else []
        roots += [node.annotation]
    else:
        roots.append(node)
    names: set[str] = set()
    for root in roots:
        for sub in ast.walk(root):
            if isinstance(sub, ast.Name):
                names.add(sub.id)
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                # String annotations and forward references ("Foo").
                names.add(sub.value)
    return names


def _annotations(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.expr]:
    args = node.args
    every = args.posonlyargs + args.args + args.kwonlyargs
    every += [a for a in (args.vararg, args.kwarg) if a is not None]
    found = [a.annotation for a in every if a.annotation is not None]
    return found + ([node.returns] if node.returns else [])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
