import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "solar.settings")

if __name__ == "__main__":
    from solar.config import initialize_storage
    initialize_storage()
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
