from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.webhooks.delivery import replay_deliveries


class Command(BaseCommand):
    help = "Re-enqueue failed (and stuck pending) webhook deliveries for active installations."

    def add_arguments(self, parser):
        parser.add_argument("--since-hours", type=int, default=72, help="Only replay deliveries created this recently.")
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument("--no-stale", action="store_true", help="Skip pending deliveries that look stuck.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, since_hours, limit, no_stale, dry_run, **options):
        since = timezone.now() - timedelta(hours=since_hours) if since_hours else None
        ids = replay_deliveries(since=since, limit=limit, include_stale_pending=not no_stale, dry_run=dry_run)
        verb = "Would replay" if dry_run else "Replayed"
        self.stdout.write(self.style.SUCCESS(f"{verb} {len(ids)} webhook deliveries."))
        if ids and options["verbosity"] > 1:
            self.stdout.write(", ".join(map(str, ids)))
