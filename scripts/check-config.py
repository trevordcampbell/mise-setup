"""Validate restore prerequisites without reading or changing a host setup."""
from pathlib import Path
import tomllib

root = Path(__file__).resolve().parents[1]
config = tomllib.loads((root / 'config.toml').read_text())
# Every tracked shell link must resolve within this repository on a new Mac.
for name, entry in config['dotfiles'].items():
    prefix = '~/.config/mise/'
    source = entry['source']
    if source.startswith(prefix):
        path = root / source.removeprefix(prefix)
        assert path.is_file(), f'Missing bootstrap source for {name}: {path}'
        assert path.resolve().is_relative_to(root), f'Source escapes checkout: {name}'
assert '--scheduled' in config['bootstrap']['macos']['launchd']['agents']['mise-maintenance']['args']
assert 'macos-app:marktext' not in config['bootstrap']['packages'], 'MarkText must use the release resolver'
assert config['vars']['marktext_artifact'] == 'MarkText.app'
assert '--marktext' in ' '.join(config['bootstrap']['hooks']['final']), 'Fresh restore must resolve MarkText'
assert 'install' in config['bootstrap']['hooks']['pre-packages'], 'Tools must precede fresh-machine font hooks'
print('Bootstrap sources, scheduled origin, and fresh MarkText resolver verified.')
