from django.urls import path
from solar.core import views as v
from solar.core import project_views as p
from solar.core.terminal_views import admin_terminal

urlpatterns = [
    path("api/admin-terminal/", admin_terminal),
    path("api/projects/", p.collection), path("api/projects/<uuid:project_id>/", p.detail),
    path("api/projects/<uuid:project_id>/tree/", p.tree), path("api/projects/<uuid:project_id>/files/", p.file),
    path("api/workspace-folders/", p.directories),
    path("", v.index), path("api/me/", v.me), path("api/login/", v.sign_in), path("api/logout/", v.sign_out),
    path("api/password/", v.password_change), path("api/users/", v.users),
    path("api/users/<uuid:user_id>/", v.user_detail), path("api/providers/", v.providers),
    path("api/providers/<uuid:provider_id>/", v.provider_detail), path("api/providers/<uuid:provider_id>/models/", v.models),
    path("api/agents/", v.agents), path("api/agents/<uuid:agent_id>/", v.agent_detail),
    path("api/skills/", v.skills), path("api/skills/<uuid:skill_id>/", v.skill_detail),
    path("api/chats/", v.chats), path("api/chats/<uuid:chat_id>/", v.chat_detail),
    path("api/chats/<uuid:chat_id>/events/", v.events), path("api/chats/<uuid:chat_id>/messages/", v.send_message),
    path("api/chats/<uuid:chat_id>/native/", v.native_start), path("api/chats/<uuid:chat_id>/files/", v.files),
    path("api/chats/<uuid:chat_id>/export/", v.export_chat), path("api/files/<uuid:file_id>/", v.file_detail),
    path("api/files/<uuid:file_id>/extract/", v.file_extract), path("api/runs/", v.runs),
    path("api/runs/<uuid:run_id>/stop/", v.run_stop), path("api/schedules/", v.schedules),
    path("api/schedules/<uuid:schedule_id>/", v.schedule_detail), path("api/desktop/", v.desktop_access),
]
