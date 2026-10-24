from django.db import migrations


def adopt_chats(apps, schema_editor):
    Schedule = apps.get_model("core", "Schedule")
    Chat = apps.get_model("core", "Chat")
    Run = apps.get_model("core", "Run")
    for schedule in Schedule.objects.filter(chat__isnull=True).iterator():
        latest = schedule.history.filter(run__chat__owner_id=schedule.owner_id).order_by("-due_at").first()
        if latest and latest.run_id:
            schedule.chat_id = latest.run.chat_id
            schedule.save(update_fields=["chat"])
    ids = Run.objects.filter(mode="native").values_list("chat_id", flat=True)
    Chat.objects.filter(id__in=ids).exclude(native_state=b"").update(native_started=True)


class Migration(migrations.Migration):
    dependencies = [("core", "0004_chat_provider_session")]
    operations = [migrations.RunPython(adopt_chats, migrations.RunPython.noop)]
