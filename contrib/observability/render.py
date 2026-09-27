#!/usr/bin/env python3
"""Render an observability YAML with the same environment source as Koyorina."""
import argparse
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("forge_environments", ROOT / "environments/load.py")
environments = importlib.util.module_from_spec(spec)
spec.loader.exec_module(environments)

parser = argparse.ArgumentParser()
parser.add_argument("file", choices=("cilium-policy.yaml", "collector.yaml"))
parser.add_argument("--environment", default="dev", choices=environments.names())
args = parser.parse_args()
print(environments.render((Path(__file__).parent / args.file).read_text(), args.environment), end="")
