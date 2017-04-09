from django.test import SimpleTestCase
from django.utils.functional import lazy
from django.utils.html import js_json as _js_json

from ..utils import setup


class JsJsonTests(SimpleTestCase):

    @setup({'js-json01': '{{ value|js_json:"test_id" }}'})
    def test_js_json01(self):
        output = self.engine.render_to_string(
            'js-json01',
            {'value': {'a': 'testing\r\njson \'string" <b>escaping</b>'}})
        self.assertEqual(output, '<script id="test_id" type="application/json">'
                                 '{"a": "testing\\r\\njson \'string\\" '
                                 '\\u003cb\\u003eescaping\\u003c/b\\u003e"}'
                                 '</script>')


class FunctionTests(SimpleTestCase):

    def test_ampersand(self):
        self.assertEqual(
            _js_json('&', 'test_id'),
            '<script id="test_id" type="application/json">"\\u0026"</script>'
        )

    def test_json_str(self):
        self.assertEqual(
            _js_json('{"a": "testing</script>"}', 'test_id'),
            '<script id="test_id" type="application/json">'
            '{"a": "testing\\u003c/script\\u003e"}</script>'
        )

    def test_script(self):
        self.assertEqual(
            _js_json(r'<script>and this</script>', 'test_id'),
            '<script id="test_id" type="application/json">"'
            '\\u003cscript\\u003eand this\\u003c/script\\u003e"'
            '</script>'
        )

    def test_lazy_string(self):
        append_script = lazy(lambda string: r'<script>this</script>' + string, str)
        self.assertEqual(
            _js_json(append_script('ampersand: &amp;'), 'test_id'),
            '<script id="test_id" type="application/json">"'
            '\\u003cscript\\u003ethis\\u003c/script\\u003e'
            'ampersand: \\u0026amp;"'
            '</script>'
        )
