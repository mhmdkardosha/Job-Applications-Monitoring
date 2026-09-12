from django.db import migrations, models

import tracker.models


class Migration(migrations.Migration):
    dependencies = [("tracker", "0003_alter_email_process_state")]

    operations = [
        migrations.AddField(
            model_name="appsettings",
            name="board_card_fields",
            field=models.JSONField(blank=True, default=tracker.models.default_board_card_fields),
        ),
    ]
