"""只读核对正式前端素材和模板，防止再次混用其他原型。"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / 'design/前端原型/讲给我听-大字圆体版-Mac与Windows'
FORMAL = ROOT / 'frontend/formal'


def object_literal(path):
    source = path.read_text(encoding='utf-8')
    return json.JSONDecoder().raw_decode(source[source.index('{'):])[0]


def test_all_reference_templates_are_exact_copies():
    original = object_literal(REFERENCE / 'src/screens.js')
    copied = object_literal(FORMAL / 'reference.js')
    assert len(original) == len(copied) == 36
    for key, screen in original.items():
        assert copied[key]['name'] == screen['name']
        assert copied[key]['body'] == screen['body'].replace('assets/', '/static/formal/assets/')
    assert (FORMAL / 'reference.css').read_text(encoding='utf-8').endswith(original['01']['style'])


def test_theme_and_assets_match_reference():
    original = (REFERENCE / 'src/theme.css').read_text(encoding='utf-8')
    copied = (FORMAL / 'theme.css').read_text(encoding='utf-8')
    assert copied == original.replace('../assets/', 'assets/')
    assets = [path for path in (REFERENCE / 'assets').rglob('*') if path.is_file()]
    assert assets
    for path in assets:
        assert (FORMAL / 'assets' / path.relative_to(REFERENCE / 'assets')).read_bytes() == path.read_bytes()


def test_formal_entry_does_not_run_prototype_simulation():
    entry = (FORMAL / 'index.html').read_text(encoding='utf-8')
    assert 'iframe' not in entry
    assert 'interactions.js' not in entry and 'features.js' not in entry
    renderer = (FORMAL / 'views.js').read_text(encoding='utf-8')
    assert "from './prototype.js'" in renderer
    assert '<section' not in renderer
