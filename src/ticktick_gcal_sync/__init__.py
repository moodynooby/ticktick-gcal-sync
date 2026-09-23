from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def main() -> None:
    script = Path(__file__).resolve().parents[2] / "ticktick_gcal_sync.py"
    spec = spec_from_file_location("ticktick_gcal_sync_script", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {script}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()
