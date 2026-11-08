"""Text normalisation for chunk content (Stage C). Raw text is kept separately.

- NFKC fixes typographic ligatures (fi/ff/ffl ligature glyphs) and compatibility
  forms; the rupee sign and curly quotes are untouched.
- Private-use glyphs (Word "Symbol" bullets, U+F0B7 and neighbours) become a bullet.
- Letter-spaced words ("T e l e p h o n e", "F a x") are closed up.
- Lines are joined with a space; a line ending in "-" is joined without one and the
  hyphen is kept. The corpus PDFs come from Word/Illustrator, which do not soft-hyphenate,
  so a trailing hyphen is a compound ("Money-Laundering", "e-document"), not a break.
- Underscore fill-in blanks collapse to "___".
Code points are written with chr() so the source holds no invisible characters.
"""

import re
import unicodedata

BULLET = chr(0x2022)
PRIVATE_USE = re.compile(f"[{chr(0xE000)}-{chr(0xF8FF)}]")
UNDERSCORES = re.compile(r"_{3,}")
SPACES = re.compile(f"[ \t{chr(0xA0)}]+")
LETTER_SPACED = re.compile(r"\b(?:[A-Za-z] ){2,}[A-Za-z]\b")  # three or more single letters
NOISE_MIN_ALNUM_WORD = 3  # a line needs one alphanumeric run this long to carry meaning
LATIN_MIN_SHARE = 0.5  # share of letters that must be Latin script


def normalize_line(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = PRIVATE_USE.sub(BULLET, text)
    text = UNDERSCORES.sub("___", text)
    text = SPACES.sub(" ", text).strip()
    return LETTER_SPACED.sub(lambda m: m.group(0).replace(" ", ""), text)


def join_lines(lines: list[str]) -> str:
    out = ""
    for raw in lines:
        line = normalize_line(raw)
        if not line:
            continue
        if not out:
            out = line
        elif out.endswith("-") and not out.endswith(" -"):
            out += line
        else:
            out += " " + line
    return out


def is_non_latin(text: str) -> bool:
    """Mostly non-Latin letters, e.g. the garbled Devanagari masthead on RBI p1."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    latin = sum("LATIN" in unicodedata.name(c, "") for c in letters)
    return latin / len(letters) < LATIN_MIN_SHARE


def is_noise(text: str, min_run: int = NOISE_MIN_ALNUM_WORD) -> bool:
    """No word-like run: box grids, rules, OCR debris ('| | |', '———', '=)').

    Native form labels can be two letters ("ID", "No"), so callers pass `min_run=2` there;
    OCR keeps the default, where two-letter fragments are usually debris.
    """
    return not re.search(rf"[A-Za-z0-9]{{{min_run},}}", normalize_line(text))


def collapse_vertical_letters(lines: list[str]) -> list[str]:
    """Runs of >= 3 single-character lines are one vertically printed word ("SURNAME")."""
    out: list[str] = []
    run: list[str] = []
    for line in [*lines, ""]:  # sentinel flushes the last run
        stripped = line.strip()
        if len(stripped) == 1 and stripped.isalnum():
            run.append(stripped)
            continue
        if len(run) >= 3:
            out.append("".join(run))
        else:
            out.extend(run)
        run = []
        if line:
            out.append(line)
    return out


def word_count(text: str) -> int:
    return len(text.split())
