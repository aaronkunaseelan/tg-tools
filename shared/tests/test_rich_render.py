"""Pure table for tg_tools_common.rich_render: which delivery shape a reply takes.

| case                 | input                                  | capability | delivery kind         | fallback reason          |
| rich markdown        | heading + list                         | rich       | telegram_rich_message | -                        |
| pi report stays rich | links, code, quote, table, footnote, $ | rich       | telegram_rich_message | -                        |
| no rich capability   | "<unsafe>"                             | plain      | telegram_text (escaped)| rich_not_supported      |
| long plain fallback  | 4000 lines                             | plain      | telegram_document     | rich_not_supported       |
| over utf-8 limit     | 32769 bytes                            | rich       | telegram_document     | rich_utf8_limit          |
| local media          | ![](/tmp/local.png)                    | rich       | telegram_text         | rich_media_requires_http |
| limits               | blocks / nesting / media / table cols  | -          | RichLimitError naming the limit                  |
"""
import pytest

from tg_tools_common.rich_render import (MAX_RICH_BLOCKS, MAX_RICH_MEDIA, MAX_RICH_NESTING, MAX_RICH_TABLE_COLUMNS,
                                         RichCapability, RichLimitError, render_rich_text, validate_rich_markdown)

PI_REPORT = """# Run Report

Paragraph with [link](https://example.com), `code`, and $x^2 + y^2$.

> Quote with **bold** text

- item one
- item two

```python
print("ok")
```

| Metric | Value |
|:--|--:|
| Tests | 18 |

Text with footnote[^n].

[^n]: Footnote body.

$$E = mc^2$$
"""


@pytest.mark.parametrize("text, rich, kind, reason", [
    ("# Result\n\n- ok", True, "telegram_rich_message", None),
    (PI_REPORT, True, "telegram_rich_message", None),
    ("![](https://example.com/image.png)", True, "telegram_rich_message", None),
    ("<unsafe>", False, "telegram_text", "rich_not_supported"),
    ("plain text\n" * 4000, False, "telegram_document", "rich_not_supported"),
    ("x" * 32769, True, "telegram_document", "rich_utf8_limit"),
    ("![](/tmp/local.png)", True, "telegram_text", "rich_media_requires_http"),
])
def test_render(text, rich, kind, reason):
    rendered = render_rich_text(text, RichCapability(final_rich_supported=rich))
    assert (rendered.delivery_kind, rendered.fallback_reason) == (kind, reason)
    if kind == "telegram_rich_message":
        assert rendered.rich_message == {"markdown": text, "skip_entity_detection": False}
    if text == "<unsafe>":
        assert rendered.html_chunks == ("&lt;unsafe&gt;",)


@pytest.mark.parametrize("markdown, limit", [
    ("\n".join(f"- item {i}" for i in range(MAX_RICH_BLOCKS + 1)), "rich_block_limit"),
    (" " * ((MAX_RICH_NESTING + 1) * 2) + "- nested", "rich_nesting_limit"),
    ("\n".join(f"![](https://example.com/{i}.png)" for i in range(MAX_RICH_MEDIA + 1)), "rich_media_limit"),
    ("|" + "|".join(str(i) for i in range(MAX_RICH_TABLE_COLUMNS + 1)) + "|", "rich_table_column_limit"),
])
def test_limits(markdown, limit):
    with pytest.raises(RichLimitError, match=limit):
        validate_rich_markdown(markdown)
