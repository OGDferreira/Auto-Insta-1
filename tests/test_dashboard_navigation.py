from pathlib import Path
import re


TEMPLATE = Path(__file__).parents[1] / "app" / "templates" / "dashboard.html"


def test_dashboard_tabs_have_matching_panels_and_hash_navigation():
    source = TEMPLATE.read_text()
    tabs = re.findall(r'<a class="workspace-tab[^>]*" href="#([^"]+)" data-view="([^"]+)"', source)
    panel_views = set(re.findall(r'class="workspace-view[^\"]*" data-panel="([^"]+)"', source))
    assert tabs
    assert {view for _, view in tabs} == panel_views
    assert 'event.preventDefault();selectDashboardView(link.dataset.view)' in source
    assert 'history.replaceState(null,"",`${window.location.pathname' in source


def test_dashboard_inline_script_is_balanced_after_account_navigation_fix():
    source = TEMPLATE.read_text()
    scripts = re.findall(r'<script>(.*?)</script>', source, flags=re.S)
    assert scripts
    script = scripts[0]
    assert 'dashboardAccountRows.forEach(row=>{' in script
    assert '});document.addEventListener("mouseup"' in script
    assert 'dashboardAccountTabs.forEach(tab=>tab.addEventListener' in script
