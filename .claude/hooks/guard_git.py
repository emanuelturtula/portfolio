"""Block git commands that would bypass this project's safety rails.

Wired as a ``PreToolUse`` hook on ``Bash``. The rules it enforces are the ones the
repository's own configuration cannot: the branch ruleset stops a direct push to ``main``
on the server, but an agent can still waste a long run getting there, and ``--no-verify``
silently skips the secret scan on a public repository.

``--no-verify`` is refused after every subcommand. Its short form ``-n`` is refused only
where git makes it one -- ``git commit`` and ``git am`` -- because everywhere else git
documents ``-n`` as something harmless: ``git grep -n`` numbers lines, ``git log -n 5``
limits output, ``git push -n`` is a dry run. Telling the two apart means reading the
command as the shell and git will: words, not text. A regular expression over the raw
text cannot see that ``-an`` holds an ``-n``, that ``-c x=y`` is not the subcommand, or
that ``-m "grep -n"`` is a commit message.

Merging is deliberately *not* blocked. Nobody outside the repository can merge regardless
of this hook: that requires write access, and the branch ruleset has no bypass actors. A
local block would only have obstructed the people who are supposed to merge. The convention
that an implementer reports a pull request URL rather than merging its own work lives in
the agent role definitions, where it belongs, because it is a workflow norm, not a security
control.

Exits 2 with an explanation to deny the command.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterator

NO_VERIFY = (
    "--no-verify skips the secret scan the git hooks run. On a public repository that is "
    "the one hook you cannot afford to skip. Fix what the hook reported instead."
)

RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\bgit\s+push\b.*(?:--force\b|--force-with-lease\b|\s-f\b)"),
        "A force push rewrites history. If a secret was committed, force-pushing does not "
        "remove it from the remote's object database, it only hides it -- the value must "
        "be rotated. If you are trying to clean up commits, rebase locally and push "
        "normally.",
    ),
    (
        re.compile(r"\bgit\s+push\b.*\borigin\s+(?:HEAD:)?main\b"),
        "Direct pushes to main are blocked by the branch ruleset. Open a pull request: "
        "branch, push the branch, then 'gh pr create'.",
    ),
    (
        re.compile(r"\bgit\s+stash\b(?!\s+(?:push|list|show))"),
        "A bare 'git stash' or 'git stash pop' shares one stack with every other worktree "
        "and session on this machine, so it can silently swallow or restore someone "
        "else's work. Make a temporary commit instead.",
    ),
]

# Where -n is --no-verify, and which of the subcommand's short options take a value. A
# value is the rest of the option's word, or the next word when nothing follows the letter,
# so the n in 'git commit -mn' is a message and the n in 'git am -mn' is not. A letter
# listed here by mistake would hide a real -n: these come from 'git commit -h' and
# 'git am -h', and each combination the tests pin was checked against git itself (#66).
SHORT_N_IS_NO_VERIFY: dict[str, tuple[str, str]] = {
    # subcommand: (letters that need a value, letters whose value is optional)
    "commit": ("CFUcmt", "Su"),
    "am": ("Cp", "S"),
}

# Where git documents -n as something other than --no-verify ('git help <subcommand>').
# Any other subcommand still has a standalone -n refused, because an alias for commit
# looks exactly like one.
SHORT_N_IS_HARMLESS: dict[str, str] = {
    "add": "--dry-run",
    "blame": "--show-number",
    "check-ignore": "--non-matching",
    "cherry-pick": "--no-commit",
    "clean": "--dry-run",
    "clone": "--no-checkout",
    "fetch": "--no-tags",
    "format-patch": "--numbered",
    "grep": "--line-number",
    "log": "--max-count",
    "merge": "--no-stat",
    "mv": "--dry-run",
    "notes": "--dry-run, for prune",
    "prune": "--dry-run",
    "pull": "--no-stat",
    "push": "--dry-run",
    "rebase": "--no-stat",
    "reflog": "--dry-run, for expire and delete",
    "repack": "do not run update-server-info",
    "rev-list": "--max-count",
    "revert": "--no-commit",
    "rm": "--dry-run",
    "shortlog": "--numbered",
    "submodule": "--summary-limit",
    "tag": "-n<num>, lines of annotation",
    "worktree": "--dry-run, for prune",
}

# git's own options, before the subcommand, that take the next word as their value.
GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--attr-source"}
)

# git accepts any unambiguous prefix of a long option, and 'git am --no-v' is one.
SHORTEST_NO_VERIFY = "--no-v"

GIT = re.compile(r"(?:.*[\\/])?git(?:\.exe)?", re.IGNORECASE)
OPERATOR_CHARS = frozenset(";&|()<>`\n")
WORD_BREAKS = OPERATOR_CHARS | {" ", "\t"}
MAX_NESTING = 5

PROTECTED_BRANCH = "main"

HEREDOC = re.compile(
    r"<<-?\s*(['\"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=tag_quote)?.*?^\s*(?P=tag)\s*$".replace(
        "(?P=tag_quote)", r"\1"
    ),
    re.DOTALL | re.MULTILINE,
)

Token = tuple[str, bool]


def executable_part(command: str) -> str:
    """Return the command with here-document bodies removed.

    The rules below describe the very patterns a commit message is most likely to discuss:
    a commit explaining why bare ``git stash`` is forbidden would otherwise be refused for
    containing the words. Only the part of the command the shell executes is inspected; the
    text piped into it is data.
    """
    return HEREDOC.sub("<<HEREDOC", command)


def shell_words(command: str) -> list[Token]:
    """Split a command into ``(text, is_operator)`` tokens the way the shell does.

    Quotes and backslashes are removed, so ``-"n"`` and ``\\-n`` both come out as the
    ``-n`` git receives, and an operator inside quotes is part of a word rather than the
    end of a command. Raises ``ValueError`` on an unterminated quote.
    """
    tokens: list[Token] = []
    word: list[str] = []
    in_word = False
    index = 0

    def finish_word() -> None:
        nonlocal in_word
        if in_word:
            tokens.append(("".join(word), False))
        word.clear()
        in_word = False

    while index < len(command):
        char = command[index]
        following = command[index + 1 : index + 2]
        if char in " \t\r":
            finish_word()
            index += 1
        elif char in OPERATOR_CHARS:
            finish_word()
            end = index
            while end < len(command) and command[end] in OPERATOR_CHARS:
                end += 1
            tokens.append((command[index:end], True))
            index = end
        elif char == "\\":
            if following != "\n":  # A backslash before a newline joins two lines.
                word.append(following)
            in_word = True
            index += 2
        elif char == "$" and following in ("'", '"'):
            index += 1  # $'...' and $"..." are quotes too.
        elif char == "'":
            end = command.find("'", index + 1)
            if end < 0:
                raise ValueError("unterminated single quote")
            word.append(command[index + 1 : end])
            in_word = True
            index = end + 1
        elif char == '"':
            index += 1
            while index < len(command) and command[index] != '"':
                escaped = command[index + 1 : index + 2]
                if command[index] == "\\" and escaped and escaped in '$`"\\\n':
                    if escaped != "\n":
                        word.append(escaped)
                    index += 2
                else:
                    word.append(command[index])
                    index += 1
            if index >= len(command):
                raise ValueError("unterminated double quote")
            in_word = True
            index += 1
        else:
            word.append(char)
            in_word = True
            index += 1
    finish_word()
    return tokens


def words_of(command: str) -> list[Token]:
    try:
        return shell_words(command)
    except ValueError:
        # Often an apostrophe in a comment, which the shell ignores. Reading every quoted
        # word as code instead can only ever refuse more, never less.
        return shell_words(command.replace("'", "").replace('"', ""))


def simple_commands(tokens: list[Token]) -> Iterator[list[str]]:
    """Yield the words of each command between ``;``, ``&&``, ``|``, newlines and so on.

    A redirection is not a boundary: ``git commit 2>&1 -n`` still hands ``-n`` to git.
    """
    words: list[str] = []
    for text, is_operator in tokens:
        if not is_operator:
            words.append(text)
        elif "<" not in text and ">" not in text:
            yield words
            words = []
    yield words


def is_no_verify(word: str) -> bool:
    name = word.split("=", 1)[0]
    return len(name) >= len(SHORTEST_NO_VERIFY) and "--no-verify".startswith(name)


def has_short_n(options: list[str], need_value: str, may_take_value: str) -> bool:
    """True if a short option word in ``options`` hands git a ``-n`` flag."""
    skip_value = False
    for word in options:
        if skip_value:
            skip_value = False
            continue
        if len(word) < 2 or word[0] != "-" or word[1] == "-":
            continue
        letters = word[1:]
        for position, letter in enumerate(letters):
            if letter == "n":
                return True
            if letter in need_value:
                skip_value = position == len(letters) - 1
                break
            if letter in may_take_value:
                break
    return False


def git_refusal(arguments: list[str]) -> str | None:
    """Why one git invocation would skip the hooks, given the words after ``git``."""
    index = 0
    while index < len(arguments) and arguments[index].startswith("-"):
        index += 2 if arguments[index] in GLOBAL_OPTIONS_WITH_VALUE else 1
    if index >= len(arguments):
        return None
    subcommand = arguments[index]
    options = arguments[index + 1 :]
    if "--" in options:  # Everything after it is a path or a pattern.
        options = options[: options.index("--")]

    if any(is_no_verify(word) for word in options):
        return NO_VERIFY
    if subcommand in SHORT_N_IS_HARMLESS:
        return None
    if subcommand in SHORT_N_IS_NO_VERIFY:
        if has_short_n(options, *SHORT_N_IS_NO_VERIFY[subcommand]):
            return f"-n is --no-verify for 'git {subcommand}'. {NO_VERIFY}"
        return None
    if "-n" in options:
        return (
            "-n is --no-verify for git commit and git am, and this guard cannot tell "
            f"whether 'git {subcommand}' is one of them: an alias could be. If -n means "
            f"something harmless there, spell out its long option. {NO_VERIFY}"
        )
    return None


def no_verify_refusal(command: str, depth: int = 0) -> str | None:
    """Why the command would skip the git hooks, or ``None`` if it would not."""
    tokens = words_of(command)
    for words in simple_commands(tokens):
        for position, word in enumerate(words):
            if GIT.fullmatch(word):
                reason = git_refusal(words[position + 1 :])
                if reason:
                    return reason
    if depth < MAX_NESTING:
        for text, is_operator in tokens:
            if not is_operator and could_hold_a_git_command(text):
                reason = no_verify_refusal(text, depth + 1)
                if reason:
                    return reason
    return None


def could_hold_a_git_command(word: str) -> bool:
    """True for a quoted word that the shell may run: bash -c '...', "$(...)", eval."""
    return "git" in word.lower() and not WORD_BREAKS.isdisjoint(word)


def current_branch() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return result.stdout.strip()


def deny(message: str) -> int:
    print(f"Refused: {message}", file=sys.stderr)
    return 2


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # Fail open: the server-side ruleset is still behind this.

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0
    command = executable_part(str(tool_input.get("command", "")))
    if not command:
        return 0

    reason = no_verify_refusal(command)
    if reason:
        return deny(reason)

    for pattern, message in RULES:
        if pattern.search(command):
            return deny(message)

    # Committing straight onto main wastes a whole run: the push is rejected at the end.
    if re.search(r"\bgit\s+commit\b", command) and current_branch() == PROTECTED_BRANCH:
        return deny(
            f"You are on {PROTECTED_BRANCH}. Every change lands through a pull request: "
            "create a feature/<issue>-<slug> branch first."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
