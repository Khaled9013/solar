from solar.core.admin_terminal import shells
from solar.core.models import Audit
from solar.core.views import api, response


@api(("GET", "POST", "DELETE"), admin=True)
def admin_terminal(request):
    if not request.user.host_access:
        raise PermissionError("Trusted host execution permission is required to use PowerShell")
    key = request.session.session_key
    session = shells.get(request.user, key)
    if request.method == "POST":
        try:
            session = shells.start(request.user, key)
        except OSError as exc:
            raise ValueError("The PC could not start PowerShell. Check the server account's process and terminal permissions.") from exc
        Audit.objects.create(actor=request.user, action="admin_terminal_started", details=session.id)
    elif request.method == "DELETE" and session:
        session.stop()
        Audit.objects.create(actor=request.user, action="admin_terminal_stopped", details=session.id)
    return response({"terminal": session.status() if session else None})
