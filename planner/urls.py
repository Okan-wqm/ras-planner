from django.urls import path
from django.views.generic import RedirectView

from . import views, views_ai, views_docs, views_process, views_steps, views_tools
from . import agents as agent_views

urlpatterns = [
    path("", views.home, name="dashboard_home"),
    path("scenario/<int:pk>/", views.dashboard, name="dashboard"),
    path("scenario/new/", views.scenario_edit, name="scenario_new"),
    path("scenario/<int:pk>/constants/", views.scenario_edit, name="scenario_edit"),
    path("scenario/<int:pk>/duplicate/", views.scenario_copy, name="scenario_copy"),
    path("scenario/<int:pk>/delete/", views.scenario_delete, name="scenario_delete"),
    path("scenario/<int:pk>/biofilter/", views.biofilter, name="biofilter"),
    path("scenario/<int:pk>/biofilter/design/", views.biofilter_design, name="biofilter_design"),
    path("scenario/<int:pk>/brief/", views_steps.brief, name="brief"),
    path("scenario/<int:pk>/production/", views.production, name="production"),
    path("scenario/<int:pk>/units/", views_steps.units, name="units"),
    path("scenario/<int:pk>/requirements/", views_steps.requirements, name="requirements"),
    path("scenario/<int:pk>/excel/", views.export_xlsx, name="export_xlsx"),
    path("scenario/<int:pk>/equipment/", views.equipment, name="equipment"),
    path("scenario/<int:pk>/capex/", views.capex, name="capex"),
    path("scenario/<int:pk>/process/", views_process.process, name="process"),
    path("scenario/<int:pk>/process/solve/", views_process.process_solve, name="process_solve"),
    path("scenario/<int:pk>/process/<int:index>/save/", views_process.process_save, name="process_save"),
    path("scenario/<int:pk>/process/<int:index>/reset/", views_process.process_reset, name="process_reset"),
    path("process-library/", views_process.process_library, name="process_library"),
    path("process-library/<int:tid>/", views.process_library_item, name="process_library_item"),
    path("scenario/<int:pk>/process/custom-solve/", views_process.process_custom_solve, name="process_custom_solve"),
    path("scenario/<int:pk>/opex/", views_docs.opex, name="opex"),
    path("scenario/<int:pk>/process/<int:index>/snapshot.png", views_docs.process_snapshot, name="process_snapshot"),
    path("proposals/", views_docs.proposals, name="proposals"),
    path("proposals/<int:pid>/", views_docs.proposal_edit, name="proposal_edit"),
    path("proposals/<int:pid>/document/", views_docs.proposal_doc, name="proposal_doc"),
    path("proposals/<int:pid>/pdf/", views_docs.proposal_pdf, name="proposal_pdf"),
    path("proposals/<int:pid>/delete/", views_docs.proposal_delete, name="proposal_delete"),
    path("proposals/<int:pid>/duplicate/", views_docs.proposal_copy, name="proposal_copy"),
    path("proposals/<int:pid>/ai/", views_docs.proposal_ai, name="proposal_ai"),
    path("company/", views_docs.company, name="company"),
    path("ai-settings/", views_docs.ai_settings, name="ai_settings"),
    path("ai/chat/", views_ai.ai_chat, name="ai_chat"),
    path("ai/mission/", agent_views.ai_mission, name="ai_mission"),
    path("agents/", views_ai.agents_page, name="agents_page"),
    path("library/jet-oxygenator/", views_tools.jet_oxygenator, name="jet_oxygenator"),
    path("library/export-credit/", views_tools.loan_calculator, name="loan_calculator"),
    path("feeds/", views.feeds, name="feeds"),
    path("standards/", views.standards, name="standards"),
]

# Old Turkish URLs: redirect so existing tabs and bookmarks keep working
_old = {
    "senaryo/<int:pk>/": "dashboard",
    "senaryo/<int:pk>/duzenle/": "scenario_edit",
    "senaryo/<int:pk>/biyofiltre/": "biofilter",
    "senaryo/<int:pk>/uretim/": "production",
    "senaryo/<int:pk>/excel/": "export_xlsx",
}
urlpatterns += [path(old, RedirectView.as_view(pattern_name=new, permanent=True))
                for old, new in _old.items()]
urlpatterns += [
    path("senaryo/yeni/", RedirectView.as_view(pattern_name="scenario_new", permanent=True)),
    path("yemler/", RedirectView.as_view(pattern_name="feeds", permanent=True)),
]
