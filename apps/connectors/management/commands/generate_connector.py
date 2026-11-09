import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.connectors.config_schema import validate_config
from apps.connectors.generator import generate_connector
from apps.connectors.llm import OllamaClient
from apps.connectors.spec_parser import find_target_schema, load_spec
from apps.marketplace.models import App


class Command(BaseCommand):
    help = "Draft an employee field-mapping connector for a third-party app from its OpenAPI spec."

    def add_arguments(self, parser):
        parser.add_argument("spec", help="Path to the app's OpenAPI 3 spec (YAML or JSON).")
        parser.add_argument("--app", required=True, help="Marketplace app slug.")
        parser.add_argument("--schema", help="Request schema to target (auto-detected if omitted).")
        parser.add_argument("--model", help="Ollama model (default: settings.OLLAMA_MODEL).")
        parser.add_argument("--no-llm", action="store_true", help="Use only the heuristic mapper.")
        parser.add_argument("--out", help="Write the config to this file instead of stdout.")
        parser.add_argument("--save", action="store_true", help="Store the config on the App record.")
        parser.add_argument(
            "--validate", metavar="CONFIG", help="Validate an existing config file against the spec and exit."
        )

    def handle(self, *args, **opts):
        if opts["validate"]:
            return self._validate(opts)

        try:
            result = generate_connector(
                opts["spec"],
                opts["app"],
                schema_name=opts["schema"],
                client=OllamaClient(model=opts["model"]) if not opts["no_llm"] else None,
                use_llm=not opts["no_llm"],
            )
        except (OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        for warning in result.warnings:
            self.stderr.write(self.style.WARNING(warning))
        mapped = len(result.config["fields"])
        total = len(result.target.fields)
        self.stderr.write(
            f"{result.target.name} ({result.target.operation or 'component'}): "
            f"mapped {mapped}/{total} target fields via {result.source}."
        )

        output = json.dumps(result.config, indent=2) + "\n"
        if opts["out"]:
            Path(opts["out"]).write_text(output)
            self.stderr.write(self.style.SUCCESS(f"Wrote {opts['out']}"))
        else:
            self.stdout.write(output)

        if opts["save"]:
            if any(w.startswith("needs review") for w in result.warnings):
                raise CommandError("Refusing to --save a config that fails validation; fix it and use --validate.")
            updated = App.objects.filter(slug=opts["app"]).update(connector_config=result.config)
            if not updated:
                raise CommandError(f"No app with slug '{opts['app']}'.")
            self.stderr.write(self.style.SUCCESS(f"Saved connector on app '{opts['app']}'."))

    def _validate(self, opts):
        spec = load_spec(opts["spec"])
        config = json.loads(Path(opts["validate"]).read_text())
        target = find_target_schema(spec, config.get("target", {}).get("schema") or opts["schema"])
        errors = validate_config(config, target.fields)
        if errors:
            raise CommandError("Invalid connector config:\n- " + "\n- ".join(errors))
        self.stdout.write(self.style.SUCCESS("Connector config is valid."))
