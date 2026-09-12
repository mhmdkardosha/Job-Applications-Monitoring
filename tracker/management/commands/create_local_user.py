from __future__ import annotations

import getpass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create (or reset the password of) the single local user."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="local")
        parser.add_argument(
            "--password",
            default=None,
            help="Password; if omitted you are prompted (recommended).",
        )
        parser.add_argument("--email", default="")

    def handle(self, *args, **options):
        User = get_user_model()
        username = options["username"]
        password = options["password"] or getpass.getpass("Password: ")
        if not password:
            raise CommandError("Password cannot be empty.")
        user, created = User.objects.get_or_create(
            username=username, defaults={"email": options["email"]}
        )
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()
        action = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{action} local user '{username}'."))
