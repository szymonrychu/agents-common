import pytest

from agents_common import templating
from agents_common.templating import TemplateError


def test_render_basic():
    assert templating.render("hi {{ name }}", {"name": "bob"}) == "hi bob"


def test_sandbox_blocks_attribute_escape():
    with pytest.raises(TemplateError):
        templating.render("{{ ''.__class__.__mro__[1].__subclasses__() }}", {})


def test_sandbox_blocks_mutation():
    with pytest.raises(TemplateError):
        templating.render("{% set _ = items.append(1) %}", {"items": [1]})


def test_strict_undefined_reports_line():
    with pytest.raises(TemplateError) as info:
        templating.render("a\nb\n{{ missing }}", {})
    assert info.value.line == 3
    assert "missing" in str(info.value)


def test_template_size_cap():
    big = "x" * (templating.MAX_TEMPLATE_BYTES + 1)
    with pytest.raises(TemplateError, match="byte limit"):
        templating.render(big, {})
    with pytest.raises(TemplateError, match="byte limit"):
        templating.validate(big)


def test_output_cap_truncates():
    out = templating.render("{{ 'x' * 60000 }}{{ 'y' * 60000 }}", {})
    assert len(out.encode()) <= templating.MAX_BODY_BYTES + len(templating.TRUNCATION_MARKER)
    assert out.endswith(templating.TRUNCATION_MARKER)


def test_timeout_kills_loop():
    template = "{% for i in range(10000) %}{% for j in range(10000) %}{% endfor %}{% endfor %}"
    with pytest.raises(TemplateError, match="timed out"):
        templating.render(template, {}, timeout=0.5)


def test_brace_escape_renders_literally():
    assert templating.render("{{ '{{' }} x {{ '}}' }}", {}) == "{{ x }}"


def test_validate_syntax_error_has_line():
    with pytest.raises(TemplateError) as info:
        templating.validate("ok\n{% if %}")
    assert info.value.line == 2


def test_undeclared_variables():
    assert templating.undeclared_variables("{{ a }} {% for x in b %}{{ x }}{% endfor %}") == [
        "a",
        "b",
    ]


def test_preview_ok():
    result = templating.preview("hi {{ name }}", {"name": "bob"})
    assert result == {"rendered": "hi bob", "error": None, "variables": ["name"]}


def test_preview_runtime_error():
    result = templating.preview("a\n{{ nope }}", {})
    assert result["rendered"] is None
    assert result["error"]["line"] == 2
    assert result["variables"] == ["nope"]


def test_preview_syntax_error():
    result = templating.preview("{% if %}", {})
    assert result["rendered"] is None
    assert result["error"]["line"] == 1
    assert result["variables"] == []


def test_from_json_filter_on_string():
    out = templating.render(
        "{{ (payload.s | from_json).a.b }}", {"payload": {"s": '{"a": {"b": 5}}'}}
    )
    assert out == "5"
    assert templating.render("{{ {'k': 1} | tojson }}", {}) == '{"k": 1}'


def test_from_json_error_is_template_error():
    with pytest.raises(TemplateError) as info:
        templating.render("{{ 'not json' | from_json }}", {})
    assert "json" in str(info.value).lower()


def test_from_json_validates_and_previews():
    templating.validate("{{ payload.x | from_json }}")
    result = templating.preview("{{ (raw | from_json).k }}", {"raw": '{"k": "v"}'})
    assert result["rendered"] == "v"
