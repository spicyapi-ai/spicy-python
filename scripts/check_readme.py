#!/usr/bin/env python3
"""Check that the Python examples in the README actually work.

Why this guard exists: two errors in the README - calling the module-level function output_assets
as if it were a client method, and omitting the required payload_version from verify_webhook -
turned no test red. The library was correct and only the documentation was wrong, while anyone
copying from the README hit an AttributeError and a TypeError as their first impression of this
SDK.

It checks three statically decidable things, which between them cover both shapes:
  1. every code block survives ast.parse (the syntax is intact);
  2. every method named in ``client.<method>(...)`` really exists on SpicyClient, and every
     ``client.assets.<method>(...)`` / ``client.assets.characters.<method>(...)`` on its namespace -
     with every required argument passed and no misspelled keyword;
  3. calls to module-level exports pass every required argument and no misspelled keyword.
Nothing is executed - the examples issue real network requests, which would be both slow and
billable.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import spicyapi  # noqa: E402
from spicyapi import SpicyAssetCharacters, SpicyAssets, SpicyClient  # noqa: E402

BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)
# These names come from the reader's own framework, not from our export surface.
FOREIGN = {"request", "os", "uuid", "json", "pathlib", "sys"}
# The namespaces reachable from a client, by attribute path. They are set on the instance, so the
# class alone cannot say what ``client.assets`` is.
NAMESPACES: dict[tuple[str, ...], type] = {
    (): SpicyClient,
    ("assets",): SpicyAssets,
    ("assets", "characters"): SpicyAssetCharacters,
}


def client_chain(func: ast.expr) -> list[str] | None:
    """``["assets", "create"]`` for ``client.assets.create``; None when it is not ``client...``."""
    parts: list[str] = []
    node = func
    while isinstance(node, ast.Attribute):
        parts.insert(0, node.attr)
        node = node.value
    if isinstance(node, ast.Name) and node.id == "client" and parts:
        return parts
    return None


def argument_problems(where: str, label: str, target: object, node: ast.Call) -> list[str]:
    """Required arguments missing from a call, and keywords the target does not take."""
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return []
    parameters = dict(signature.parameters)
    parameters.pop("self", None)
    problems: list[str] = []
    given = {kw.arg for kw in node.keywords if kw.arg}
    positional = len(node.args)
    splatted = any(kw.arg is None for kw in node.keywords) or any(
        isinstance(arg, ast.Starred) for arg in node.args
    )
    for name, parameter in parameters.items():
        if parameter.default is not inspect.Parameter.empty:
            continue
        if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if name in given:
            continue
        if parameter.kind is not inspect.Parameter.KEYWORD_ONLY and positional > 0:
            positional -= 1
            continue
        if not splatted:
            problems.append(f"{where}: {label}() is missing required argument {name}")
    if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        for name in sorted(given - set(parameters)):
            problems.append(f"{where}: {label}() has no argument named {name}")
    return problems


def main() -> int:
    readme = ROOT / "README.md"
    problems: list[str] = []
    blocks = BLOCK.findall(readme.read_text())
    if not blocks:
        print("check_readme: the README contains no python code block at all - the check itself "
              "has stopped working")
        return 1

    for index, source in enumerate(blocks, start=1):
        where = f"README.md python block {index}"
        try:
            tree = ast.parse(source)
        except SyntaxError as error:
            problems.append(f"{where}: syntax error {error.msg} (line {error.lineno})")
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            # 2. client.<method>(...) and client.<namespace>.<method>(...)
            func = node.func
            chain = client_chain(func)
            if chain is not None:
                *prefix, method = chain
                label = "client." + ".".join(chain)
                owner = NAMESPACES.get(tuple(prefix))
                if owner is None:
                    problems.append(
                        f"{where}: client.{'.'.join(prefix)} is not a namespace of SpicyClient"
                    )
                    continue
                target = getattr(owner, method, None)
                if not callable(target):
                    hint = ""
                    if not prefix and hasattr(spicyapi, method):
                        hint = (
                            f" - it is a module-level function, so write {method}(...) "
                            f"rather than client.{method}(...)"
                        )
                    problems.append(f"{where}: {owner.__name__} has no method {method}(){hint}")
                    continue
                problems.extend(argument_problems(where, label, target, node))
                continue

            # 3. required arguments of module-level exports
            if (
                isinstance(func, ast.Name)
                and func.id in spicyapi.__all__
                and func.id not in FOREIGN
            ):
                target = getattr(spicyapi, func.id, None)
                if not callable(target) or inspect.isclass(target):
                    continue
                try:
                    signature = inspect.signature(target)
                except (TypeError, ValueError):
                    continue
                given = {kw.arg for kw in node.keywords if kw.arg}
                positional = len(node.args)
                for name, parameter in signature.parameters.items():
                    if parameter.default is not inspect.Parameter.empty:
                        continue
                    if parameter.kind in (
                        inspect.Parameter.VAR_POSITIONAL,
                        inspect.Parameter.VAR_KEYWORD,
                    ):
                        continue
                    if name in given:
                        continue
                    if (
                        parameter.kind is not inspect.Parameter.KEYWORD_ONLY
                        and positional > 0
                    ):
                        positional -= 1
                        continue
                    problems.append(f"{where}: {func.id}() is missing required argument {name}")
                unknown = given - set(signature.parameters)
                for name in sorted(unknown):
                    problems.append(f"{where}: {func.id}() has no argument named {name}")

    if problems:
        print(f"check_readme: {len(problems)} README example(s) do not line up with the code\n")
        for problem in problems:
            print(f"  - {problem}")
        print("\nFix the README to match the code, not the library to match the README.")
        return 1

    print(f"check_readme: {len(blocks)} python block(s), every call lines up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
