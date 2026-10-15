import ast
import inspect
import json
import shutil
from pathlib import Path
from unittest import mock

from conftest import BaseCase
import account
import isolation
import store


class TestAudit207Fix(BaseCase):
    def test_unicode_scalar_rejected_by_decoder(self):
        for escaped in (r'\uD800', r'\uDFFF', r'\U0000D800', r'\U00110000'):
            with self.subTest(escaped=escaped):
                with self.assertRaises(ValueError):
                    isolation._decode_toml_basic_string(escaped)

    def test_invalid_basic_keys_and_values_leave_config_untouched(self):
        data = self._tmp / 'codex-data'
        data.mkdir()
        path = data / 'config.toml'
        for text in ('"\\uD800" = 1\n', 'key = "\\uD800"\n', 'key = ["\\uDFFF"]\n',
                     'key = """\\uD800"""\n'):
            with self.subTest(text=text):
                path.write_text(text)
                original = path.read_bytes()
                with self.assertRaises(isolation.IsolationError):
                    isolation._disable_codex_daemon_auto_start(data)
                self.assertEqual(path.read_bytes(), original)

    def test_encoding_write_error_is_sanitized_for_create_and_update(self):
        data = self._tmp / 'codex-data'
        data.mkdir()
        path = data / 'config.toml'
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    path.write_text('model = "synthetic"\n')
                error = UnicodeEncodeError('utf-8', 'sensitive value', 0, 1, 'synthetic reason')
                with mock.patch.object(isolation.store, 'atomic_write_text', side_effect=error):
                    with self.assertRaises(isolation.IsolationError) as caught:
                        isolation._disable_codex_daemon_auto_start(data)
                self.assertNotIn('sensitive value', str(caught.exception))
                self.assertNotIn('synthetic reason', str(caught.exception))

    def test_all_atomic_text_writes_handle_valueerror(self):
        tree = ast.parse(inspect.getsource(isolation))
        writes = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Try) and any(
                isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                and child.func.attr == 'atomic_write_text'
                for statement in node.body for child in ast.walk(statement)
            ):
                handled = {child.id for handler in node.handlers if handler.type
                           for child in ast.walk(handler.type) if isinstance(child, ast.Name)}
                writes.append(node.lineno)
                self.assertIn('ValueError', handled)
        all_writes = [node for node in ast.walk(tree)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and node.func.attr == 'atomic_write_text']
        self.assertEqual(len(writes), len(all_writes))
        self.assertEqual(len(writes), 2)

    def test_exact_api_key_and_comments(self):
        data = self._tmp / 'grok-data'
        data.mkdir()
        path = data / 'config.toml'
        cases = [
            ('api_key_env = "MY_ENV"', None),
            ('api_key_helper = "/usr/bin/helper"', None),
            ('api_key_command = "synthetic command"', None),
            ('api_key = "sk-12345" # primary key', 'sk-12345'),
            ("api_key = 'sk-abc'", 'sk-abc'),
            ('api_key=sk-no-quotes', 'sk-no-quotes'),
            ('# api_key = "commented-out"', None),
            ('[section]\napi_key = "sk-12345"', 'sk-12345'),
            ('api_key="first"\napi_key="last"', 'last'),
            ('"api_key"="quoted"', 'quoted'),
            ("'api_key'='literal'", 'literal'),
            ('api_key="sk#hash"#comment', 'sk#hash'),
            (r'api_key="sk\"quote" # comment', 'sk"quote'),
            ('api_key="unterminated', None),
            ('api_key="valid" trailing-junk', None),
            ('api_key="first"\napi_key=""', None),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                path.write_text(text)
                self.assertEqual(account.grok_config_api_key(data), expected)

    def test_helper_only_does_not_authenticate_grok(self):
        data = self._tmp / 'grok-data'
        data.mkdir()
        (data / 'config.toml').write_text('api_key_env="MY_ENV"\n')
        self.assertIsNone(account.inspect_grok_auth(data))
        self.assertEqual(account.auth_state(data, engine='grok'), 'not-authenticated')

    def test_display_email_rejects_link_with_warning(self):
        profile_store = store.Store(self.store_root)
        profile = profile_store.create('linked', engine='codex')
        data = profile_store.profiles_dir / profile.name / 'data'
        external = self._tmp / 'external'
        external.mkdir()
        shutil.rmtree(data)
        try:
            data.symlink_to(external, target_is_directory=True)
        except OSError as exc:
            self.skipTest('directory symlink unavailable: ' + type(exc).__name__)
        with mock.patch('ui.warn') as warning:
            self.assertIsNone(account.display_email(profile_store, profile))
        rendered = str(warning.call_args)
        self.assertIn('linked', rendered)
        self.assertIn('StoreError', rendered)

    def test_display_email_read_failure_is_sanitized(self):
        profile_store = store.Store(self.store_root)
        profile = profile_store.create('alpha', engine='codex')
        with mock.patch.object(account, 'detect_email', side_effect=PermissionError('secret detail')), \
             mock.patch('ui.warn') as warning:
            self.assertIsNone(account.display_email(profile_store, profile))
        self.assertIn('PermissionError', str(warning.call_args))
        self.assertNotIn('secret detail', str(warning.call_args))

    def test_valid_unicode_scalars_and_multiline_continuation_survive(self):
        self.assertEqual(isolation._decode_toml_basic_string(r'\uD7FF\uE000\U0010FFFF'),
                         '\ud7ff\ue000\U0010ffff')
        text = 'key = """first\\\n  second"""\n'
        updated = isolation._with_daemon_auto_start_disabled(text)
        self.assertTrue(updated.startswith(text))
        self.assertIn('daemon_auto_start = false', updated)
