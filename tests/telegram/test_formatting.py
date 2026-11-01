from __future__ import annotations

import re

import pytest

from alfred.telegram.formatting import (
    TELEGRAM_LIMIT,
    split_for_telegram,
    split_markdown,
    to_telegram_html,
)


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("**disk full**", "<b>disk full</b>"),
        ("__disk full__", "<b>disk full</b>"),
        ("*maybe*", "<i>maybe</i>"),
        ("_maybe_", "<i>maybe</i>"),
        ("~~gone~~", "<s>gone</s>"),
        ("run `ls -la <dir>`", "run <code>ls -la &lt;dir&gt;</code>"),
        (
            "see [docs](https://x.lan/a?b=1&c=2)",
            'see <a href="https://x.lan/a?b=1&amp;c=2">docs</a>',
        ),
        ("- one\n- **two**", "• one\n• <b>two</b>"),
        ("* one\n  + nested", "• one\n  • nested"),
        ("1. first\n2. second", "1. first\n2. second"),
        ("## Status", "<b>Status</b>"),
        ("a < b && c > d", "a &lt; b &amp;&amp; c &gt; d"),
        ("<b>not a tag</b>", "&lt;b&gt;not a tag&lt;/b&gt;"),
        ("snake_case_name and 2*3*4", "snake_case_name and 2*3*4"),
        ("**bold with *italic* inside**", "<b>bold with <i>italic</i> inside</b>"),
        ("`**not bold**`", "<code>**not bold**</code>"),
    ],
)
def test_inline_and_lines(markdown: str, expected: str) -> None:
    assert to_telegram_html(markdown) == expected


def test_fenced_code() -> None:
    markdown = "Run:\n```bash\necho '<hi>' && **x**\n```\ndone"
    assert to_telegram_html(markdown) == (
        "Run:\n<pre><code class=\"language-bash\">echo '&lt;hi&gt;' &amp;&amp; **x**"
        "</code></pre>\ndone"
    )


def test_fenced_code_without_language_and_unclosed() -> None:
    assert to_telegram_html("```\na < b") == "<pre>a &lt; b</pre>"


def words(text: str) -> list[str]:
    return [w for w in re.split(r"\s+", text.replace("```", " ")) if w]


def test_6000_character_reply_split_without_losing_content() -> None:
    paragraphs = [f"Paragraph {i}: " + " ".join(f"w{i}x{j}" for j in range(60)) for i in range(16)]
    reply = "\n\n".join(paragraphs)[:6000]
    assert len(reply) == 6000
    parts = split_markdown(reply)
    assert len(parts) >= 2
    assert all(len(p) <= 3500 for p in parts)
    assert words(" ".join(parts)) == words(reply)
    # Split at paragraph boundaries.
    assert all(p.startswith("Paragraph") for p in parts)


def test_long_line_hard_cut() -> None:
    reply = "x" * 8000
    parts = split_markdown(reply)
    assert "".join(parts) == reply and all(len(p) <= 3500 for p in parts)


def test_code_block_crossing_a_split() -> None:
    code = "\n".join(f"line {i} = {i * 2}" for i in range(400))
    reply = f"Here:\n\n```python\n{code}\n```\n\nThat's it."
    parts = split_markdown(reply)
    assert len(parts) >= 2
    for part in parts:
        fences = [line for line in part.split("\n") if line.startswith("```")]
        assert len(fences) % 2 == 0, part[:80]
    assert all(p.startswith("```python\n") for p in parts[1:-1])
    # Every code line arrives exactly once.
    joined = "\n".join(parts)
    assert all(joined.count(f"line {i} = {i * 2}\n") == 1 for i in range(399))
    # Each part with code renders as its own complete <pre> block.
    for part in parts:
        if "```" in part:
            html = to_telegram_html(part)
            assert html.count("<pre") == 1
            assert html.count("</pre>") == 1


def test_rendered_parts_fit_telegram() -> None:
    reply = "&<>" * 3000  # escaping grows this 4-5x
    pairs = split_for_telegram(reply)
    assert all(len(html) <= TELEGRAM_LIMIT for _, html in pairs)
    assert "".join(md for md, _ in pairs) == reply


def test_empty_text() -> None:
    assert split_markdown("") == []
