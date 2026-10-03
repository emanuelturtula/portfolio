"""The image and the runtime manifest carry the scheduled backups' volume (#22, spec 029).

Criterion 10. The copies must live in a named volume of their own, mounted at
``/app/backups``, so that removing the data volume does not remove them; the application
must be told that directory through ``PORTFOLIO_BACKUP_DIR``; and the image must create the
directory owned by ``app``, because Docker gives a new named volume the owner of the image
directory it is mounted on, and the container runs as ``app``.

Standard library only, like the rest of this suite, so the manifest is read as text by a
reader that understands exactly the shape ``deploy/compose.yml`` has: two-space indentation,
block mappings and block sequences. A test of what a file says should not need the parser
whose behaviour it might be checking.
"""

from __future__ import annotations

import shlex
import unittest

from deploy_harness import KIT_COMPOSE, REPO_ROOT

DOCKERFILE = REPO_ROOT / "Dockerfile"
BACKUPS = "/app/backups"
DATA = "/app/data"


def block(lines: list[str], path: list[str]) -> list[str]:
    """The lines under ``path`` (e.g. ``["services", "app", "volumes"]``), comments dropped.

    Each key is found at the indentation its depth gives, and the block runs until a
    non-blank, non-comment line at that indentation or less.
    """
    start, indent = 0, -1
    for depth, key in enumerate(path):
        wanted = " " * (2 * depth) + f"{key}:"
        for number in range(start, len(lines)):
            if lines[number].rstrip() == wanted or lines[number].startswith(wanted + " "):
                start, indent = number + 1, 2 * depth
                break
        else:
            raise AssertionError(f"{':'.join(path[: depth + 1])} is not in the manifest")
    found = []
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        found.append(stripped)
    return found


def arguments_of(run: str, command: list[str]) -> list[list[str]]:
    """The arguments of each `command` in a joined ``RUN`` line, one list per occurrence.

    The line is split at ``&&`` first, so an argument always belongs to the command it
    follows and never to the one after it.
    """
    found = []
    for part in run.removeprefix("RUN ").split("&&"):
        words = part.split()
        if words[: len(command)] == command:
            found.append(words[len(command) :])
    return found


def runtime_environment(lines: list[str]) -> dict[str, str]:
    """Every variable an ``ENV`` sets after the last ``FROM``: the image that runs, by name.

    The earlier stages build and are thrown away, so a variable set there is not in the
    container. Each ``ENV`` is split as a shell would split it, so a quoted value keeps its
    spaces and loses its quotes. The legacy ``ENV NAME value`` form is refused rather than
    misread: every word must be ``NAME=value``.
    """
    last_from = max(number for number, line in enumerate(lines) if line.startswith("FROM "))
    found: dict[str, str] = {}
    for line in lines[last_from + 1 :]:
        if not line.startswith("ENV "):
            continue
        for word in shlex.split(line.removeprefix("ENV ")):
            name, equals, value = word.partition("=")
            if not equals:
                raise AssertionError(f"{line!r} is not in the NAME=value form")
            found[name] = value
    return found


def manifest() -> list[str]:
    return KIT_COMPOSE.read_text(encoding="utf-8").splitlines()


def instructions() -> list[str]:
    """The Dockerfile's instructions, with continuation lines joined and comments dropped."""
    joined: list[str] = []
    pending = ""
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        joined.append(" ".join((pending + stripped).split()))
        pending = ""
    return joined


class ComposeTests(unittest.TestCase):
    def test_the_application_is_told_where_the_copies_go(self) -> None:
        environment = block(manifest(), ["services", "app", "environment"])
        self.assertIn(f"PORTFOLIO_BACKUP_DIR: {BACKUPS}", environment)

    def test_the_copies_have_a_named_volume_of_their_own(self) -> None:
        mounts = block(manifest(), ["services", "app", "volumes"])
        self.assertEqual(mounts, [f"- data:{DATA}", f"- backups:{BACKUPS}"])
        self.assertEqual(block(manifest(), ["volumes"]), ["data:", "backups:"])

    def test_the_database_stays_on_the_data_volume(self) -> None:
        environment = block(manifest(), ["services", "app", "environment"])
        database = f"PORTFOLIO_DATABASE_URL: sqlite+aiosqlite:///{DATA}/portfolio.db"
        self.assertIn(database, environment)
        self.assertFalse(BACKUPS.startswith(DATA + "/"), "the copies would be in the data volume")

    def test_the_backup_directory_is_not_left_to_the_secrets_file(self) -> None:
        """``environment:`` overrides ``env_file:``, so the directory is fixed here, not there."""
        env_file = block(manifest(), ["services", "app", "env_file"])
        self.assertEqual(env_file, ["- ${PORTFOLIO_SECRETS_ENV_FILE:-/dev/null}"])

    def test_the_reader_finds_what_it_is_for(self) -> None:
        # A reader nobody has seen fail is a reader nobody knows the state of.
        sample = [
            "services:",
            "  app:",
            "    environment:",
            "      # a comment",
            "      A: 1",
            "    volumes:",
            "      - x:/x",
            "volumes:",
            "  x:",
        ]
        self.assertEqual(block(sample, ["services", "app", "environment"]), ["A: 1"])
        self.assertEqual(block(sample, ["services", "app", "volumes"]), ["- x:/x"])
        self.assertEqual(block(sample, ["volumes"]), ["x:"])
        with self.assertRaises(AssertionError):
            block(sample, ["services", "app", "ports"])


class DockerfileTests(unittest.TestCase):
    def test_both_directories_are_volumes(self) -> None:
        volumes = [line for line in instructions() if line.startswith("VOLUME ")]
        self.assertEqual(volumes, [f'VOLUME ["{DATA}", "{BACKUPS}"]'])

    def test_the_backup_directory_is_created_and_owned_by_app(self) -> None:
        (setup,) = [line for line in instructions() if line.startswith("RUN useradd ")]
        made = arguments_of(setup, ["mkdir", "-p"])
        owned = arguments_of(setup, ["chown", "app:app"])
        for directory in (DATA, BACKUPS):
            self.assertTrue(any(directory in each for each in made), f"{directory} not made")
            self.assertTrue(any(directory in each for each in owned), f"{directory} not owned")

    def test_the_command_splitter_keeps_each_command_to_itself(self) -> None:
        # A directory named only by the next command is not one this command made: the first
        # version of the test above was a regular expression that ran on past the `&&`.
        line = "RUN useradd app && mkdir -p /a && chown app:app /a /b"
        self.assertEqual(arguments_of(line, ["mkdir", "-p"]), [["/a"]])
        self.assertEqual(arguments_of(line, ["chown", "app:app"]), [["/a", "/b"]])
        self.assertEqual(arguments_of(line, ["rm", "-rf"]), [])

    def test_the_directories_exist_before_the_process_drops_to_app(self) -> None:
        lines = instructions()
        setup = next(n for n, line in enumerate(lines) if line.startswith("RUN useradd "))
        user = next(n for n, line in enumerate(lines) if line == "USER app")
        volume = next(n for n, line in enumerate(lines) if line.startswith("VOLUME "))
        self.assertLess(setup, user)
        self.assertLess(user, volume)

    def test_the_joiner_finds_what_it_is_for(self) -> None:
        # The same rule as above: the reader is shown to read before it is trusted.
        self.assertTrue(any(line.startswith("RUN useradd --uid 1000") for line in instructions()))
        self.assertIn("EXPOSE 8000", instructions())

    def test_the_image_itself_tells_the_application_where_the_copies_go(self) -> None:
        """R10: run without the compose file, the copies still land in their own volume.

        The setting's default, ``./data/backups``, is under the working directory ``/app``,
        which would put them inside the data volume. The image's value is the compose file's.
        """
        environment = runtime_environment(instructions())
        self.assertEqual(environment.get("PORTFOLIO_BACKUP_DIR"), BACKUPS)
        composed = block(manifest(), ["services", "app", "environment"])
        self.assertIn(f"PORTFOLIO_BACKUP_DIR: {environment['PORTFOLIO_BACKUP_DIR']}", composed)

    def test_the_environment_reader_reads_only_the_image_that_runs(self) -> None:
        sample = [
            "FROM builder AS build",
            "ENV LEFT_BEHIND=1",
            "FROM runtime",
            'ENV PATH="/app/.venv/bin:${PATH}" PYTHONPATH=/app/src',
            "ENV EMPTY= QUOTED='a b'",
            "RUN true",
        ]
        self.assertEqual(
            runtime_environment(sample),
            {
                "PATH": "/app/.venv/bin:${PATH}",
                "PYTHONPATH": "/app/src",
                "EMPTY": "",
                "QUOTED": "a b",
            },
        )
        with self.assertRaises(AssertionError):
            runtime_environment(["FROM runtime", "ENV LEGACY /app/backups"])
        self.assertEqual(runtime_environment(instructions())["PORTFOLIO_ENVIRONMENT"], "prod")


if __name__ == "__main__":
    unittest.main()
