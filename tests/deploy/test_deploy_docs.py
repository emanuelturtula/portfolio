"""The operator docs give commands that work against the layout deploy.py creates.

Before #94 every ``docker compose`` command in docs/operations.md pointed at
``<deploy-root>/compose.yml``, a file that did not exist, and none of them set the four
variables compose refuses to run without. The owner edited ``secrets.env`` on the Pi and
could not apply it. These tests keep that from coming back:

* nothing formatted as code may invoke ``docker compose`` directly -- whatever is in code
  formatting is what an operator pastes, and only ``compose.sh`` supplies the project, the
  file and the variables;
* every ``compose.sh`` command names it where it lives, ``~/portfolio-app/prod``;
* the legacy root, ``portfolio-app-deploy``, is named only where the migration is described.

Design specs under docs/specs/ are records of decisions, not instructions, and are exempt.
"""

from __future__ import annotations

import re
import unittest
from collections.abc import Iterator
from pathlib import Path

from deploy_harness import REPO_ROOT, deploy

DOCS = REPO_ROOT / "docs"
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^#{1,6}\s")
INLINE = re.compile(r"`([^`]+)`")
DOCKER_COMPOSE = re.compile(r"\bdocker[ -]compose\b")
COMPOSE_SH = re.compile(r"(\S*)compose\.sh\b")
LIVE = "~/portfolio-app/prod"


def operator_docs() -> list[Path]:
    return sorted(p for p in DOCS.rglob("*.md") if "specs" not in p.relative_to(DOCS).parts)


def code(path: Path) -> Iterator[tuple[int, str, str]]:
    return code_in(path.read_text(encoding="utf-8"))


def code_in(text: str) -> Iterator[tuple[int, str, str]]:
    """Yield (line number, "fenced" or "inline", text) for everything in code formatting."""
    fenced = False
    for number, line in enumerate(text.splitlines(), start=1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            yield number, "fenced", line
        else:
            for span in INLINE.findall(line):
                yield number, "inline", span


def headed_lines(path: Path) -> Iterator[tuple[int, str, str]]:
    return headed_lines_in(path.read_text(encoding="utf-8"))


def headed_lines_in(text: str) -> Iterator[tuple[int, str, str]]:
    """Yield (line number, nearest heading above or on it, line), ignoring fenced '#'."""
    heading, fenced = "", False
    for number, line in enumerate(text.splitlines(), start=1):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced and HEADING.match(line):
            heading = line
        yield number, heading, line


class OperatorDocsTests(unittest.TestCase):
    def test_the_docs_exist(self) -> None:
        names = {path.name for path in operator_docs()}
        self.assertTrue({"deployment.md", "operations.md"} <= names, names)

    def test_nothing_in_code_formatting_invokes_docker_compose_directly(self) -> None:
        offenders = [
            f"{path.relative_to(REPO_ROOT)}:{number} ({kind}): {text.strip()}"
            for path in operator_docs()
            for number, kind, text in code(path)
            if DOCKER_COMPOSE.search(text)
        ]
        self.assertEqual(offenders, [], "use ~/portfolio-app/prod/compose.sh instead")

    def test_no_command_points_at_a_compose_file_that_does_not_exist(self) -> None:
        for path in operator_docs():
            text = path.read_text(encoding="utf-8")
            for stale in ("<deploy-root>/compose.yml", "<deploy root>", "-p portfolio-app-prod"):
                with self.subTest(doc=path.name, stale=stale):
                    self.assertNotIn(stale, text)

    def test_every_compose_sh_command_names_where_it_lives(self) -> None:
        commands = 0
        for path in operator_docs():
            directory = ""
            for number, kind, text in code(path):
                if kind == "fenced" and text.strip().startswith("cd "):
                    directory = text.strip()[3:].strip()
                for prefix in COMPOSE_SH.findall(text):
                    invocation = kind == "fenced" or text.strip().startswith(prefix + "compose.sh ")
                    if not invocation:
                        continue  # prose naming the file, such as `compose.sh` or `prod/compose.sh`
                    commands += 1
                    where = f"{path.name}:{number}: {text.strip()}"
                    if prefix == "./":
                        self.assertEqual(directory, LIVE, f"{where} runs from the wrong directory")
                    else:
                        self.assertEqual(prefix, LIVE + "/", where)
        self.assertGreaterEqual(commands, 8, "the docs lost their compose.sh commands")

    def test_the_legacy_root_is_named_only_where_the_migration_is_described(self) -> None:
        mentions = 0
        for path in operator_docs():
            for number, heading, line in headed_lines(path):
                if "portfolio-app-deploy" in line:
                    mentions += 1
                    self.assertRegex(
                        heading, re.compile("migrat", re.IGNORECASE), f"{path.name}:{number}"
                    )
        self.assertGreater(mentions, 0, "the migration must still be described")

    def test_applying_a_secrets_change_is_the_documented_recreate(self) -> None:
        operations = (DOCS / "operations.md").read_text(encoding="utf-8")
        recreate = f"{LIVE}/compose.sh up -d --force-recreate app"
        self.assertIn(recreate, operations.splitlines())
        self.assertIn(recreate, (DOCS / "deployment.md").read_text(encoding="utf-8"))
        self.assertIn(f"{LIVE}/secrets.env", (DOCS / "deployment.md").read_text(encoding="utf-8"))

    def test_deploy_root_is_redefined_as_the_live_directory(self) -> None:
        paragraphs = (DOCS / "operations.md").read_text(encoding="utf-8").split("\n\n")
        definition = next(p for p in paragraphs if "`<deploy-root>`" in p)
        self.assertIn(f"`{LIVE}`", definition)

    def test_the_deploy_py_docstring_describes_the_layout(self) -> None:
        doc = deploy.__doc__ or ""
        for name in ("~/portfolio-app/", "compose.sh", "backup/", "failed/", "last-attempt.json"):
            self.assertIn(name, doc)

    def test_the_rules_catch_what_they_are_for(self) -> None:
        # A guard nobody has seen fail is a guard nobody knows the state of.
        sample = "\n".join(
            [
                "## Applying a change",
                "```bash",
                "docker compose -p portfolio-app-prod -f <deploy-root>/compose.yml up app",
                "# ~/portfolio-app-deploy is not a heading inside a fence",
                "```",
                "Run `docker compose exec app true`, or `<deploy-root>/compose.sh ps`.",
                "## Migrating",
                "The old root was ~/portfolio-app-deploy.",
            ]
        )
        found = [text for _, _, text in code_in(sample) if DOCKER_COMPOSE.search(text)]
        self.assertEqual(len(found), 2)
        prefixes = [p for _, _, text in code_in(sample) for p in COMPOSE_SH.findall(text)]
        self.assertEqual(prefixes, ["<deploy-root>/"])
        headings = [h for _, h, line in headed_lines_in(sample) if "portfolio-app-deploy" in line]
        self.assertEqual(headings, ["## Applying a change", "## Migrating"])


if __name__ == "__main__":
    unittest.main()
