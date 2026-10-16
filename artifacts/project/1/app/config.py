import yaml
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
def load_yaml(path):
    p = Path(path)
    with open(p if p.is_absolute() else ROOT / p, encoding="utf-8") as f:
        return yaml.safe_load(f)
def settings():
    return load_yaml("config/sources.yaml")
