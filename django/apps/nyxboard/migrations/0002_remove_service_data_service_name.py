# Generated by Django 5.2 on 2025-04-13 12:53

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("nyxboard", "0001_initial"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="service",
            name="data",
        ),
        migrations.AddField(
            model_name="service",
            name="name",
            field=models.CharField(
                default="service", max_length=255, verbose_name="Service Name"
            ),
            preserve_default=False,
        ),
    ]
