"""Shared presentation catalog. No process-global request language or text substitution."""
import json
from pathlib import Path

CATALOG = json.loads((Path(__file__).resolve().parent / 'static' / 'messages.json').read_text(encoding='utf-8'))


def normalize_language(value=None, default='zh-CN'):
    if value is None:
        return default
    value = str(value).strip().lower().replace('_', '-')
    if value == 'zh' or value.startswith('zh-'):
        return 'zh-CN'
    return 'en'


def request_language(payload=None, accept_language=None):
    if isinstance(payload, dict) and 'language' in payload:
        return normalize_language(payload['language'], default='en')
    if accept_language:
        preferences = []
        for index, entry in enumerate(accept_language.split(',')):
            tag, *parameters = entry.strip().split(';')
            quality = 1.0
            try:
                for parameter in parameters:
                    if parameter.strip().startswith('q='):
                        quality = float(parameter.strip()[2:])
            except ValueError:
                quality = 0
            if quality > 0:
                preferences.append((quality, -index, tag))
        if preferences:
            return normalize_language(max(preferences)[2], default='en')
    return 'zh-CN'


def format_message(code, params=None, language='zh-CN'):
    language = normalize_language(language)
    def value(item):
        if isinstance(item, dict) and isinstance(item.get('code'), str):
            return format_message(item['code'], item.get('params', {}), language)
        if isinstance(item, (list, tuple)):
            return '; '.join(str(value(part)) for part in item)
        return item
    template = CATALOG[language].get(code, CATALOG['en'].get(code, code))
    return template.format_map({key: value(item) for key, item in (params or {}).items()})


class InputError(ValueError):
    """Structured user-correctable failure; legacy str() remains Chinese."""
    def __init__(self, code, **params):
        self.code = code
        self.params = params
        super().__init__(format_message(code, params, 'zh-CN'))


def error_payload(exc, language='zh-CN'):
    code = exc.code if isinstance(exc, InputError) else 'http.invalid'
    params = exc.params if isinstance(exc, InputError) else {}
    return {'error': format_message(code, params, language), 'error_code': code, 'error_params': params}
