"""
What one edit hunk changed, read from its diff lines.

The classes separate work that is not rework (only adding, only removing) and
cosmetic rewrites (the same text on other line breaks, comments) from changes
to code, sized by how many tokens differ.
"""
import difflib
import re
from dataclasses import dataclass

CONSTRUCTION = 'construction'    # only adds: every removed line is still there
DELETION = 'deletion'            # only removes
REFLOW = 'reflow'                # the same text on other line breaks or spacing
COMMENT = 'comment'              # only comments changed
CODE_SMALL = 'code_small'        # up to SMALL_TOKENS tokens of code changed
CODE_MEDIUM = 'code_medium'      # up to MEDIUM_TOKENS
CODE_LARGE = 'code_large'
UNKNOWN = 'unknown'              # the transcript carried no diff lines

SMALL_TOKENS = 3
MEDIUM_TOKENS = 15
MATCH_LIMIT_TOKENS = 2000

# A line that is only a comment, by its opening. Language-agnostic on purpose,
# so the body of a multi-line docstring or block comment reads as code. A `#`
# that opens a preprocessor directive (the word right after it, as in
# `#include`) or a shebang is code; `# if this fails` is a comment.
_COMMENT_RE = re.compile(
    r'^\s*(#(?!!|(?:include|define|undef|if|ifdef|ifndef|elif|else|endif|pragma|error|warning|import|region|endregion)\b)'
    r'|//|/\*|\*|;|--\s|"""|\'\'\'|<!--)')
_TOKEN_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|\S')
_SPACE_RE = re.compile(r'\s+')


@dataclass(frozen=True)
class Change:
    kind: str
    tokens_changed: int
    added_code: int       # lines added that are code
    added_comment: int    # lines added that are only a comment


def is_comment(line: str) -> bool:
    return bool(_COMMENT_RE.match(line))


def _squeezed(lines: list[str]) -> str:
    return _SPACE_RE.sub('', ''.join(lines))


def count_added(lines: list[str]) -> tuple[int, int]:
    """(code lines, comment-only lines) among non-blank `lines`."""
    kept = [line for line in lines if line.strip()]
    comments = sum(1 for line in kept if is_comment(line))
    return len(kept) - comments, comments


def classify(diff_lines: list[str]) -> Change:
    """`diff_lines` as a patch hunk carries them: '-' removed, '+' added, ' ' context."""
    removed = [line[1:] for line in diff_lines if line.startswith('-') and line[1:].strip()]
    added = [line[1:] for line in diff_lines if line.startswith('+') and line[1:].strip()]
    code, comments = count_added(added)
    if not removed and not added:
        return Change(UNKNOWN, 0, 0, 0)
    if not added:
        return Change(DELETION, 0, 0, 0)
    kept = {line.strip() for line in added}
    if all(line.strip() in kept for line in removed):
        return Change(CONSTRUCTION, 0, code, comments)
    if _squeezed(removed) == _squeezed(added):
        return Change(REFLOW, 0, code, comments)
    code_removed = [line for line in removed if not is_comment(line)]
    code_added = [line for line in added if not is_comment(line)]
    if _squeezed(code_removed) == _squeezed(code_added):
        return Change(COMMENT, 0, code, comments)
    before = _TOKEN_RE.findall(' '.join(code_removed))
    after = _TOKEN_RE.findall(' '.join(code_added))
    if len(before) + len(after) > MATCH_LIMIT_TOKENS:
        # Matching is quadratic; a hunk this size is a large change however it aligns.
        return Change(CODE_LARGE, max(len(before), len(after)), code, comments)
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    changed = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != 'equal')
    kind = CODE_SMALL if changed <= SMALL_TOKENS else CODE_MEDIUM if changed <= MEDIUM_TOKENS else CODE_LARGE
    return Change(kind, changed, code, comments)
