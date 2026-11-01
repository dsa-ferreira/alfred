"""Model Markdown to Telegram HTML, and splitting long replies (design D9).

Only a small subset is converted: bold, italic, strikethrough, inline code,
fenced code, links, bullet lists and headings (as bold). Everything else is
escaped, so only ``<``, ``>`` and ``&`` ever need care. If Telegram still
rejects the result, the caller resends the Markdown as plain text.

Splitting happens on the Markdown source, before conversion: at blank lines,
then newlines, then a hard cut. A fenced code block that crosses a split is
closed at the end of one part and reopened at the start of the next.
"""

from __future__ import annotations

import html
import re

TELEGRAM_LIMIT = 4096
# Splitting is done on the source; leave room for tags and escaping.
SOURCE_LIMIT = 3500

_FENCE = re.compile(r"^\s*```")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*|__(?!\s)(.+?)(?<!\s)__")
_ITALIC = re.compile(
    r"(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])|(?<![\w_])_(?![\s_])(.+?)(?<![\s_])_(?![\w_])"
)
_STRIKE = re.compile(r"~~(?!\s)(.+?)(?<!\s)~~")
_PLACEHOLDER = re.compile("\x00(\\d+)\x00")


def to_telegram_html(markdown: str) -> str:
    out: list[str] = []
    lines = markdown.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE.match(line):
            language = line.strip()[3:].strip()
            body: list[str] = []
            i += 1
            while i < len(lines) and not _FENCE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # the closing fence (or the end of the text)
            code = html.escape("\n".join(body), quote=False)
            if language and re.fullmatch(r"[\w+#.-]+", language):
                out.append(f'<pre><code class="language-{language}">{code}</code></pre>')
            else:
                out.append(f"<pre>{code}</pre>")
            continue
        if heading := _HEADING.match(line):
            out.append(f"<b>{_inline(heading.group(1))}</b>")
        elif bullet := _BULLET.match(line):
            out.append(f"{bullet.group(1)}• {_inline(bullet.group(2))}")
        else:
            out.append(_inline(line))
        i += 1
    return "\n".join(out)


def _inline(text: str) -> str:
    """Convert inline Markdown in one line; everything else is escaped."""
    saved: list[str] = []

    def keep(fragment: str) -> str:
        saved.append(fragment)
        return f"\x00{len(saved) - 1}\x00"

    text = text.replace("\x00", "")
    text = _INLINE_CODE.sub(
        lambda m: keep(f"<code>{html.escape(m.group(1), quote=False)}</code>"), text
    )
    text = _LINK.sub(
        lambda m: keep(f'<a href="{html.escape(m.group(2))}">{_inline(m.group(1))}</a>'), text
    )
    text = html.escape(text, quote=False)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)
    text = _STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", text)
    return _PLACEHOLDER.sub(lambda m: saved[int(m.group(1))], text)


def split_markdown(text: str, limit: int = SOURCE_LIMIT) -> list[str]:
    """Split ``text`` into parts of at most ``limit`` characters (plus fence lines)."""
    parts: list[str] = []
    rest = text.strip("\n")
    reopen = ""
    while rest:
        rest = reopen + rest
        if len(rest) <= limit:
            parts.append(rest)
            break
        cut, skip = _cut_point(rest, limit, after=len(reopen))
        part, rest = rest[:cut], rest[cut + skip :]
        fence = _open_fence(part)
        if fence is not None:
            part += "\n```"
            reopen = fence + "\n"
        else:
            reopen = ""
        parts.append(part)
    return parts


def _cut_point(text: str, limit: int, after: int) -> tuple[int, int]:
    """Where to cut, and how many separator characters to drop there.

    The cut is always past ``after`` (a reopened fence line), so every part
    makes progress.
    """
    window = text[:limit]
    for separator in ("\n\n", "\n"):
        at = window.rfind(separator)
        if at > after:
            return at, len(separator)
    return limit, 0


def _open_fence(part: str) -> str | None:
    """The opening fence line if ``part`` ends inside a code block."""
    opening: str | None = None
    for line in part.split("\n"):
        if _FENCE.match(line):
            opening = None if opening is not None else line.strip()
    return opening


def split_for_telegram(text: str) -> list[tuple[str, str]]:
    """``(markdown, html)`` pairs, each html at most Telegram's message length."""
    result: list[tuple[str, str]] = []
    pending = [(part, SOURCE_LIMIT) for part in split_markdown(text)]
    while pending:
        part, limit = pending.pop(0)
        rendered = to_telegram_html(part)
        if len(rendered) <= TELEGRAM_LIMIT or limit < 200:
            result.append((part, rendered))
            continue
        # Escaping made it too long: split this part again, smaller.
        smaller = limit // 2
        pending[:0] = [(p, smaller) for p in split_markdown(part, smaller)]
    return result
