"""Opt-in live provider acceptance for an explicitly supplied disposable script."""
import argparse
import json
import time
from pathlib import Path

from clawtune_sidecar.config import SidecarConfig
from command_parser import CommandFeatureService, ParseContext
from command_parser.generation import GeneratorConfig, RuleGenerator
from command_parser.registry import RuleRegistry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("script")
    args = parser.parse_args()
    config = GeneratorConfig.from_sidecar(SidecarConfig.from_env())
    if not (config.base_url and config.api_key and config.model):
        parser.error("configure upstream URL, API key and model first")
    workspace = args.workspace.resolve(strict=True)
    context = ParseContext(repo="rule-acceptance", runtime_id="rule-acceptance",
                           workspace=str(workspace), cwd=str(workspace))
    registry = RuleRegistry(workspace / ".clawtune-command-rules")
    generator = RuleGenerator(registry, config)
    service = CommandFeatureService(registry, generator)
    try:
        argv = ["python", args.script]
        before = service.argv(argv, context)
        deadline = time.monotonic() + 2 * config.timeout + 10
        while time.monotonic() < deadline:
            states = generator.status()
            if not states or all(row["status"] in {"ready", "failed"} for row in states.values()):
                break
            time.sleep(.1)
        after = service.argv(argv, context)
        print(json.dumps({"before": before.to_dict(), "after": after.to_dict(),
                          "jobs": generator.status()}, indent=2))
        rule, _ = registry.lookup(args.script, context, script=True)
        return 0 if rule else 1
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
