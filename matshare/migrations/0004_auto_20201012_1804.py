# Generated by Django 3.0.10 on 2020-10-12 18:04

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("matshare", "0003_auto_20200921_1130"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="language",
            field=models.CharField(
                blank=True,
                choices=[("en", "English")],
                max_length=10,
                null=True,
                verbose_name="preferred language",
            ),
        ),
    ]