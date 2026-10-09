from phase2_fakes import make_issue, make_page
from research_autopilot.models import TaskSpec


def spec() -> TaskSpec:
    return TaskSpec.from_sources(make_issue(), make_page())
